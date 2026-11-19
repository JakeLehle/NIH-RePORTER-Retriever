"""Human-readable outputs: program officer summary table and per-query
portfolio documents (Markdown)."""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

import pandas as pd

# Terms so generic in RePORTER's thesaurus that they say nothing about topic.
GENERIC_TERMS = {
    "research", "goals", "data", "testing", "role", "development", "methods",
    "outcome", "public health", "research personnel", "funding", "investigators",
    "communities", "science", "complex", "affect", "phase", "sampling", "training",
    "united states national institutes of health", "work", "time", "measures",
    "address", "base", "process", "structure", "knowledge", "disease",
    "insight", "measurement", "technology", "system", "molecular", "output",
    "flexibility", "calibration", "protocols documentation", "data collection",
    "experimental designs", "base pairing", "data set", "detection", "maps",
    "biology", "human", "modeling", "therapeutic", "future", "resources",
}


def _split(v) -> list[str]:
    return [x.strip() for x in str(v).split(";") if x.strip()] if isinstance(v, str) else []


def _counts(series: pd.Series, top: int | None = None) -> str:
    c = Counter(x for v in series for x in _split(v))
    return "; ".join(f"{k} ({n})" for k, n in c.most_common(top))


def _money(x) -> str:
    return "n/a" if x is None or pd.isna(x) else f"${x:,.0f}"


def distinctive_terms(portfolio: pd.DataFrame, subset_idx, top: int = 15) -> str:
    """Terms frequent in this subset relative to everything retrieved (tf-idf-like)."""
    n_all = len(portfolio)
    df_all = Counter(t.lower() for v in portfolio["pref_terms"] for t in set(_split(v)))
    sub = Counter(t.lower() for v in portfolio.loc[subset_idx, "pref_terms"] for t in set(_split(v)))
    scored = {
        t: n * math.log((n_all + 1) / (df_all[t] + 0.5))
        for t, n in sub.items() if t not in GENERIC_TERMS and n >= 2
    }
    best = sorted(scored, key=scored.get, reverse=True)[:top]
    return "; ".join(f"{t} ({sub[t]})" for t in best)


def program_officer_summary(portfolio: pd.DataFrame, queries: pd.DataFrame) -> pd.DataFrame:
    """One row per program officer appearing on any retrieved grant."""
    cols = ["program_officer", "queried_directly", "n_grants", "n_new_this_year",
            "total_award_latest_fy", "institutes", "activity_codes", "opportunities",
            "top_organizations", "matched_queries", "distinctive_terms"]
    if portfolio.empty:
        return pd.DataFrame(columns=cols)
    pf = portfolio.reset_index(drop=True)
    exploded = pf.assign(program_officer=pf["program_officers"].map(_split)).explode("program_officer")
    exploded = exploded.dropna(subset=["program_officer"])
    queried = set()
    if not queries.empty:
        queried = {v.lower() for v in queries.loc[queries["query_type"] == "program_officer", "value"]}

    rows = []
    for po, grp in exploded.groupby("program_officer"):
        parts = po.lower().split()
        rows.append({
            "program_officer": po,
            "queried_directly": any(
                q.split()[-1] == parts[-1] and q.split()[0] == parts[0] for q in queried
            ) if parts else False,
            "n_grants": grp["core_project_num"].nunique(),
            "n_new_this_year": int(grp["is_new"].fillna(False).astype(bool).sum()),
            "total_award_latest_fy": grp["award_amount_latest_fy"].sum(min_count=1),
            "institutes": _counts(grp["ic_admin"]),
            "activity_codes": _counts(grp["activity_code"]),
            "opportunities": _counts(grp["opportunity_numbers"], top=10),
            "top_organizations": _counts(grp["org_name"], top=5),
            "matched_queries": _counts(grp["matched_queries"]),
            "distinctive_terms": distinctive_terms(pf, grp.index),
        })
    return pd.DataFrame(rows, columns=cols).sort_values(["queried_directly", "n_grants"], ascending=False)


def query_markdown(query: pd.Series, grants: pd.DataFrame, portfolio_all: pd.DataFrame) -> str:
    lines = [
        f"# Portfolio: {query['label']}",
        "",
        f"- **Query type:** {query['query_type']}",
        f"- **Searched for:** {query['value']}",
    ]
    if isinstance(query.get("group"), str) and query["group"]:
        lines.append(f"- **Group:** {query['group']}")
    lines += [
        f"- **Retrieved:** {query['retrieved_at']}",
        f"- **RePORTER records:** {query['n_records']} → **unique grants:** {len(grants)}",
        f"- **Total award (latest FY, parent awards):** {_money(grants['award_amount_latest_fy'].sum(min_count=1))}",
    ]
    if isinstance(query.get("reporter_url"), str):
        lines.append(f"- **View on RePORTER:** {query['reporter_url']}")
    if grants.empty:
        return "\n".join(lines + ["", "_No grants matched this query._", ""])

    lines += [
        "",
        "## At a glance",
        "",
        f"- **Activity codes:** {_counts(grants['activity_code'])}",
        f"- **Institutes:** {_counts(grants['ic_admin'])}",
        f"- **Program officers:** {_counts(grants['program_officers'])}",
        f"- **Funding opportunities:** {_counts(grants['opportunity_numbers'], top=10)}",
        f"- **Distinctive terms:** {distinctive_terms(portfolio_all, grants.index)}",
        "",
        "## Grants",
        "",
        "| Grant | Title | Contact PI | Organization | Ends | Latest FY award |",
        "|---|---|---|---|---|---|",
    ]
    grants = grants.sort_values(["activity_code", "core_project_num"])
    for g in grants.itertuples():
        title = str(g.project_title).replace("|", "/")
        lines.append(
            f"| [{g.core_project_num}](#{g.core_project_num.lower()}) | {title} | {g.contact_pi} | "
            f"{g.org_name} | {g.project_end_date} | {_money(g.award_amount_latest_fy)} |"
        )
    lines += ["", "## Abstracts", ""]
    for g in grants.itertuples():
        lines += [
            f"### {g.core_project_num}",
            "",
            f"**{g.project_title}**  ",
            f"{g.activity_code} · {g.ic_admin} · PI: {g.principal_investigators} · "
            f"{g.org_name} · {g.project_start_date} → {g.project_end_date} · "
            f"PO: {g.program_officers} · [RePORTER]({g.project_detail_url})",
            "",
            str(g.abstract_text) if isinstance(g.abstract_text, str) else "_No abstract available._",
            "",
        ]
        if isinstance(g.phr_text, str):
            lines += [f"*Public health relevance:* {g.phr_text}", ""]
    return "\n".join(lines)


def write_reports(db_dir: str | Path, reports_dir: str | Path) -> None:
    db_dir, reports_dir = Path(db_dir), Path(reports_dir)
    (reports_dir / "portfolios").mkdir(parents=True, exist_ok=True)
    portfolio = pd.read_csv(db_dir / "portfolio.tsv", sep="\t")
    queries = pd.read_csv(db_dir / "queries.tsv", sep="\t", keep_default_na=False)

    program_officer_summary(portfolio, queries).to_csv(
        reports_dir / "program_officer_summary.tsv", sep="\t", index=False)

    for q in queries.to_dict(orient="records"):
        mask = portfolio["matched_queries"].map(lambda v: q["query_id"] in _split(v)).astype(bool)
        md = query_markdown(pd.Series(q), portfolio[mask], portfolio)
        (reports_dir / "portfolios" / f"{q['query_id']}.md").write_text(md, encoding="utf-8")
