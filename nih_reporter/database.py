"""Merge per-query results into a portfolio database.

Outputs (all under ``<output_dir>/database``):
  records.tsv        one row per RePORTER record (appl_id) - every fiscal year,
                     supplement and subproject that matched any query
  portfolio.tsv      one row per grant (core_project_num), collapsed from records
  query_hits.tsv     which query matched which grant (many-to-many)
  portfolio.jsonl    one JSON object per grant with the text fields (title,
                     abstract, public health relevance, terms) - the input for
                     downstream analysis
  nih_reporter.sqlite  all of the above plus long PI / PO tables
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .normalize import people_rows, records_to_frame
from .pipeline import read_json

log = logging.getLogger(__name__)

PORTFOLIO_COLUMNS = [
    "core_project_num", "latest_project_num", "project_title", "activity_code", "ic_admin",
    "contact_pi", "principal_investigators", "program_officers", "org_name", "org_state",
    "org_country", "opportunity_numbers", "project_start_date", "project_end_date",
    "latest_fiscal_year", "fiscal_years", "award_amount_latest_fy", "n_records", "is_active",
    "is_new", "study_section", "matched_queries", "matched_groups", "project_detail_url",
    "pref_terms", "abstract_text", "phr_text",
]
QUERY_COLUMNS = ["query_id", "query_type", "value", "label", "group", "criteria",
                 "retrieved_at", "n_records", "reporter_url"]
HIT_COLUMNS = ["query_id", "appl_id", "core_project_num"]
PI_COLUMNS = ["appl_id", "core_project_num", "profile_id", "name", "first_name", "last_name",
              "is_contact_pi", "title"]
PO_COLUMNS = ["appl_id", "core_project_num", "name", "first_name", "last_name"]


def _join_unique(values: Iterable[Any], sep: str = "; ") -> str | None:
    seen: list[str] = []
    for v in values:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        for part in str(v).split(sep):
            part = part.strip()
            if part and part not in seen:
                seen.append(part)
    return sep.join(seen) or None


def load_raw(raw_paths: Iterable[str | Path]):
    queries, records, hits, pis, pos = [], {}, [], [], []
    for path in raw_paths:
        raw = read_json(path)
        q = raw["query"]
        queries.append({
            "query_id": q["query_id"], "query_type": q["query_type"], "value": q["value"],
            "label": q["label"], "group": q["group"], "criteria": json.dumps(q["criteria"]),
            "retrieved_at": raw.get("retrieved_at"), "n_records": len(raw["results"]),
            "reporter_url": raw.get("reporter_url"),
        })
        for rec in raw["results"]:
            appl_id = rec.get("appl_id")
            hits.append({"query_id": q["query_id"], "appl_id": appl_id,
                         "core_project_num": rec.get("core_project_num")})
            if appl_id not in records:
                records[appl_id] = rec
                p_i, p_o = people_rows(rec)
                pis.extend(p_i)
                pos.extend(p_o)
    return (pd.DataFrame(queries, columns=QUERY_COLUMNS), list(records.values()),
            pd.DataFrame(hits, columns=HIT_COLUMNS), pd.DataFrame(pis, columns=PI_COLUMNS),
            pd.DataFrame(pos, columns=PO_COLUMNS))


def collapse_portfolio(records: pd.DataFrame, hits: pd.DataFrame, queries: pd.DataFrame) -> pd.DataFrame:
    """One row per core project number."""
    if records.empty:
        return pd.DataFrame(columns=PORTFOLIO_COLUMNS)
    df = records.copy()
    df["_is_parent"] = df["subproject_id"].isna()
    df["_is_main"] = ~df["award_type"].astype(str).str.startswith("3")  # 3 = supplement
    df = df.sort_values(
        ["core_project_num", "_is_parent", "_is_main", "fiscal_year", "award_notice_date", "appl_id"],
        ascending=[True, False, False, False, False, False], na_position="last",
    )
    primary = df.groupby("core_project_num", sort=False).head(1).set_index("core_project_num")

    # Latest fiscal year of the main award (a newer supplement-only year doesn't count).
    latest_fy = df[df["_is_parent"] & df["_is_main"]].groupby("core_project_num")["fiscal_year"].max()
    latest_fy = latest_fy.reindex(df["core_project_num"].unique()).fillna(
        df.groupby("core_project_num")["fiscal_year"].max())
    parent_latest = df[df["_is_parent"] & (df["fiscal_year"] == df["core_project_num"].map(latest_fy))]
    award_latest = parent_latest.groupby("core_project_num")["award_amount"].sum(min_count=1)

    grouped = df.groupby("core_project_num")
    agg = pd.DataFrame({
        "fiscal_years": grouped["fiscal_year"].apply(lambda s: ";".join(str(int(x)) for x in sorted(s.dropna().unique()))),
        "n_records": grouped.size(),
        "program_officers_all": grouped["program_officers"].apply(_join_unique),
        "opportunity_numbers": grouped["opportunity_number"].apply(_join_unique),
        "project_end_date_max": grouped["project_end_date"].max(),
        "abstract_any": grouped["abstract_text"].apply(lambda s: next((x for x in s if isinstance(x, str) and x), None)),
    })
    groups = queries.set_index("query_id")["group"].to_dict() if not queries.empty else {}
    hit_agg = hits.drop_duplicates(["query_id", "core_project_num"]).groupby("core_project_num")["query_id"]
    agg["matched_queries"] = hit_agg.apply(lambda s: "; ".join(sorted(s)))
    agg["matched_groups"] = hit_agg.apply(lambda s: _join_unique(groups.get(q) for q in sorted(s)))
    agg["award_amount_latest_fy"] = award_latest

    keep = ["project_num", "project_title", "activity_code", "ic_admin", "contact_pi",
            "principal_investigators", "org_name", "org_state", "org_country",
            "project_start_date", "fiscal_year", "is_active", "is_new", "study_section",
            "project_detail_url", "pref_terms", "abstract_text", "phr_text"]
    out = primary[keep].join(agg)
    out["abstract_text"] = out["abstract_text"].fillna(out.pop("abstract_any"))
    out = out.rename(columns={
        "project_num": "latest_project_num", "fiscal_year": "latest_fiscal_year",
        "program_officers_all": "program_officers", "project_end_date_max": "project_end_date",
    })
    out = out[PORTFOLIO_COLUMNS[1:]].reset_index().sort_values(["matched_queries", "core_project_num"])
    return out


def portfolio_jsonl_records(portfolio: pd.DataFrame) -> list[dict[str, Any]]:
    def split(v):
        return [x.strip() for x in str(v).split(";") if x.strip()] if isinstance(v, str) else []

    out = []
    for row in portfolio.to_dict(orient="records"):
        out.append({
            "core_project_num": row["core_project_num"],
            "title": row["project_title"],
            "abstract": row["abstract_text"],
            "public_health_relevance": row["phr_text"],
            "terms": split(row["pref_terms"]),
            "activity_code": row["activity_code"],
            "institute": row["ic_admin"],
            "contact_pi": row["contact_pi"],
            "principal_investigators": split(row["principal_investigators"]),
            "program_officers": split(row["program_officers"]),
            "organization": row["org_name"],
            "opportunity_numbers": split(row["opportunity_numbers"]),
            "project_start_date": row["project_start_date"],
            "project_end_date": row["project_end_date"],
            "award_amount_latest_fy": None if pd.isna(row["award_amount_latest_fy"]) else float(row["award_amount_latest_fy"]),
            "matched_queries": split(row["matched_queries"]),
            "matched_groups": split(row["matched_groups"]),
            "url": row["project_detail_url"],
        })
    return out


def build_database(raw_paths: Iterable[str | Path], out_dir: str | Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    queries, raw_records, hits, pis, pos = load_raw(raw_paths)
    records = records_to_frame(raw_records)
    portfolio = collapse_portfolio(records, hits, queries)

    paths = {
        "queries": out_dir / "queries.tsv",
        "records": out_dir / "records.tsv",
        "portfolio": out_dir / "portfolio.tsv",
        "query_hits": out_dir / "query_hits.tsv",
        "jsonl": out_dir / "portfolio.jsonl",
        "sqlite": out_dir / "nih_reporter.sqlite",
    }
    queries.to_csv(paths["queries"], sep="\t", index=False)
    records.to_csv(paths["records"], sep="\t", index=False)
    portfolio.to_csv(paths["portfolio"], sep="\t", index=False)
    hits.to_csv(paths["query_hits"], sep="\t", index=False)
    with paths["jsonl"].open("w", encoding="utf-8") as fh:
        for rec in portfolio_jsonl_records(portfolio):
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if paths["sqlite"].exists():
        paths["sqlite"].unlink()
    with sqlite3.connect(paths["sqlite"]) as con:
        queries.to_sql("queries", con, index=False)
        records.to_sql("records", con, index=False)
        portfolio.to_sql("portfolio", con, index=False)
        hits.to_sql("query_hits", con, index=False)
        pis.to_sql("principal_investigators", con, index=False)
        pos.to_sql("program_officers", con, index=False)
        con.execute("CREATE INDEX idx_hits_query ON query_hits(query_id)")
        con.execute("CREATE INDEX idx_po_name ON program_officers(last_name, first_name)")

    log.info("Database: %d queries, %d records, %d grants", len(queries), len(records), len(portfolio))
    return paths
