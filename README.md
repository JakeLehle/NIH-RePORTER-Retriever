# NIH RePORTER Retriever

A Snakemake pipeline that queries the [NIH RePORTER API](https://api.reporter.nih.gov/)
and builds a database of the grants currently funded under specific **program
officers**, **funding opportunities** (RFA/PAR/PA numbers), or **consortia / free-text
searches**.

The goal is to answer *"who funds what I want to do?"* and then, in later phases,
summarise those portfolios to find gaps and draft Specific Aims pages that address them.

| Phase | Status | What it does |
|---|---|---|
| 1. Retrieve | ✅ this repo | Input tables → RePORTER queries → one human-readable JSON grant file per search term, checked and listed in a manifest. Pure retrieval, no summarisation or AI. |
| 2. Landscape & gaps | planned | Takes the checked grant files and summarises the funded work: questions being addressed, approaches, and where novel work could fit. |

## Install

```bash
conda env create -f environment.yml     # or: pip install -e ".[test]"
conda activate nih-reporter
```

## Quick start

```bash
# 1. Edit the example input tables in config/ (or make a new project folder)
nih-reporter create-config my_project/

# 2. Preview the queries the config resolves to (no API calls)
nih-reporter list-queries my_project/config.yaml

# 3. Run the pipeline
nih-reporter run my_project/config.yaml --cores 2
#   equivalent to:
snakemake -s workflow/Snakefile --configfile my_project/config.yaml --cores 2

# Refresh from the API later (raw results are cached otherwise)
nih-reporter run my_project/config.yaml --refresh

# One-off lookup without editing tables
nih-reporter search --po "Michael Pazin" --opportunity RFA-HG-20-043 --text IGVF --out-dir results_quick
```

## Inputs

Any combination of three tables (TSV or CSV, `#` lines are comments). Every table
may also have optional `label` and `group` columns; `group` lets you roll several
queries up together, e.g. all the RFAs that fund one consortium.

**`program_officers.tsv`**
```
first_name	last_name	group
Michael	Pazin	NHGRI functional genomics
```

**`opportunities.tsv`** – RFA / PAR / PA numbers
```
opportunity_number	group
RFA-HG-20-043	IGVF
RFA-HG-20-047	IGVF
```

**`text_searches.tsv`** – consortium names, acronyms, topics. Searched in title,
terms and abstract by default.
```
search_text	label	group
IGVF	IGVF (text match)	IGVF
```

> Tip: a consortium is usually best captured by its RFAs (precise) **plus** a text
> search for its name (catches grants funded through parent announcements that
> mention the consortium). Giving both the same `group` keeps them together.

### Filters (`config.yaml`)

Applied to every query: `active_only` (default `true`), `fiscal_years`, `agencies`
(IC abbreviations, e.g. `NHGRI`), `activity_codes`, `exclude_subprojects`, and
`extra_criteria` for anything else in the RePORTER criteria spec.

## Outputs

```
results/
├── grants/
│   ├── {query}.json              ONE FILE PER SEARCH TERM: every grant it found, all fields (see below)
│   └── manifest.json             written only if every grant file passes its checks → Phase 2 input
├── queries/{query}.json          resolved search criteria per query
├── raw/{query}.json              untouched API records + retrieval timestamp
├── tables/{query}.tsv            flat table, one row per RePORTER record
├── database/
│   ├── records.tsv               every record (one per appl_id: each FY, supplement, subproject)
│   ├── portfolio.tsv             ONE ROW PER GRANT (core project number), collapsed
│   ├── query_hits.tsv            which query found which grant
│   ├── portfolio.jsonl           one JSON object per grant across ALL searches (flat, de-duplicated)
│   └── nih_reporter.sqlite       all tables above + long PI and PO tables
└── reports/
    ├── program_officer_summary.tsv   one row per PO seen on ANY retrieved grant
    └── portfolios/{query}.md         readable portfolio: overview, grant table, all abstracts
```

### Grant files (`grants/{query}.json`)

The main Phase 1 output: a pretty-printed JSON file per search term, built straight
from the API records.

```jsonc
{
  "schema_version": 1,
  "search":   { "query_type": "opportunity", "searched_for": "RFA-HG-20-043",
                "group": "IGVF", "criteria": {...}, "retrieved_at": "...", "api_records": 8 },
  "overview": { "n_grants": 8, "total_award_latest_fy": 14698628,
                "activity_codes": {"UM1": 8}, "program_officers": {...},
                "funding_opportunities": {...}, "organizations": {...},
                "project_end_years": {"2027": 8}, "spending_categories": {...} },
  "grants": [
    { "core_project_num": "UM1HG011966", "title": "...", "activity_code": "UM1",
      "institute": {...}, "opportunity_numbers": [...], "program_officers": [...],
      "principal_investigators": [{"name": "...", "is_contact_pi": true}, ...],
      "organization": {...}, "dates": {"project_start": "...", "project_end": "..."},
      "funding": {"latest_fiscal_year": 2025, "award_amount_latest_fy": ...,
                  "award_amount_by_fiscal_year": {...}},
      "spending_categories": [...], "terms": [...],
      "abstract": "...", "public_health_relevance": "...", "reporter_url": "...",
      "records": [ every RePORTER record (fiscal year / supplement / subproject) for this grant ] }
  ]
}
```

The `check_grant_files` rule then checks every grant file: it exists and parses,
it has the required fields, its grant count matches its overview, and its record
count matches the raw API pull. Grants with no abstract are logged as
warnings. If everything passes it writes `grants/manifest.json` (files, grant counts,
checksums, warnings); if anything fails the job stops and lists the problems. Phase 2
rules take the manifest as their input, so they only ever run on complete, checked data.

`program_officer_summary.tsv` includes every PO that appears on a retrieved grant,
not just the ones you searched for — so searching an RFA or consortium also tells
you *which POs manage that area*. Columns: number of grants, total latest-FY award,
institutes, activity codes, funding opportunities, top organisations and
distinctive terms.

Example SQL against the database:

```sql
-- Grants per PO, ending in the next two years
SELECT p.name, COUNT(DISTINCT g.core_project_num) AS n
FROM program_officers p JOIN portfolio g USING (core_project_num)
WHERE g.project_end_date < '2028-12-31'
GROUP BY p.name ORDER BY n DESC;
```

### Notes on the data

* RePORTER returns one record per *application* (each fiscal year, supplement and
  subproject). `portfolio.tsv` collapses these to one row per grant:
  the newest parent (non-subproject) record supplies the title/abstract, and
  `award_amount_latest_fy` sums parent awards in the latest fiscal year (supplements
  included, subprojects excluded to avoid double counting center grants).
* `active_only: true` uses RePORTER's *include active projects* flag, which can also
  return prior-FY records of active grants; they collapse into the same grant row.
* The API allows at most 15,000 records per query; larger queries stop with a
  message asking you to narrow them (fiscal years, agencies, activity codes).
* Requests are throttled to ~1 per second as NIH asks.

## Layout

```
config/                 example config.yaml + input tables
workflow/Snakefile      rules: query_spec → fetch_query → flatten_query / build_database → reports
nih_reporter/           python package used by the rules and the CLI
  api.py                RePORTER client (paging, retries, throttle)
  queries.py            input tables + filters → search criteria
  normalize.py          flatten raw records
  database.py           merge, collapse to grants, SQLite / JSONL
  reports.py            PO summary + Markdown portfolios
  cli.py                nih-reporter command
tests/                  offline tests (pytest) with trimmed real API fixtures
```

## Roadmap: Phase 2 (draft)

New rules take `grants/manifest.json` as input and work from the grant files it lists:

1. **Landscape summary** – per search term (or `group`), summarise the funded work:
   the questions being addressed, approaches / model systems / technologies, and
   how the portfolio is distributed across them.
2. **Gap analysis** – identify where the landscape is thin: questions or approaches
   with little or no funded work, and grants ending soon with no follow-on. For
   each gap: what has been done nearby, and what novel work there would add.
3. Later: draft Specific Aims pages aimed at chosen gaps, naming the PO to contact.
