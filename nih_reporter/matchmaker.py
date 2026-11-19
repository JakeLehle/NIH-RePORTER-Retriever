"""Find program officers for a piece of text (e.g. your Specific Aims) with
NIH RePORTER Matchmaker, and pick the ones worth a full portfolio pull.

Matchmaker (https://reporter.nih.gov/matchmaker) is not part of the documented
RePORTER API. This module calls the same service the Matchmaker web page uses:

  POST https://reporter.nih.gov/services/MatchMaker/ProjectPO   program officials table
  POST https://reporter.nih.gov/services/MatchMaker/Projects    top 500 similar projects

Because it is undocumented it may change without notice, or start asking for a
captcha; if so this module stops with a clear message and you can paste the
officers into program_officers.tsv by hand instead. One run makes ~6 requests,
throttled to one per second.

Counting officers
-----------------
Matchmaker reports, for every PO, how many of the ~500 most similar projects
they manage - but that attribution can be out of date (portfolios get
reassigned, officers leave). By default we therefore take Matchmaker's similar
projects, look each one up in the official RePORTER API, and credit it to the
program officer on its newest record ("current" counts). Matchmaker's own
counts are kept alongside for comparison.

Selecting officers
------------------
The counts are long-tailed (most POs manage one matching grant), so we keep
only the POs above a cutoff:

  knee          the "elbow" of the ranked counts: the point where counts stop
                dropping fast (Kneedle-style maximum distance below the chord)
  above_median  counts strictly above the median count
  min_projects  counts >= value
  top_n         the value highest-ranked POs (ties at the cutoff are kept)
  coverage      the fewest top POs that together manage >= value (e.g. 0.5)
                of all matched projects

``min_projects`` (default 2) is a floor applied on top of every method, and
``max_officers`` an optional cap. POs tied at the cutoff are always kept together.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from . import __version__
from .pipeline import write_json
from .queries import parse_person_name

log = logging.getLogger(__name__)

BASE = "https://reporter.nih.gov/services/MatchMaker"
MAX_TEXT_CHARS = 15_000
METHODS = ("knee", "above_median", "min_projects", "top_n", "coverage")

_lock = threading.Lock()
_last = 0.0


class MatchmakerError(RuntimeError):
    pass


def _throttle(min_interval: float) -> None:
    global _last
    with _lock:
        wait = _last + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()


class MatchmakerClient:
    def __init__(self, min_interval: float = 1.0, timeout: float = 120, max_retries: int = 3,
                 session: requests.Session | None = None):
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "User-Agent": f"nih-reporter-retriever/{__version__}",
        })

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            _throttle(self.min_interval)
            try:
                resp = self.session.post(f"{BASE}/{endpoint}", json=payload, timeout=self.timeout)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        data = None
                    if isinstance(data, dict) and "results" in data:
                        return data
                    raise MatchmakerError(
                        "Matchmaker answered but not with results (it may be asking for a "
                        "captcha). Run the search on https://reporter.nih.gov/matchmaker and "
                        f"copy the officers into program_officers.tsv instead. Reply: {resp.text[:200]}")
                if resp.status_code in (429,) or resp.status_code >= 500:
                    last = MatchmakerError(f"HTTP {resp.status_code}")
                else:
                    raise MatchmakerError(f"Matchmaker HTTP {resp.status_code}: {resp.text[:300]}")
            except (requests.ConnectionError, requests.Timeout) as err:
                last = err
            time.sleep(min(30, 2 ** attempt))
        raise MatchmakerError(f"Matchmaker request failed after {self.max_retries} attempts: {last}")

    @staticmethod
    def _payload(text: str, active_only: bool, limit: int, offset: int) -> dict[str, Any]:
        return {
            "criteria": {"title": "", "text": text, "include_active_projects": bool(active_only)},
            "facet_filters": {}, "limit": limit, "offset": offset,
            "sort_order": "", "sort_field": "",
        }

    def program_officers(self, text: str, active_only: bool = True) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            data = self._post("ProjectPO", self._payload(text, active_only, 100, offset))
            page = data.get("results") or []
            out.extend(page)
            total = (data.get("meta") or {}).get("total") or len(out)
            offset += len(page)
            if not page or offset >= total:
                return out

    def similar_projects(self, text: str, active_only: bool = True) -> list[dict[str, Any]]:
        """Matchmaker's (up to) 500 most similar projects.

        The service ignores offset paging, so ask for all 500 in one request.
        Note: despite include_active_projects, the list also contains projects
        that have ended (is_active "N"); current_officers() filters on the API.
        """
        data = self._post("Projects", self._payload(text, active_only, 500, 0))
        return data.get("results") or []


# ------------------------------------------------------------------ selection

def knee_count(counts: list[int]) -> int:
    """Count at the knee of a descending, long-tailed curve.

    Normalise rank (x) and count (y) to [0, 1]; the knee is the point furthest
    below the straight line from the top-ranked PO to the last one.
    """
    c = sorted(counts, reverse=True)
    if len(c) < 3 or c[0] == c[-1]:
        return c[-1] if c else 0
    n, hi, lo = len(c), c[0], c[-1]
    best_i, best_d = 0, -1.0
    for i, v in enumerate(c):
        x = i / (n - 1)
        y = (v - lo) / (hi - lo)
        d = (1 - x) - y
        if d > best_d:
            best_i, best_d = i, d
    return c[best_i]


def select_officers(counts: list[int], method: str = "knee", value: float | None = None,
                    min_projects: int = 2, max_officers: int | None = None) -> tuple[int, str]:
    """Return (threshold count, explanation). Officers with count >= threshold are kept."""
    if method not in METHODS:
        raise ValueError(f"Unknown selection method '{method}'. Use one of: {', '.join(METHODS)}")
    c = sorted((int(x) for x in counts), reverse=True)
    if not c:
        return 1, "no program officers returned"
    if method == "knee":
        t, why = knee_count(c), "knee of the ranked counts"
    elif method == "above_median":
        med = statistics.median(c)
        t, why = int(med) + 1, f"above the median ({med:g})"
    elif method == "min_projects":
        t, why = int(value or min_projects), f"at least {int(value or min_projects)} matched grants"
    elif method == "top_n":
        k = max(1, min(int(value or 10), len(c)))
        t, why = c[k - 1], f"top {k} (ties kept)"
    else:  # coverage
        frac = float(value or 0.5)
        total, run, t = sum(c), 0, c[0]
        for v in c:
            run += v
            t = v
            if run >= frac * total:
                break
        why = f"top officers covering {frac:.0%} of matched grants"
    if t < min_projects:
        t, why = min_projects, why + f", raised to the floor of {min_projects}"
    if max_officers and sum(1 for v in c if v >= t) > max_officers:
        # raise the threshold until the cap is met (ties kept together)
        t = c[max_officers - 1] + (1 if sum(1 for v in c if v >= c[max_officers - 1]) > max_officers else 0)
        why += f", capped at {max_officers} officers"
    return t, why


# ------------------------------------------------------------------ run + outputs

def _bar(n: int, scale: float) -> str:
    return "█" * max(1, round(n * scale))


def _display(full_name: str) -> str:
    """'PRICE, AMANDA JOY' -> 'Amanda Joy Price'; 'AMANDA JOY PRICE' -> 'Amanda Joy Price'."""
    name = " ".join((full_name or "").split())
    last, _, rest = name.partition(",")
    return (f"{rest.strip()} {last.strip()}" if rest else name).strip().title()


def _key(full_name: str) -> tuple[str, str]:
    first, last = parse_person_name(full_name or "")
    return first.lower(), last.lower()


def current_officers(core_nums: list[str], active_only: bool = True, api_client=None,
                     batch: int = 100) -> dict[str, dict[str, Any]]:
    """Look up each grant in the RePORTER API and return its *current* program
    officer(s) and institute: {core_project_num: {"officers": [...], "institute": ...}}.

    Matchmaker's own PO attribution can be out of date (portfolios get
    reassigned); the newest main-award record in the API is authoritative.
    """
    from .api import ReporterClient
    from .landscape import _primary

    api_client = api_client or ReporterClient()
    by_core: dict[str, list[dict[str, Any]]] = {}
    for i in range(0, len(core_nums), batch):
        crit: dict[str, Any] = {"project_nums": core_nums[i:i + batch]}
        if active_only:
            crit["include_active_projects"] = True
        for rec in api_client.search(crit)["results"]:
            by_core.setdefault(rec.get("core_project_num"), []).append(rec)
    out = {}
    for core, recs in by_core.items():
        p = _primary(recs)
        out[core] = {
            "officers": [_display(po.get("full_name") or "") for po in p.get("program_officers") or []],
            "institute": (p.get("agency_ic_admin") or {}).get("abbreviation"),
            "title": p.get("project_title"),
        }
    return out


def run_matchmaker(text: str, out_dir: str | Path, active_only: bool = True,
                   method: str = "knee", value: float | None = None, min_projects: int = 2,
                   max_officers: int | None = None, group: str = "",
                   count_source: str = "current",
                   client: MatchmakerClient | None = None, api_client=None) -> dict[str, Any]:
    """Query Matchmaker, count matched grants per officer, select officers, write outputs.

    count_source:
      "current"    (default) credit each similar project to the program officer on
                   its newest record in the RePORTER API - up to date, and only
                   counts grants that are still active when active_only is set.
      "matchmaker" use Matchmaker's own per-officer counts as shown on the website.

    Writes to out_dir:
      program_officers.tsv      selected officers (pipeline input format)
      program_officers_all.tsv  every officer, ranked, with both counts and selected flag
      similar_projects.tsv      Matchmaker's similar projects + their current officer(s)
      selection.md              human-readable ranking + cutoff, to sanity-check
      matchmaker.json           raw responses + settings + stats
    """
    if count_source not in ("current", "matchmaker"):
        raise ValueError("count_source must be 'current' or 'matchmaker'")
    text = " ".join(text.split())
    if not text:
        raise MatchmakerError("The Matchmaker text is empty.")
    if len(text) > MAX_TEXT_CHARS:
        raise MatchmakerError(f"Matchmaker accepts up to {MAX_TEXT_CHARS:,} characters; the text has "
                              f"{len(text):,}. Trim it (e.g. aims page without references).")
    client = client or MatchmakerClient()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    log.info("Matchmaker: %d characters of text, active_only=%s", len(text), active_only)
    pos = client.program_officers(text, active_only)
    projects = client.similar_projects(text, active_only)
    log.info("Matchmaker returned %d program officers and %d similar projects", len(pos), len(projects))

    # Officers as Matchmaker reports them. Matchmaker sometimes lists one person
    # twice (trailing space, with/without middle name): merge on first + last name.
    officers: dict[tuple[str, str], dict[str, Any]] = {}
    for p in pos:
        name = _display(p.get("full_name"))
        k = _key(name)
        if not k[1]:
            continue
        o = officers.setdefault(k, {"name": name, "institute": p.get("ic_abbr"), "email": None,
                                    "matchmaker_count": 0, "current_count": 0, "matched_grants": []})
        o["matchmaker_count"] += int(p.get("projects_counts") or 0)
        o["email"] = o["email"] or p.get("po_email") or p.get("email")
        if len(name) > len(o["name"]):
            o["name"] = name  # keep the fuller form, e.g. with middle name

    # Current officer of every similar project, from the official API
    cores = list(dict.fromkeys(p.get("core_project_num") for p in projects if p.get("core_project_num")))
    current = current_officers(cores, active_only, api_client) if cores else {}
    log.info("Resolved current officers for %d of %d similar grants", len(current), len(cores))
    for core, info in current.items():
        for name in info["officers"]:
            k = _key(name)
            o = officers.setdefault(k, {"name": name, "institute": info["institute"], "email": None,
                                        "matchmaker_count": 0, "current_count": 0, "matched_grants": []})
            o["current_count"] += 1
            o["matched_grants"].append(core)

    count_col = "current_count" if count_source == "current" else "matchmaker_count"
    rows = []
    for (first, last), o in officers.items():
        rows.append({"name": o["name"], "first_name": first.title(), "last_name": last.title(),
                     "current_count": o["current_count"], "matchmaker_count": o["matchmaker_count"],
                     "institute": o["institute"], "email": o["email"],
                     "matched_grants": "; ".join(o["matched_grants"])})
    cols = ["name", "first_name", "last_name", "current_count", "matchmaker_count",
            "institute", "email", "matched_grants"]
    df = pd.DataFrame(rows, columns=cols)
    df = df[df[count_col] > 0].sort_values([count_col, "name"], ascending=[False, True]).reset_index(drop=True)
    df.insert(0, "rank", range(1, len(df) + 1))

    counts = df[count_col].tolist()
    threshold, why = select_officers(counts, method, value, min_projects, max_officers)
    df["selected"] = df[count_col] >= threshold
    sel = df[df["selected"]]
    # Officers Matchmaker credits (2+ projects) who manage none of the matched grants now
    stale = [o["name"] for o in sorted(officers.values(), key=lambda o: -o["matchmaker_count"])
             if o["matchmaker_count"] >= 2 and o["current_count"] == 0] if count_source == "current" else []

    df.to_csv(out / "program_officers_all.tsv", sep="\t", index=False)
    pd.DataFrame({
        "first_name": sel["first_name"], "last_name": sel["last_name"],
        "label": sel["name"], "group": group,
        "matched_grants": sel[count_col], "institute": sel["institute"], "email": sel["email"],
    }).to_csv(out / "program_officers.tsv", sep="\t", index=False)

    proj_rows = [{
        "rank": i, "core_project_num": p.get("core_project_num"), "project_num": p.get("full_project_num"),
        "title": p.get("project_title"), "activity_code": p.get("activity_code"),
        "institute": p.get("admin_icd_long_abbr"), "organization": p.get("org_name"),
        "fiscal_year": p.get("fiscal_year"), "match_score": p.get("match_score"),
        "matchmaker_is_active": p.get("is_active"),
        "currently_active": p.get("core_project_num") in current,
        "current_program_officers": "; ".join((current.get(p.get("core_project_num")) or {}).get("officers", [])),
    } for i, p in enumerate(projects, 1)]
    pd.DataFrame(proj_rows).to_csv(out / "similar_projects.tsv", sep="\t", index=False)

    stats = {
        "count_source": count_source,
        "similar_projects": len(projects), "similar_grants_resolved": len(current),
        "n_officers": len(df), "n_selected": int(df["selected"].sum()),
        "matched_total": int(sum(counts)), "matched_selected": int(sel[count_col].sum()),
        "median": statistics.median(counts) if counts else None,
        "threshold": threshold, "reason": why, "stale_matchmaker_officers": stale,
    }
    settings = {"active_only": active_only, "method": method, "value": value, "min_projects": min_projects,
                "max_officers": max_officers, "group": group, "count_source": count_source}
    write_json({
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "text": text, "settings": settings, "stats": stats,
        "program_officers_raw": pos, "similar_projects_raw": projects,
    }, out / "matchmaker.json")

    what = ("current program officer (RePORTER API) of each of Matchmaker's similar grants"
            if count_source == "current" else "Matchmaker's own per-officer counts")
    lines = [
        "# Matchmaker program officer selection", "",
        f"- **Similar projects from Matchmaker:** {len(projects)}"
        + (f" ({len(current)} {'currently active ' if active_only else ''}grants found in the API)"
           if count_source == "current" else ""),
        f"- **Counted by:** {what}",
        f"- **Officers:** {stats['n_officers']} managing {stats['matched_total']} matched grants "
        f"(median {stats['median']} each)",
        f"- **Rule:** `{method}` → keep officers with **≥ {threshold}** matched grants ({why})",
        f"- **Selected:** {stats['n_selected']} officers, covering {stats['matched_selected']} matched "
        f"grants ({stats['matched_selected'] / max(1, stats['matched_total']):.0%})",
    ]
    if stale:
        shown = ", ".join(stale[:15]) + (f" and {len(stale) - 15} more" if len(stale) > 15 else "")
        lines.append(f"- **Not counted:** {len(stale)} officers Matchmaker credits with 2+ similar projects "
                     f"manage none of them as {'active ' if active_only else ''}grants today (the grants "
                     f"ended, or were reassigned to another officer): {shown}.")
    lines += ["", "Change `matchmaker.selection` in the config (or the flags of `nih-reporter matchmaker`) "
              "and rerun to move the cutoff.", "", "## Count distribution", "", "```"]
    if counts:
        widest = max(counts.count(x) for x in set(counts))
        for v in sorted(set(counts), reverse=True):
            n = counts.count(v)
            mark = "  ← cutoff" if v == threshold else ""
            lines.append(f"{v:>4} grant{'s' if v != 1 else ' '} | {n:>3} officer{'s' if n != 1 else ' '} "
                         f"{_bar(n, 30 / widest)}{mark}")
    lines += ["```", "", "## Ranked officers", "",
              "| Rank | Program officer | Institute | Matched grants (current) | Matchmaker count | Selected |",
              "|---:|---|---|---|---:|:---:|"]
    scale = 30 / max(counts) if counts else 1
    for r in df.itertuples():
        n = getattr(r, count_col)
        lines.append(f"| {r.rank} | {r.name} | {r.institute or ''} | `{_bar(n, scale)}` {r.current_count} | "
                     f"{r.matchmaker_count} | {'✓' if r.selected else ''} |")
    (out / "selection.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("Selected %d of %d officers (>= %d matched grants: %s)",
             stats["n_selected"], stats["n_officers"], threshold, why)
    return stats
