"""Command line interface.

    nih-reporter create-config my_project/        # starter config + input tables
    nih-reporter run my_project/config.yaml       # run the Snakemake workflow
    nih-reporter run my_project/config.yaml --retrieve-only   # Phase 1 only (no LLM)
    nih-reporter search --po "Michael Pazin"      # quick one-off lookup, no Snakemake
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
from .queries import QuerySpec, build_queries, global_criteria, groups_of, parse_person_name, slugify
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
    for name in ["program_officers.tsv", "opportunities.tsv", "text_searches.tsv"]:
        target = project / name
        if not target.exists():
            shutil.copy(EXAMPLE_CONFIG_DIR / name, target)
    cfg = yaml.safe_load((EXAMPLE_CONFIG_DIR / "config.yaml").read_text())
    cfg["output_dir"] = str(project / "results")
    cfg["inputs"] = {k: str(project / f"{k}.tsv") for k in cfg["inputs"]}
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
        cmd += ["--forcerun", "fetch_query"]
    if retrieve_only:
        cmd.append("retrieve")
    cmd += list(snakemake_args)
    click.echo(" ".join(cmd))
    sys.exit(subprocess.call(cmd))


@main.command("search", help="One-off search without Snakemake; writes the same outputs to OUT_DIR.")
@click.option("--po", "po_names", multiple=True, help='Program officer, "First Last". Repeatable.')
@click.option("--opportunity", "opps", multiple=True, help="Opportunity number, e.g. RFA-HG-20-043. Repeatable.")
@click.option("--text", "texts", multiple=True, help="Free-text search (title/terms/abstract). Repeatable.")
@click.option("--include-inactive", is_flag=True, help="Include grants that are no longer active (default: active only).")
@click.option("--out-dir", default="results_search", show_default=True)
def search(po_names, opps, texts, include_inactive, out_dir):
    base = global_criteria({"active_only": not include_inactive})
    specs = []
    for name in po_names:
        first, last = parse_person_name(name)
        po = {"first_name": first, "last_name": last}
        specs.append(QuerySpec(f"po_{slugify(f'{last} {first}')}", "program_officer",
                               f"{first} {last}".strip(), name, "", {**base, "po_names": [po]}))
    for opp in opps:
        specs.append(QuerySpec(f"opp_{slugify(opp)}", "opportunity", opp.upper(), opp.upper(), "",
                               {**base, "foa": [opp.upper()]}))
    for text in texts:
        specs.append(QuerySpec(f"txt_{slugify(text)}", "text", text, text, "",
                               {**base, "advanced_text_search": {
                                   "operator": "and", "search_field": "projecttitle,terms,abstracttext",
                                   "search_text": text}}))
    if not specs:
        raise click.UsageError("Give at least one --po, --opportunity or --text")
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
    specs = build_queries(cfg["inputs"], cfg.get("filters"))
    for q in specs:
        click.echo(f"{q.query_id}\t{q.query_type}\t{q.value}\t{q.criteria}")
    for gid, qids in groups_of(specs).items():
        click.echo(f"{gid}\tgroup\t{', '.join(qids)}")


if __name__ == "__main__":
    main()
