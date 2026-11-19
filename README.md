# NIH RePORTER Retriever

A Snakemake pipeline that queries the [NIH RePORTER API](https://api.reporter.nih.gov/)
and builds a database of the grants currently funded under specific **program
officers**, **funding opportunities** (RFA/PAR/PA numbers), or **consortia / free-text
searches**.

The goal is to answer *"who funds what I want to do?"* and then, in later stages,
summarise those portfolios to find gaps and draft Specific Aims pages that address them.

| Stage | Status | What it does |
|---|---|---|
| 1. Retrieve | ✅ this repo | Input tables → RePORTER queries → portfolio database + readable reports |
| 2. Summarise | planned | Per-grant structured summaries, topic clustering per PO / consortium |
| 3. Gap analysis | planned | Compare portfolio coverage against the FOA / IC strategic-plan priorities |
| 4. Aims drafting | planned | Draft Specific Aims pages aimed at the gaps, routed to the matching PO |

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
├── queries/{query}.json          resolved search criteria per query
├── raw/{query}.json              untouched API records + retrieval timestamp
├── tables/{query}.tsv            flat table, one row per RePORTER record
├── database/
│   ├── records.tsv               every record (one per appl_id: each FY, supplement, subproject)
│   ├── portfolio.tsv             ONE ROW PER GRANT (core project number), collapsed
│   ├── query_hits.tsv            which query found which grant
│   ├── portfolio.jsonl           one JSON object per grant with title/abstract/PHR/terms → Stage 2 input
│   └── nih_reporter.sqlite       all tables above + long PI and PO tables
└── reports/
    ├── program_officer_summary.tsv   one row per PO seen on ANY retrieved grant
    └── portfolios/{query}.md         readable portfolio: overview, grant table, all abstracts
```

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

## Roadmap for stages 2–4 (draft)

New rules will go in `workflow/rules/*.smk`, all starting from `database/portfolio.jsonl`:

1. **`summarize_grants`** – LLM pass per grant → structured JSON (biological question,
   model system, data modality / technology, disease area, aims, deliverables).
   Cached per `core_project_num`, so only new grants cost anything on refresh.
2. **`cluster_portfolio`** – embed summaries, cluster per PO / group, label clusters →
   a "map" of each portfolio with counts and $ per theme.
3. **`gap_analysis`** – compare the map against reference text (the FOA's stated
   research objectives, the IC strategic plan, your own research interests) to flag
   objectives with few or no funded grants, plus grants ending soon with no follow-on.
4. **`draft_aims`** – for chosen gaps, draft a one-page Specific Aims outline citing
   the closest funded grants (for differentiation) and naming the PO to contact.
