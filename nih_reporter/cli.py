"""Command line interface.

    nih-reporter create-config my_project/        # starter config + input tables
    nih-reporter run my_project/config.yaml       # run the Snakemake workflow
    nih-reporter run my_project/config.yaml --retrieve-only   # Phase 1 only (no LLM)
    nih-reporter search --po "Michael Pazin"      # quick one-off lookup, no Snakemake
    nih-reporter search --po-file my_pos.tsv      # same, for a table of officers
    nih-reporter matchmaker specific_aims.txt     # find officers for your aims text
    nih-reporter summarize results/grants/po_pazin_michael.json --model llama3.1:8b
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

import click
import yaml

from .database import build_database
from .landscape import build_grant_file, check_grant_files
from .pipeline import fetch_query, flatten_query, write_spec
from .queries import build_queries, build_queries_from_rows, groups_of, read_table
from .reports import write_reports

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAKEFILE = REPO_ROOT / "workflow" / "Snakefile"
EXAMPLE_CONFIG_DIR = REPO_ROOT / "config"


@click.group()
@click.option("-v", "--verbose", is_flag=True)
def main(verbose):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")


@main.command("create-config", help="Write a starter config.yaml and input tables into PROJECT_DIR.")
@click.argument("project_dir", type=click.Path(file_okay=False))
def create_config(project_dir):
    project = Path(project_dir)
    project.mkdir(parents=True, exist_ok=True)
    for name in ["program_officers.tsv", "opportunities.tsv", "text_searches.tsv", "specific_aims.txt"]:
        target = project / name
        if not target.exists():
            shutil.copy(EXAMPLE_CONFIG_DIR / name, target)
    cfg = yaml.safe_load((EXAMPLE_CONFIG_DIR / "config.yaml").read_text())
    cfg["output_dir"] = str(project / "results")
    cfg["inputs"] = {k: str(project / f"{k}.tsv") for k in cfg["inputs"]}
    cfg["matchmaker"]["aims_file"] = str(project / "specific_aims.txt")
    cfg["summarize"]["prompts"] = {k: str(REPO_ROOT / v) for k, v in cfg["summarize"]["prompts"].items()}
    cfg_path = project / "config.yaml"
    if cfg_path.exists():
        raise click.ClickException(f"{cfg_path} already exists")
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    click.echo(f"Wrote {cfg_path}. Edit the .tsv tables, then: nih-reporter run {cfg_path}")


@main.command("run", context_settings={"ignore_unknown_options": True},
              help="Run the Snakemake workflow. Extra arguments are passed to snakemake.")
@click.argument("config_yaml", type=click.Path(exists=True, dir_okay=False))
@click.option("--cores", default=1, show_default=True)
@click.option("--dry-run", "-n", is_flag=True)
@click.option("--refresh", is_flag=True, help="Re-query the API even if raw results exist.")
@click.option("--retrieve-only", is_flag=True, help="Phase 1 only: retrieval and grant files, no LLM.")
@click.argument("snakemake_args", nargs=-1, type=click.UNPROCESSED)
def run(config_yaml, cores, dry_run, refresh, retrieve_only, snakemake_args):
    cmd = ["snakemake", "-s", str(SNAKEFILE), "--configfile", config_yaml, "--cores", str(cores),
           "--resources", "ollama=1"]
    if dry_run:
        cmd.append("-n")
    if refresh:
        cmd += ["--forcerun", "fetch_query", "matchmaker"]
    if retrieve_only:
        cmd.append("retrieve")
    cmd += list(snakemake_args)
    click.echo(" ".join(cmd))
    sys.exit(subprocess.call(cmd))


@main.command("search", help="One-off search without a config or Snakemake; writes the same "
                             "outputs (grant files, manifest, database, reports) to --out-dir.")
@click.option("--po", "po_names", multiple=True, help='Program officer: "First Last" or "Last, First M". Repeatable.')
@click.option("--po-file", "po_files", multiple=True, type=click.Path(exists=True, dir_okay=False),
              help="Table of program officers (TSV/CSV, as for the pipeline). Repeatable.")
@click.option("--opportunity", "opps", multiple=True, help="Opportunity number, e.g. RFA-HG-20-043. Repeatable.")
@click.option("--text", "texts", multiple=True, help="Free-text search (title/terms/abstract). Repeatable.")
@click.option("--include-inactive", is_flag=True, help="Include grants that are no longer active (default: active only).")
@click.option("--out-dir", default="results_search", show_default=True)
def search(po_names, po_files, opps, texts, include_inactive, out_dir):
    rows: dict[str, list[dict[str, str]]] = {
        "program_officers": [{"name": n} for n in po_names],
        "opportunities": [{"opportunity_number": o} for o in opps],
        "text_searches": [{"search_text": t} for t in texts],
    }
    for f in po_files:
        rows["program_officers"] += read_table(f)
    specs = build_queries_from_rows(rows, {"active_only": not include_inactive}, allow_empty=True)
    if not specs:
        raise click.UsageError("Give at least one --po, --po-file, --opportunity or --text")
    click.echo(f"{len(specs)} search(es): " + ", ".join(s.value for s in specs))
    out = Path(out_dir)
    raws = []
    for spec in specs:
        spec_path = out / "queries" / f"{spec.query_id}.json"
        raw_path = out / "raw" / f"{spec.query_id}.json"
        write_spec(spec, spec_path)
        fetch_query(spec_path, raw_path)
        flatten_query(raw_path, out / "tables" / f"{spec.query_id}.tsv")
        build_grant_file(raw_path, out / "grants" / f"{spec.query_id}.json")
        raws.append(raw_path)
    grant_files = [out / "grants" / f"{s.query_id}.json" for s in specs]
    manifest = check_grant_files(grant_files, raws, out / "grants" / "manifest.json")
    build_database(raws, out / "database")
    write_reports(out / "database", out / "reports")
    click.echo(f"Done: {manifest['n_grants_total']} grants in {len(grant_files)} file(s) under "
               f"{out / 'grants'}. Database: {out / 'database'}, reports: {out / 'reports'}")


@main.command("matchmaker", help="Send your Specific Aims (a text file) to RePORTER Matchmaker, rank the "
                                 "program officers by matched projects and select those above a cutoff. "
                                 "Writes program_officers.tsv (selected) + selection.md to --out-dir.")
@click.argument("aims_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--method", type=click.Choice(["knee", "above_median", "min_projects", "top_n", "coverage"]),
              default="knee", show_default=True, help="How to choose the cutoff.")
@click.option("--value", type=float, help="N for min_projects / top_n, fraction for coverage (e.g. 0.5).")
@click.option("--min-projects", type=int, default=2, show_default=True,
              help="Floor for every method (2 drops officers with a single matching project).")
@click.option("--max-officers", type=int, help="Optional hard cap on selected officers.")
@click.option("--include-inactive", is_flag=True, help="Match against all projects, not only active ones.")
@click.option("--count-source", type=click.Choice(["current", "matchmaker"]), default="current", show_default=True,
              help="current: credit each similar grant to its PO in the RePORTER API now; "
                   "matchmaker: use the website's counts (can be out of date).")
@click.option("--group", default="", help="Group label to give the selected officers.")
@click.option("--out-dir", default="results_matchmaker", show_default=True)
def matchmaker_cmd(aims_file, method, value, min_projects, max_officers, include_inactive, count_source,
                   group, out_dir):
    from .matchmaker import run_matchmaker

    stats = run_matchmaker(Path(aims_file).read_text(encoding="utf-8"), out_dir,
                           active_only=not include_inactive, method=method, value=value,
                           min_projects=min_projects, max_officers=max_officers, group=group,
                           count_source=count_source)
    out = Path(out_dir)
    click.echo(f"{stats['n_officers']} officers; selected {stats['n_selected']} with >= "
               f"{stats['threshold']} matched grants ({stats['reason']}).")
    if stats["stale_matchmaker_officers"]:
        click.echo(f"{len(stats['stale_matchmaker_officers'])} officers Matchmaker credits manage none of the "
                   "similar grants today (ended or reassigned) - see selection.md.")
    click.echo(f"Review {out / 'selection.md'}; selected list: {out / 'program_officers.tsv'}")
    click.echo(f"Next: nih-reporter search --po-file {out / 'program_officers.tsv'}   "
               f"(or list that file under inputs.program_officers in a config)")


@main.command("summarize", help="Summarise one grant file (grants/*.json) with a local Ollama model.")
@click.argument("grant_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--config", "config_yaml", type=click.Path(exists=True, dir_okay=False),
              help="Take the summarize settings from this config; options below override it.")
@click.option("--model", help="Ollama model, e.g. llama3.1:8b")
@click.option("--host", help="Ollama host [default: http://localhost]")
@click.option("--port", type=int, help="Ollama port [default: 11434]")
@click.option("--num-ctx", type=int, help="Context window in tokens.")
@click.option("--out-dir", help="Default: summaries/ next to the grants/ folder.")
def summarize_cmd(grant_file, config_yaml, model, host, port, num_ctx, out_dir):
    from .summarize import summarize_portfolio

    cfg = {}
    if config_yaml:
        cfg = yaml.safe_load(Path(config_yaml).read_text()).get("summarize") or {}
    ollama = dict(cfg.get("ollama") or {})
    for key, val in {"model": model, "host": host, "port": port, "num_ctx": num_ctx}.items():
        if val is not None:
            ollama[key] = val
    if not ollama.get("model"):
        raise click.UsageError("Give --model or a --config with summarize.ollama.model")
    cfg["ollama"] = ollama
    grant_path = Path(grant_file)
    out = Path(out_dir) if out_dir else grant_path.parent.parent / "summaries"
    out.mkdir(parents=True, exist_ok=True)
    manifest = grant_path.parent / "manifest.json"
    summarize_portfolio(grant_path, out / f"{grant_path.stem}.json", out / f"{grant_path.stem}.md", cfg,
                        cache_dir=out / "cache", manifest=manifest if manifest.exists() else None)
    click.echo(f"Wrote {out / (grant_path.stem + '.md')}")


@main.command("list-queries", help="Show the queries a config resolves to, without calling the API.")
@click.argument("config_yaml", type=click.Path(exists=True, dir_okay=False))
def list_queries(config_yaml):
    cfg = yaml.safe_load(Path(config_yaml).read_text())
    mm = cfg.get("matchmaker") or {}
    specs = build_queries(cfg.get("inputs"), cfg.get("filters"), allow_empty=bool(mm.get("enabled")))
    if mm.get("enabled"):
        click.echo(f"# + program officers selected by Matchmaker from {mm.get('aims_file')} "
                   f"(added at run time; preview with: nih-reporter matchmaker {mm.get('aims_file')})")
    for q in specs:
        click.echo(f"{q.query_id}\t{q.query_type}\t{q.value}\t{q.criteria}")
    for gid, qids in groups_of(specs).items():
        click.echo(f"{gid}\tgroup\t{', '.join(qids)}")


if __name__ == "__main__":
    main()
