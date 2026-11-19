"""Turn user-supplied input tables + config filters into RePORTER search specs.

Three kinds of input table are supported (all optional, at least one needed).
Every table may be TSV or CSV, lines starting with ``#`` are ignored, and
column names are case-insensitive. Every table may also carry optional
``label`` (a human readable name) and ``group`` (e.g. a consortium name; all
queries sharing a group are also rolled up into one combined portfolio)
columns.

program_officers   columns: first_name, last_name
                   or a single ``name`` column: "First M Last" or "Last, First M"
                   (the format RePORTER / Matchmaker display)
opportunities      columns: opportunity_number      (e.g. RFA-HG-21-003, PAR-23-145)
text_searches      columns: search_text [, search_field, operator]
                   for consortium names / acronyms / topics, e.g. "IGVF"
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_TEXT_FIELDS = "projecttitle,terms,abstracttext"

# Criteria keys that define *what* a query searches for. Everything else in a
# spec's criteria is a global filter (active only, fiscal years, ...), which is
# reused when looking up the complete record history of each grant found.
QUERY_KEYS = {"po_names", "foa", "advanced_text_search", "project_nums"}


@dataclass
class QuerySpec:
    query_id: str
    query_type: str  # program_officer | opportunity | text
    value: str  # the thing searched for, human readable
    label: str
    group: str
    criteria: dict[str, Any] = field(default_factory=dict)
    # After the search, also fetch every record of each grant found (by core
    # project number), so renewals / supplements / continuations filed under a
    # different opportunity number are included.
    complete_grants: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QuerySpec":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def filter_criteria(self) -> dict[str, Any]:
        return {k: v for k, v in self.criteria.items() if k not in QUERY_KEYS}

    def fingerprint(self) -> str:
        return hashlib.sha1(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()[:12]


def slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text.strip()).strip("_").lower()
    return slug or "query"


def group_id(group: str) -> str:
    return f"group_{slugify(group)}"


def read_table(path: str | Path) -> list[dict[str, str]]:
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return []
    delimiter = "," if path.suffix.lower() == ".csv" else "\t"
    reader = csv.DictReader(lines, delimiter=delimiter)
    rows = []
    for row in reader:
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in row.items()})
    return rows


def global_criteria(filters: dict[str, Any] | None) -> dict[str, Any]:
    """Criteria applied to every query, from the ``filters`` config block."""
    filters = filters or {}
    crit: dict[str, Any] = {}
    if filters.get("active_only", True):
        # With no fiscal_years, include_active_projects=True restricts the
        # search to currently active projects.
        crit["include_active_projects"] = True
    if filters.get("fiscal_years"):
        crit["fiscal_years"] = [int(y) for y in filters["fiscal_years"]]
    if filters.get("agencies"):
        crit["agencies"] = list(filters["agencies"])
    if filters.get("activity_codes"):
        crit["activity_codes"] = list(filters["activity_codes"])
    if filters.get("exclude_subprojects"):
        crit["exclude_subprojects"] = True
    crit.update(filters.get("extra_criteria") or {})
    return crit


def parse_person_name(name: str) -> tuple[str, str]:
    """'Pazin, Michael J' or 'Michael J Pazin' -> ('Michael', 'Pazin')."""
    name = " ".join(name.replace(".", " ").split())
    if "," in name:
        last, _, rest = name.partition(",")
        first = rest.split()[0] if rest.split() else ""
        return first.strip(), last.strip()
    parts = name.split()
    if len(parts) == 1:
        return "", parts[0]
    return parts[0], parts[-1]


def _po_specs(rows: Iterable[dict[str, str]]) -> list[QuerySpec]:
    specs = []
    for row in rows:
        first, last = row.get("first_name", ""), row.get("last_name", "")
        if not last and row.get("name"):
            first, last = parse_person_name(row["name"])
        if not last:
            continue
        po = {"first_name": first, "last_name": last}
        value = f"{first} {last}".strip()
        specs.append(QuerySpec(
            query_id=f"po_{slugify(f'{last} {first}')}",
            query_type="program_officer",
            value=value,
            label=row.get("label") or value,
            group=row.get("group", ""),
            criteria={"po_names": [po]},
        ))
    return specs


def _opportunity_specs(rows: Iterable[dict[str, str]]) -> list[QuerySpec]:
    specs = []
    for row in rows:
        opp = row.get("opportunity_number") or row.get("foa") or row.get("opportunity", "")
        if not opp:
            continue
        opp = opp.upper()
        specs.append(QuerySpec(
            query_id=f"opp_{slugify(opp)}",
            query_type="opportunity",
            value=opp,
            label=row.get("label") or opp,
            group=row.get("group", ""),
            criteria={"foa": [opp]},
        ))
    return specs


def _text_specs(rows: Iterable[dict[str, str]]) -> list[QuerySpec]:
    specs = []
    for row in rows:
        text = row.get("search_text", "")
        if not text:
            continue
        specs.append(QuerySpec(
            query_id=f"txt_{slugify(row.get('label') or text)}",
            query_type="text",
            value=text,
            label=row.get("label") or text,
            group=row.get("group", ""),
            criteria={"advanced_text_search": {
                "operator": row.get("operator") or "and",
                "search_field": row.get("search_field") or DEFAULT_TEXT_FIELDS,
                "search_text": text,
            }},
        ))
    return specs


BUILDERS = {
    "program_officers": _po_specs,
    "opportunities": _opportunity_specs,
    "text_searches": _text_specs,
}


def build_queries(inputs: dict[str, str | None], filters: dict[str, Any] | None = None,
                  base_dir: str | Path = ".") -> list[QuerySpec]:
    """Read every configured input table and return de-duplicated query specs."""
    filters = filters or {}
    base = global_criteria(filters)
    complete = bool(filters.get("complete_grants", True))
    for unknown in set(inputs or {}) - set(BUILDERS):
        raise ValueError(f"Unknown input type '{unknown}'. Use one of: {', '.join(BUILDERS)}")
    specs: list[QuerySpec] = []
    for key, builder in BUILDERS.items():
        path = (inputs or {}).get(key)
        if not path:
            continue
        p = Path(path)
        if not p.is_absolute():
            p = Path(base_dir) / p
        if not p.exists():
            raise FileNotFoundError(f"Input table for '{key}' not found: {p}")
        specs.extend(builder(read_table(p)))

    seen_ids: dict[str, int] = {}
    seen_criteria: set[str] = set()
    out: list[QuerySpec] = []
    for spec in specs:
        spec.criteria = {**base, **spec.criteria}
        spec.complete_grants = complete
        key = json.dumps(spec.criteria, sort_keys=True).lower()
        if key in seen_criteria:  # same search listed twice (e.g. a PO pasted twice)
            continue
        seen_criteria.add(key)
        if spec.query_id in seen_ids:
            seen_ids[spec.query_id] += 1
            spec.query_id = f"{spec.query_id}_{seen_ids[spec.query_id]}"
        else:
            seen_ids[spec.query_id] = 1
        out.append(spec)
    if not out:
        raise ValueError("No queries found: check the 'inputs' section of the config.")
    return out


def groups_of(specs: Iterable[QuerySpec]) -> dict[str, list[str]]:
    """{group portfolio id: [query ids]} for every group with 2+ queries.

    (A group with a single query would just duplicate that query's portfolio.)
    """
    groups: dict[str, list[str]] = {}
    for s in specs:
        if s.group:
            groups.setdefault(group_id(s.group), []).append(s.query_id)
    return {g: q for g, q in groups.items() if len(q) > 1}
