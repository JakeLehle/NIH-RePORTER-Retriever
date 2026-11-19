"""Pipeline steps. Each function is called by one Snakemake rule and by the CLI."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .api import ReporterClient
from .normalize import records_to_frame
from .queries import QuerySpec

log = logging.getLogger(__name__)


def write_json(obj: Any, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
    tmp.replace(path)


def read_json(path: str | Path) -> Any:
    with Path(path).open(encoding="utf-8") as fh:
        return json.load(fh)


def write_spec(spec: QuerySpec, path: str | Path) -> None:
    write_json(spec.to_dict(), path)


def fetch_query(spec_path: str | Path, out_path: str | Path, api_cfg: dict[str, Any] | None = None) -> int:
    """Run one query against RePORTER and save the raw response records.

    If the spec has ``complete_grants`` set, a second pass looks up every grant
    the search found by its core project number (with the same filters, e.g.
    active only), so all of its records are included even when a renewal,
    supplement or continuation was filed under a different opportunity number
    or does not mention the search text. Each record is tagged with
    ``_found_by``: "search" or "grant_lookup".
    """
    api_cfg = api_cfg or {}
    spec = QuerySpec.from_dict(read_json(spec_path))
    client = ReporterClient(
        min_interval=float(api_cfg.get("min_seconds_between_requests", 1.0)),
        max_retries=int(api_cfg.get("max_retries", 5)),
        timeout=float(api_cfg.get("timeout_seconds", 120)),
        page_size=int(api_cfg.get("page_size", 500)),
    )
    log.info("Fetching %s (%s: %s)", spec.query_id, spec.query_type, spec.value)
    result = client.search(spec.criteria)
    records = {r["appl_id"]: {**r, "_found_by": "search"} for r in result["results"]}

    n_lookup = 0
    if spec.complete_grants and records:
        cores = sorted({r["core_project_num"] for r in records.values() if r.get("core_project_num")})
        base = spec.filter_criteria()
        batch = int(api_cfg.get("grant_lookup_batch_size", 100))
        for i in range(0, len(cores), batch):
            extra = client.search({**base, "project_nums": cores[i:i + batch]})
            for r in extra["results"]:
                if r["appl_id"] not in records:
                    records[r["appl_id"]] = {**r, "_found_by": "grant_lookup"}
                    n_lookup += 1
        log.info("Grant lookup over %d grants added %d records", len(cores), n_lookup)

    payload = {
        "query": spec.to_dict(),
        **result,
        "search_records": len(result["results"]),
        "grant_lookup_records": n_lookup,
        "results": list(records.values()),
    }
    write_json(payload, out_path)
    log.info("Saved %d records for %s -> %s", len(records), spec.query_id, out_path)
    return len(records)


def flatten_query(raw_path: str | Path, out_tsv: str | Path) -> None:
    """Per-query flat table (one row per RePORTER record)."""
    raw = read_json(raw_path)
    df = records_to_frame(raw["results"])
    df.insert(0, "query_id", raw["query"]["query_id"])
    Path(out_tsv).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_tsv, sep="\t", index=False)
