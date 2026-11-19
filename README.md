# NIH RePORTER Retriever

A Snakemake pipeline that pulls every **currently funded** grant in a program officer's
portfolio (or a funding opportunity's, or a consortium's) from the
[NIH RePORTER API](https://api.reporter.nih.gov/), writes it all to readable JSON files,
and then has a **local LLM (via Ollama)** summarise each portfolio: the questions being
addressed, the approaches used, patterns, and where new work could fit.

## Intended workflow

1. Give the pipeline your **Specific Aims text**. It sends it to
   [RePORTER Matchmaker](https://reporter.nih.gov/matchmaker), finds the program officers
   who manage the most similar *currently funded* grants, and keeps the ones above a
   cutoff (most officers manage only one similar grant, so they are dropped).
   You can also skip this and list the officers yourself.
2. For each selected officer it pulls every active grant in their portfolio:
   `results/grants/po_<name>.json` – all details and full abstracts (pure retrieval, no AI).
3. A local LLM writes `results/summaries/po_<name>.md`: overview, research themes, the
   questions and approaches, patterns, and gaps / opportunities, citing grant numbers.
4. Use the summaries to sharpen your aims and decide whom to contact.

The same works for a funding opportunity (RFA/PAR) or a consortium (its RFAs plus a
text search for its name, rolled up into one combined portfolio).

| Phase | What it does | Needs |
|---|---|---|
| 0. Matchmaker (optional) | Aims text → similar grants → their current officers → ranked → cutoff → `matchmaker/program_officers.tsv` | Internet access to `reporter.nih.gov` |
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
nih-reporter create-config my_project/        # starter config + input tables + aims template
nih-reporter list-queries my_project/config.yaml   # preview the searches, no API calls

nih-reporter run my_project/config.yaml                    # everything
nih-reporter run my_project/config.yaml --retrieve-only    # no LLM step
nih-reporter run my_project/config.yaml --refresh          # re-query Matchmaker + the API
```

`nih-reporter run` is a thin wrapper around
`snakemake -s workflow/Snakefile --configfile <config> --cores 1 --resources ollama=1 [retrieve]`;
anything after the config path that it doesn't recognise is passed to snakemake.

## Feeding in program officers

There are four ways in, from quickest to most automated. All of them produce the same
outputs, and program officers are always searched by **first + last name**, active
grants only unless you say otherwise.

### 1. One officer, no config

```bash
nih-reporter search --po "Pazin, Michael J"
```
Writes `results_search/grants/po_pazin_michael.json` (+ manifest, database, reports).
Names can be "Last, First M" (as RePORTER/Matchmaker show them) or "First M Last";
the middle name is ignored. Add `--include-inactive` for all years.

### 2. A few officers typed out, no config

```bash
nih-reporter search --po "Pazin, Michael J" --po "Lisa Chadwick" --po "Gilchrist, Daniel A"
```
One grant file per officer. `--opportunity RFA-HG-20-043` and `--text IGVF` can be mixed in.
Duplicates (the same person written two ways) are searched once.

### 3. A file of officers

Either one-off:
```bash
nih-reporter search --po-file my_officers.tsv
```
or as part of a project (`inputs.program_officers` in `config.yaml`, which can also be
a list of files). The file is TSV or CSV (by extension), `#` lines are comments, and it
needs **either** a `name` column **or** `first_name` + `last_name` columns. Optional
`label` and `group` columns; any other columns are ignored, so you can keep notes.
```
name	group
Pazin, Michael J	functional genomics
Lisa Chadwick	functional genomics
Daniel A Gilchrist
```

### 4. From your Specific Aims via Matchmaker

Matchmaker has no download button, so the pipeline asks it directly. Preview first:

```bash
nih-reporter matchmaker my_aims.txt                    # default: knee cutoff
nih-reporter matchmaker my_aims.txt --method above_median
nih-reporter matchmaker my_aims.txt --method top_n --value 10
```

This writes to `results_matchmaker/`:
- `selection.md` – the count distribution, where the cutoff fell, and a ranked table:
  **check this first**;
- `program_officers.tsv` – the selected officers, ready to use as input (route 3);
- `program_officers_all.tsv` – every officer with both counts and the grants matched;
- `similar_projects.tsv` – Matchmaker's 500 similar projects and their current officers.

To make it part of a project run, set in `config.yaml`:
```yaml
matchmaker:
  enabled: true
  aims_file: my_aims.txt
  selection: {method: knee, min_projects: 2, max_officers: null}
```
The selected officers are then added to any `program_officers` table and the rest of the
pipeline runs over all of them (a Snakemake checkpoint). Changing the aims file or the
selection settings reruns Matchmaker; officers already pulled are not pulled again.

**How officers are counted.** Matchmaker returns the ~500 projects most similar to your
text. Its own per-officer counts include grants that have **already ended** (its
"active" filter is not applied to that list) and officers who no longer hold those
grants. So by default (`count_source: current`) each similar project is looked up in
the official API and credited to the program officer on its current record; ended
grants are not counted. In a test with a functional-genomics aims paragraph, 239 of
the 500 similar projects were active, managed by 111 officers, 73 of whom had a single
one; Matchmaker credited 32 officers with 2+ projects who held none of them as active
grants. Use `count_source: matchmaker` to reproduce the website's numbers.

**Choosing the cutoff** (`selection.method`). `min_projects` (default 2) is a floor for
every method, `max_officers` an optional cap, and officers tied at the cutoff are kept
together. With the test above (111 officers):

| Method | Keeps | Result in the test |
|---|---|---|
| `knee` (default) | officers above the elbow of the ranked counts | ≥ 3 grants → 18 officers (48% of matched grants) |
| `above_median` | more matched grants than the median | ≥ 2 → 38 |
| `min_projects` + `value: N` | at least N | N = 5 → 9 |
| `top_n` + `value: N` | the N highest-ranked (ties kept) | N = 10 → 10 |
| `coverage` + `value: 0.5` | fewest top officers covering that share of matched grants | → 38 |

**Caveats.** Matchmaker is not part of the documented RePORTER API; the pipeline calls
the same service the website uses (2 requests per run, plus ~5 to the official API).
It accepts up to 15,000 characters. If NIH changes it or it asks for a captcha, the
step stops with a message and you can fall back to route 3. Officer matching is by
first + last name, so two officers sharing both would be merged.

## Other inputs

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
currently funded by consortium X", or give Matchmaker-selected officers a `group` to get
one combined portfolio of all of them.

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
├── matchmaker/                   selection.md, program_officers(_all).tsv, similar_projects.tsv
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
workflow/Snakefile      [matchmaker checkpoint] → query_spec → fetch_query → grant_file / group_grant_file
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
  matchmaker.py         Matchmaker query, current-officer counts, cutoff selection
  cli.py                nih-reporter command
tests/                  offline tests (fake RePORTER / Matchmaker responses + fake Ollama server)
```
