"""Turn user-supplied input tables + config filters into RePORTER search specs.

Three kinds of input table are supported (all optional, at least one needed).
Every table may be TSV or CSV, lines starting with ``#`` are ignored, and
column names are case-insensitive. Every table may also carry optional
``label`` (a human readable name) and ``group`` (e.g. a consortium name, used
to roll several queries up together downstream) columns.

program_officers   columns: first_name, last_name   (or a single ``name`` column)
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


@dataclass
class QuerySpec:
    query_id: str
    query_type: str  # program_officer | opportunity | text
    value: str  # the thing searched for, human readable
    label: str
    group: str
    criteria: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QuerySpec":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__})

    def fingerprint(self) -> str:
        return hashlib.sha1(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()[:12]


def slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text.strip()).strip("_").lower()
    return slug or "query"


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


def _po_specs(rows: Iterable[dict[str, str]]) -> list[QuerySpec]:
    specs = []
    for row in rows:
        first, last, name = row.get("first_name", ""), row.get("last_name", ""), row.get("name", "")
        if last:
            po = {"first_name": first, "last_name": last}
            value = f"{first} {last}".strip()
            slug = slugify(f"{last} {first}")
        elif name:
            po = {"any_name": name}
            value = name
            slug = slugify(name)
        else:
            continue
        specs.append(QuerySpec(
            query_id=f"po_{slug}",
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
    base = global_criteria(filters)
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
    for unknown in set(inputs or {}) - set(BUILDERS):
        raise ValueError(f"Unknown input type '{unknown}'. Use one of: {', '.join(BUILDERS)}")

    seen: dict[str, int] = {}
    out: list[QuerySpec] = []
    for spec in specs:
        spec.criteria = {**base, **spec.criteria}
        if spec.query_id in seen:
            seen[spec.query_id] += 1
            spec.query_id = f"{spec.query_id}_{seen[spec.query_id]}"
        else:
            seen[spec.query_id] = 1
        out.append(spec)
    if not out:
        raise ValueError("No queries found: check the 'inputs' section of the config.")
    return out
