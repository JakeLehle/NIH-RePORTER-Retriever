"""Flatten raw RePORTER project records into tidy, one-row-per-record tables."""

from __future__ import annotations

from typing import Any

import pandas as pd

# Column order for the flattened projects table.
PROJECT_COLUMNS = [
    "appl_id", "project_num", "core_project_num", "subproject_id", "fiscal_year",
    "project_title", "activity_code", "award_type", "funding_mechanism",
    "ic_admin", "ic_admin_name", "opportunity_number",
    "contact_pi", "principal_investigators", "program_officers",
    "org_name", "org_city", "org_state", "org_country", "org_dept_type",
    "project_start_date", "project_end_date", "budget_start", "budget_end",
    "award_notice_date", "award_amount", "direct_cost_amt", "indirect_cost_amt",
    "is_active", "is_new", "study_section", "spending_categories",
    "project_detail_url", "pref_terms", "abstract_text", "phr_text",
]


def _date(value: Any) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def _name(person: dict[str, Any]) -> str:
    full = person.get("full_name")
    if full:
        return " ".join(full.split()).title()
    parts = [person.get("first_name"), person.get("middle_name"), person.get("last_name")]
    return " ".join(p for p in parts if p).title()


def _clean_text(text: Any) -> str | None:
    if not text:
        return None
    return " ".join(str(text).split())


def flatten_project(rec: dict[str, Any]) -> dict[str, Any]:
    org = rec.get("organization") or {}
    ic = rec.get("agency_ic_admin") or {}
    pis = rec.get("principal_investigators") or []
    pos = rec.get("program_officers") or []
    contact = next((p for p in pis if p.get("is_contact_pi")), pis[0] if pis else None)
    study = rec.get("full_study_section") or {}
    return {
        "appl_id": rec.get("appl_id"),
        "project_num": rec.get("project_num"),
        "core_project_num": rec.get("core_project_num"),
        "subproject_id": rec.get("subproject_id"),
        "fiscal_year": rec.get("fiscal_year"),
        "project_title": _clean_text(rec.get("project_title")),
        "activity_code": rec.get("activity_code"),
        "award_type": rec.get("award_type"),
        "funding_mechanism": rec.get("funding_mechanism"),
        "ic_admin": ic.get("abbreviation"),
        "ic_admin_name": ic.get("name"),
        "opportunity_number": rec.get("opportunity_number"),
        "contact_pi": _name(contact) if contact else None,
        "principal_investigators": "; ".join(_name(p) for p in pis) or None,
        "program_officers": "; ".join(_name(p) for p in pos) or None,
        "org_name": org.get("org_name"),
        "org_city": org.get("org_city"),
        "org_state": org.get("org_state"),
        "org_country": org.get("org_country"),
        "org_dept_type": org.get("dept_type"),
        "project_start_date": _date(rec.get("project_start_date")),
        "project_end_date": _date(rec.get("project_end_date")),
        "budget_start": _date(rec.get("budget_start")),
        "budget_end": _date(rec.get("budget_end")),
        "award_notice_date": _date(rec.get("award_notice_date")),
        "award_amount": rec.get("award_amount"),
        "direct_cost_amt": rec.get("direct_cost_amt"),
        "indirect_cost_amt": rec.get("indirect_cost_amt"),
        "is_active": rec.get("is_active"),
        "is_new": rec.get("is_new"),
        "study_section": study.get("name") or study.get("srg_code"),
        "spending_categories": rec.get("spending_categories_desc"),
        "project_detail_url": rec.get("project_detail_url"),
        "pref_terms": rec.get("pref_terms"),
        "abstract_text": _clean_text(rec.get("abstract_text")),
        "phr_text": _clean_text(rec.get("phr_text")),
    }


def people_rows(rec: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Long-format PI and PO rows for one record (for the SQLite database)."""
    appl_id = rec.get("appl_id")
    core = rec.get("core_project_num")
    pis = [{
        "appl_id": appl_id, "core_project_num": core,
        "profile_id": p.get("profile_id"), "name": _name(p),
        "first_name": p.get("first_name"), "last_name": p.get("last_name"),
        "is_contact_pi": bool(p.get("is_contact_pi")), "title": p.get("title"),
    } for p in rec.get("principal_investigators") or []]
    pos = [{
        "appl_id": appl_id, "core_project_num": core, "name": _name(p),
        "first_name": (p.get("first_name") or "").title(),
        "last_name": (p.get("last_name") or "").title(),
    } for p in rec.get("program_officers") or []]
    return pis, pos


def records_to_frame(records: list[dict[str, Any]]) -> pd.DataFrame:
    rows = [flatten_project(r) for r in records]
    return pd.DataFrame(rows, columns=PROJECT_COLUMNS)
