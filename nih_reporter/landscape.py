"""Per-search-term grant files: one human-readable JSON file per query.

Built directly from the raw RePORTER records (no summarisation or AI step).
Each file holds the search that was run, a factual overview (counts and
dollars), and one entry per grant with every useful field from every
fiscal-year / supplement / subproject record collapsed under it.

These files are the hand-off to Phase 2. ``check_grant_files`` checks
they exist and are complete, and writes a manifest that the next rule takes as
its input.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .normalize import _clean_text, _date, _name
from .pipeline import read_json, write_json

SCHEMA_VERSION = 1
REQUIRED_GRANT_KEYS = [
    "core_project_num", "title", "activity_code", "institute", "program_officers",
    "principal_investigators", "organization", "dates", "funding", "abstract",
    "public_health_relevance", "terms", "records", "reporter_url",
]


def _split(text: Any) -> list[str]:
    return [t.strip() for t in str(text).split(";") if t.strip()] if text else []


def _unique(values: Iterable[Any]) -> list[Any]:
    out = []
    for v in values:
        if v not in (None, "", []) and v not in out:
            out.append(v)
    return out


def _primary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Newest parent (non-subproject) record; same rule as database.collapse_portfolio."""
    return sorted(
        records,
        key=lambda r: (r.get("subproject_id") is None, r.get("fiscal_year") or 0,
                       r.get("award_notice_date") or "", r.get("appl_id") or 0),
        reverse=True,
    )[0]


def grant_entry(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse all RePORTER records of one grant (core project number) into one entry."""
    p = _primary(records)
    org = p.get("organization") or {}
    ic = p.get("agency_ic_admin") or {}
    study = p.get("full_study_section") or {}

    pis: dict[str, dict[str, Any]] = {}
    for r in sorted(records, key=lambda r: r.get("fiscal_year") or 0):
        for person in r.get("principal_investigators") or []:
            pis[_name(person)] = {
                "name": _name(person),
                "is_contact_pi": bool(person.get("is_contact_pi")),
                "title": person.get("title"),
                "profile_id": person.get("profile_id"),
            }

    parents = [r for r in records if r.get("subproject_id") is None]
    by_fy: dict[int, float] = defaultdict(float)
    for r in parents:
        if r.get("fiscal_year") and r.get("award_amount") is not None:
            by_fy[int(r["fiscal_year"])] += r["award_amount"]
    latest_fy = max(by_fy) if by_fy else p.get("fiscal_year")
    latest_parents = [r for r in parents if r.get("fiscal_year") == latest_fy]

    abstract = _clean_text(p.get("abstract_text")) or next(
        (_clean_text(r.get("abstract_text")) for r in records if r.get("abstract_text")), None)
    phr = _clean_text(p.get("phr_text")) or next(
        (_clean_text(r.get("phr_text")) for r in records if r.get("phr_text")), None)

    return {
        "core_project_num": p.get("core_project_num"),
        "latest_project_num": p.get("project_num"),
        "title": _clean_text(p.get("project_title")),
        "activity_code": p.get("activity_code"),
        "funding_mechanism": p.get("funding_mechanism"),
        "institute": {"abbreviation": ic.get("abbreviation"), "name": ic.get("name")},
        "opportunity_numbers": _unique(r.get("opportunity_number") for r in records),
        "program_officers": _unique(_name(po) for r in records for po in r.get("program_officers") or []),
        "principal_investigators": sorted(pis.values(), key=lambda x: not x["is_contact_pi"]),
        "organization": {
            "name": org.get("org_name"),
            "department_type": org.get("dept_type"),
            "city": org.get("org_city"),
            "state": org.get("org_state"),
            "country": org.get("org_country"),
        },
        "dates": {
            "project_start": _date(p.get("project_start_date")),
            "project_end": max((_date(r.get("project_end_date")) or "" for r in records), default=None) or None,
            "latest_budget_start": _date(p.get("budget_start")),
            "latest_budget_end": _date(p.get("budget_end")),
            "latest_award_notice": _date(p.get("award_notice_date")),
        },
        "funding": {
            "latest_fiscal_year": latest_fy,
            "award_amount_latest_fy": by_fy.get(latest_fy) if by_fy else None,
            "direct_cost_latest_fy": sum(r.get("direct_cost_amt") or 0 for r in latest_parents) or None,
            "indirect_cost_latest_fy": sum(r.get("indirect_cost_amt") or 0 for r in latest_parents) or None,
            "award_amount_by_fiscal_year": {str(k): v for k, v in sorted(by_fy.items())},
        },
        "is_active": any(r.get("is_active") for r in records),
        "is_new": bool(p.get("is_new")),
        "study_section": study.get("name") or study.get("srg_code"),
        "spending_categories": _split(p.get("spending_categories_desc")),
        "terms": _split(p.get("pref_terms")),
        "abstract": abstract,
        "public_health_relevance": phr,
        "reporter_url": p.get("project_detail_url"),
        "records": [
            {
                "appl_id": r.get("appl_id"),
                "project_num": r.get("project_num"),
                "fiscal_year": r.get("fiscal_year"),
                "subproject_id": r.get("subproject_id"),
                "award_type": r.get("award_type"),
                "award_amount": r.get("award_amount"),
                "title": _clean_text(r.get("project_title")) if r.get("subproject_id") else None,
            }
            for r in sorted(records, key=lambda r: (r.get("fiscal_year") or 0, r.get("appl_id") or 0))
        ],
    }


def _counts(values: Iterable[Any]) -> dict[str, int]:
    return dict(Counter(v for v in values if v).most_common())


def build_grant_file(raw_path: str | Path, out_path: str | Path) -> dict[str, Any]:
    raw = read_json(raw_path)
    by_core: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in raw["results"]:
        by_core[rec.get("core_project_num") or f"appl_{rec.get('appl_id')}"].append(rec)
    grants = sorted((grant_entry(recs) for recs in by_core.values()),
                    key=lambda g: (g["activity_code"] or "", g["core_project_num"] or ""))

    awards = [g["funding"]["award_amount_latest_fy"] for g in grants
              if g["funding"]["award_amount_latest_fy"] is not None]
    q = raw["query"]
    doc = {
        "schema_version": SCHEMA_VERSION,
        "search": {
            "query_id": q["query_id"],
            "query_type": q["query_type"],
            "searched_for": q["value"],
            "label": q["label"],
            "group": q["group"] or None,
            "criteria": q["criteria"],
            "retrieved_at": raw.get("retrieved_at"),
            "reporter_url": raw.get("reporter_url"),
            "api_records": len(raw["results"]),
            "api_total_reported": raw.get("total"),
        },
        "overview": {
            "n_grants": len(grants),
            "total_award_latest_fy": sum(awards) if awards else 0,
            "activity_codes": _counts(g["activity_code"] for g in grants),
            "institutes": _counts(g["institute"]["abbreviation"] for g in grants),
            "program_officers": _counts(po for g in grants for po in g["program_officers"]),
            "funding_opportunities": _counts(o for g in grants for o in g["opportunity_numbers"]),
            "organizations": _counts(g["organization"]["name"] for g in grants),
            "project_end_years": dict(sorted(_counts(
                (g["dates"]["project_end"] or "")[:4] for g in grants).items())),
            "spending_categories": _counts(c for g in grants for c in g["spending_categories"]),
            "grants_missing_abstract": sum(1 for g in grants if not g["abstract"]),
        },
        "grants": grants,
    }
    write_json(doc, out_path)
    return doc


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_grant_file(path: str | Path, raw_path: str | Path | None = None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one grant file."""
    errors, warnings = [], []
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return [f"{path}: missing or empty"], []
    try:
        doc = read_json(path)
    except json.JSONDecodeError as err:
        return [f"{path}: not valid JSON ({err})"], []

    for key in ("schema_version", "search", "overview", "grants"):
        if key not in doc:
            errors.append(f"{path.name}: missing top-level key '{key}'")
    if errors:
        return errors, warnings

    grants = doc["grants"]
    if doc["overview"].get("n_grants") != len(grants):
        errors.append(f"{path.name}: overview.n_grants={doc['overview'].get('n_grants')} "
                      f"but file holds {len(grants)} grants")
    n_records = sum(len(g.get("records", [])) for g in grants)
    if n_records != doc["search"].get("api_records"):
        errors.append(f"{path.name}: grants hold {n_records} records but the API returned "
                      f"{doc['search'].get('api_records')}")
    if raw_path is not None:
        raw_n = len(read_json(raw_path)["results"])
        if raw_n != doc["search"].get("api_records"):
            errors.append(f"{path.name}: raw file has {raw_n} records, grant file says "
                          f"{doc['search'].get('api_records')} (stale? rerun the workflow)")
    total = doc["search"].get("api_total_reported")
    if total is not None and total != doc["search"].get("api_records"):
        warnings.append(f"{path.name}: API reported {total} matches but {doc['search'].get('api_records')} were retrieved")

    seen = set()
    for i, g in enumerate(grants):
        missing = [k for k in REQUIRED_GRANT_KEYS if k not in g]
        if missing:
            errors.append(f"{path.name}: grant #{i} missing keys {missing}")
            continue
        if not g["core_project_num"] or not g["title"]:
            errors.append(f"{path.name}: grant #{i} has no project number or title")
        if g["core_project_num"] in seen:
            errors.append(f"{path.name}: duplicate grant {g['core_project_num']}")
        seen.add(g["core_project_num"])
        if not g["abstract"]:
            warnings.append(f"{path.name}: {g['core_project_num']} has no abstract")
    if not grants:
        warnings.append(f"{path.name}: search matched no grants")
    return errors, warnings


def check_grant_files(grant_paths: Iterable[str | Path], raw_paths: Iterable[str | Path] | None,
                      manifest_path: str | Path) -> dict[str, Any]:
    """Check every grant file; write a manifest if all pass, raise otherwise."""
    grant_paths = [Path(p) for p in grant_paths]
    raw_paths = [Path(p) for p in raw_paths] if raw_paths else [None] * len(grant_paths)
    all_errors, entries = [], []
    for gp, rp in zip(grant_paths, raw_paths):
        errors, warnings = check_grant_file(gp, rp)
        all_errors.extend(errors)
        entry: dict[str, Any] = {"file": str(gp), "errors": errors, "warnings": warnings}
        if not errors:
            doc = read_json(gp)
            entry.update({
                "query_id": doc["search"]["query_id"],
                "query_type": doc["search"]["query_type"],
                "searched_for": doc["search"]["searched_for"],
                "group": doc["search"]["group"],
                "retrieved_at": doc["search"]["retrieved_at"],
                "n_grants": doc["overview"]["n_grants"],
                "total_award_latest_fy": doc["overview"]["total_award_latest_fy"],
                "sha256": _sha256(gp),
            })
        entries.append(entry)
    if all_errors:
        raise ValueError("Grant file check failed:\n  " + "\n  ".join(all_errors))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "n_files": len(entries),
        "n_grants_total": sum(e["n_grants"] for e in entries),
        "files": entries,
    }
    write_json(manifest, manifest_path)
    return manifest
