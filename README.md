# NIH RePORTER Retriever

A Snakemake pipeline that pulls every **currently funded** grant in a program officer's
portfolio (or a funding opportunity's, or a consortium's) from the
[NIH RePORTER API](https://api.reporter.nih.gov/), writes it all to readable JSON files,
and then has a **local LLM (via Ollama)** summarise each portfolio: the questions being
addressed, the approaches used, patterns, and where new work could fit.

## Intended workflow

1. Paste your Specific Aims into [RePORTER Matchmaker](https://reporter.nih.gov/matchmaker)
   and note the program officers who manage the most similar funded projects.
2. List those POs in `program_officers.tsv`.
3. Run the pipeline. For each PO you get:
   - `results/grants/po_<name>.json` – every active grant in their portfolio, with all
     details and full abstracts (pure retrieval, no AI);
   - `results/summaries/po_<name>.md` – a local-LLM landscape summary: overview,
     research themes, the questions and approaches, patterns, and gaps / opportunities,
     with every claim citing grant numbers.
4. Use the summaries to sharpen your aims and decide whom to contact.

The same works for a funding opportunity (RFA/PAR) or a consortium (its RFAs plus a
text search for its name, rolled up into one combined portfolio).

| Phase | What it does | Needs |
|---|---|---|
| 1. Retrieve | Input tables → RePORTER searches → complete grant records → one JSON grant file per search term (+ per group) → checked, listed in `grants/manifest.json` | Internet access to `api.reporter.nih.gov` |
| 2. Summarise | `manifest.json` + each grant file → local LLM → `summaries/*.md` / `*.json` | [Ollama](https://ollama.com) running locally with a model pulled |

## Install

```bash
conda env create -f environment.yml     # or: pip install -e ".[test]"
conda activate nih-reporter

# For Phase 2 - install Ollama from https://ollama.com, then:
ollama pull llama3.1:8b                  # or any model; set it in config.yaml
ollama serve                             # if not already running as a service
```

## Quick start

```bash
nih-reporter create-config my_project/          # starter config + input tables
# edit my_project/program_officers.tsv (and/or the other tables)
nih-reporter list-queries my_project/config.yaml   # preview searches, no API calls

nih-reporter run my_project/config.yaml                    # Phase 1 + Phase 2
nih-reporter run my_project/config.yaml --retrieve-only    # Phase 1 only (no LLM)
nih-reporter run my_project/config.yaml --refresh          # re-pull from the API

# One-off lookups without editing tables
nih-reporter search --po "Pazin, Michael J" --out-dir results_quick
nih-reporter summarize results_quick/grants/po_pazin_michael.json --model llama3.1:8b
```

`nih-reporter run` is a thin wrapper around
`snakemake -s workflow/Snakefile --configfile <config> --cores 1 --resources ollama=1 [retrieve]`.

## Inputs

Any combination of three tables (TSV or CSV, `#` lines are comments). Each may have
optional `label` and `group` columns. Duplicate rows (e.g. the same PO listed twice)
are dropped.

**`program_officers.tsv`** – either `first_name` + `last_name` columns, or one `name`
column in either "First M Last" or "Last, First M" form (as RePORTER displays them):
```
name
Pazin, Michael J
Lisa Chadwick
```

**`opportunities.tsv`** – RFA / PAR / PA numbers
```
opportunity_number	group
RFA-HG-20-043	IGVF
RFA-HG-20-047	IGVF
```

**`text_searches.tsv`** – consortium names, acronyms, topics (searched in title, terms
and abstract)
```
search_text	label	group
IGVF	IGVF (text match)	IGVF
```

Searches that share a `group` (2 or more) are also merged into a combined portfolio,
`grants/group_<name>.json`, which gets its own summary — use this for "everything
currently funded by consortium X".

## Key settings (`config.yaml`)

```yaml
filters:
  active_only: true        # true = currently funded grants only; false = all years
  complete_grants: true    # see below
  fiscal_years: []         # e.g. [2024, 2025, 2026]
  agencies: []             # e.g. [NHGRI]
  activity_codes: []       # e.g. [R01, U01]

summarize:
  enabled: true
  ollama:
    host: http://localhost
    port: 11434
    model: llama3.1:8b
    num_ctx: 32768          # context window (tokens)
  max_chars_per_call: 60000 # larger portfolios are read in parts, then combined
```

**`active_only`** applies to every search. **`complete_grants`**: after each search,
every grant it found is looked up again by its core project number (with the same
filters), so all of that grant's records are included even when a renewal, supplement
or continuation year was filed under a different opportunity number, or doesn't
mention the consortium's name. Program officer searches already match on the PO, so
this matters most for opportunity and consortium searches. Each record in the grant
files says whether it came from the search or this lookup (`found_by`).

## Outputs

```
results/
├── grants/
│   ├── {search}.json             one file per search term: every grant, all fields
│   ├── group_{group}.json        combined portfolio per group (e.g. consortium)
│   └── manifest.json             written only if every grant file passes checks
├── summaries/                    Phase 2 (local LLM)
│   ├── {portfolio}.md            readable landscape summary + grant digest table
│   ├── {portfolio}.json          same, structured: per-grant digests + synthesis
│   └── cache/{model}/            per-grant digests, reused on reruns
├── raw/{search}.json             untouched API records + retrieval timestamp
├── database/                     portfolio.tsv (one row per grant), records.tsv,
│                                 query_hits.tsv, portfolio.jsonl, nih_reporter.sqlite
├── reports/                      program_officer_summary.tsv, portfolios/{search}.md
└── logs/
```

### Grant files (`grants/*.json`)

Pretty-printed JSON built straight from the API records:

```jsonc
{
  "search":   { "query_type": "program_officer", "searched_for": "Michael Pazin",
                "criteria": {...}, "retrieved_at": "...",
                "search_records": 65, "grant_lookup_records": 0 },
  "overview": { "n_grants": 56, "total_award_latest_fy": ..., "activity_codes": {...},
                "program_officers": {...}, "funding_opportunities": {...},
                "organizations": {...}, "project_end_years": {...}, "spending_categories": {...} },
  "grants": [
    { "core_project_num": "UM1HG011966", "title": "...", "activity_code": "UM1",
      "principal_investigators": [...], "program_officers": [...], "organization": {...},
      "dates": {...}, "funding": { "latest_fiscal_year": 2025, "award_amount_latest_fy": ...,
                                   "award_amount_by_fiscal_year": {...} },
      "terms": [...], "abstract": "...", "public_health_relevance": "...",
      "records": [ every RePORTER record for this grant: year, award type, amount, found_by ] }
  ]
}
```

`check_grant_files` then confirms each file exists, parses, has every required field,
and holds exactly the records retrieved from the API. Only if all pass is
`manifest.json` written (with checksums); otherwise the run stops and lists the
problems. The summarise rule takes the manifest as input and also refuses a grant
file that changed after it was checked.

### Summaries (`summaries/*.md`)

For each portfolio the local model:

1. **Digests each grant** – research question, aims, approach, methods, model
   systems, area, outputs (cached per grant, so a refresh only re-digests new or
   changed grants);
2. **Synthesises the portfolio** from the digests plus exact counts from the grant
   file, writing: *Portfolio overview · Research themes · Approaches and technologies ·
   Patterns across the portfolio · Gaps and opportunities*.

Every claim must cite grant numbers; any cited number that isn't in the portfolio is
flagged at the top of the report. The prompts are plain text in `workflow/prompts/` —
edit them, or point `summarize.prompts` at your own.

Model choice matters: 8B models give a serviceable first pass; 14B–70B models
(e.g. `qwen2.5:14b`, `gemma3:27b`, `llama3.3:70b`) give noticeably better synthesis
if your hardware allows. Raise `num_ctx` and `max_chars_per_call` together for
large-context models.

## Notes on the data

* RePORTER returns one record per *application* (each fiscal year, supplement and
  subproject). Grant files and `portfolio.tsv` collapse these to one entry per grant:
  the newest main award (not a supplement, not a subproject) supplies the title,
  abstract and latest fiscal year; supplements still appear in
  `award_amount_by_fiscal_year` and `records`.
* `active_only: true` uses RePORTER's *include active projects* flag.
* The API allows at most 15,000 records per search; larger searches stop with a
  message asking you to narrow them.
* Requests are throttled to ~1 per second as NIH asks.

## Layout

```
config/                 example config.yaml + input tables
workflow/Snakefile      query_spec → fetch_query → grant_file / group_grant_file
                        → check_grant_files → summarize_portfolio
                        (+ flatten_query, build_database, reports)
workflow/prompts/       LLM prompt templates
nih_reporter/
  api.py                RePORTER client (paging, retries, throttle)
  queries.py            input tables + filters → searches
  pipeline.py           fetch (search + grant lookup), flatten
  landscape.py          grant files + checks + manifest
  llm.py                Ollama client
  summarize.py          per-grant digests + portfolio synthesis
  database.py, reports.py   TSV / SQLite database, PO summary, Markdown reports
  cli.py                nih-reporter command
tests/                  offline tests (fake RePORTER responses + fake Ollama server)
```
