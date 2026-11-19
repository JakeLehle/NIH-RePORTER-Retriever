"""Thin client for the NIH RePORTER v2 projects search endpoint.

API docs: https://api.reporter.nih.gov/

Notes on the API that shape this client:
  * Results are paged with ``offset``/``limit``; ``limit`` is at most 500 and
    ``offset`` at most 14,999, so one query can return at most 15,000 records.
  * NIH asks callers to make no more than ~1 request per second, so every
    request goes through a process-wide throttle (shared across threads, which
    is how Snakemake executes ``run:`` blocks locally).
  * The API returns one record per application (``appl_id``), i.e. one row per
    fiscal year / supplement / subproject. Collapsing to one row per grant is
    done downstream (see ``database.py``).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

from . import __version__

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.reporter.nih.gov/v2/projects/search"
MAX_LIMIT = 500
MAX_OFFSET = 14_999
MAX_RECORDS = MAX_OFFSET + 1

_throttle_lock = threading.Lock()
_last_request = 0.0


class ReporterAPIError(RuntimeError):
    pass


class TooManyResultsError(ReporterAPIError):
    pass


def _throttle(min_interval: float) -> None:
    global _last_request
    with _throttle_lock:
        wait = _last_request + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


class ReporterClient:
    def __init__(
        self,
        min_interval: float = 1.0,
        max_retries: int = 5,
        timeout: float = 120.0,
        page_size: int = MAX_LIMIT,
        session: requests.Session | None = None,
    ):
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.page_size = min(page_size, MAX_LIMIT)
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"nih-reporter-retriever/{__version__}",
            }
        )

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            _throttle(self.min_interval)
            try:
                resp = self.session.post(SEARCH_URL, json=payload, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.json()
                # 429 / 5xx are worth retrying; 4xx (bad criteria) are not.
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_err = ReporterAPIError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                else:
                    raise ReporterAPIError(
                        f"HTTP {resp.status_code} for criteria {payload.get('criteria')}: {resp.text[:500]}"
                    )
            except (requests.ConnectionError, requests.Timeout, ValueError) as err:
                last_err = err
            backoff = min(60.0, 2.0 ** attempt)
            log.warning("Request failed (attempt %d/%d): %s; retrying in %.0fs",
                        attempt, self.max_retries, last_err, backoff)
            time.sleep(backoff)
        raise ReporterAPIError(f"Giving up after {self.max_retries} attempts: {last_err}")

    def search(self, criteria: dict[str, Any], include_fields: list[str] | None = None) -> dict[str, Any]:
        """Run one search and page through every result.

        Returns a dict with ``total``, ``results`` and provenance metadata.
        """
        results: list[dict[str, Any]] = []
        offset = 0
        total = None
        search_id = None
        while True:
            payload: dict[str, Any] = {
                "criteria": criteria,
                "offset": offset,
                "limit": self.page_size,
                "sort_field": "appl_id",
                "sort_order": "asc",
            }
            if include_fields:
                payload["include_fields"] = include_fields
            data = self._post(payload)
            meta = data.get("meta", {})
            if total is None:
                total = int(meta.get("total", 0))
                search_id = meta.get("search_id")
                log.info("Query matched %d records", total)
                if total > MAX_RECORDS:
                    raise TooManyResultsError(
                        f"Query matched {total} records but the API only pages through "
                        f"{MAX_RECORDS}. Narrow it (fiscal_years, agencies, activity_codes "
                        f"or a more specific search) and rerun. Criteria: {criteria}"
                    )
            page = data.get("results") or []
            results.extend(page)
            offset += len(page)
            if not page or offset >= total:
                break
            if offset > MAX_OFFSET:
                break
        if total is not None and len(results) != total:
            log.warning("Expected %d records but retrieved %d", total, len(results))
        return {
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "search_id": search_id,
            "reporter_url": f"https://reporter.nih.gov/search/{search_id}/projects" if search_id else None,
            "total": total or 0,
            "results": results,
        }
