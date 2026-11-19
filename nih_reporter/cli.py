"""Command line interface.

    nih-reporter create-config my_project/        # starter config + input tables
    nih-reporter run my_project/config.yaml       # run the Snakemake workflow
    nih-reporter search --po "Michael Pazin"      # quick one-off lookup, no Snakemake
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
from .queries import QuerySpec, build_queries, global_criteria, slugify
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
@click.argument("snakemake_args", nargs=-1, type=click.UNPROCESSED)
def run(config_yaml, cores, dry_run, refresh, snakemake_args):
    cmd = ["snakemake", "-s", str(SNAKEFILE), "--configfile", config_yaml, "--cores", str(cores)]
    if dry_run:
        cmd.append("-n")
    if refresh:
        cmd += ["--forcerun", "fetch_query"]
    cmd += list(snakemake_args)
    click.echo(" ".join(cmd))
    sys.exit(subprocess.call(cmd))


@main.command("search", help="One-off search without Snakemake; writes the same outputs to OUT_DIR.")
@click.option("--po", "po_names", multiple=True, help='Program officer, "First Last". Repeatable.')
@click.option("--opportunity", "opps", multiple=True, help="Opportunity number, e.g. RFA-HG-20-043. Repeatable.")
@click.option("--text", "texts", multiple=True, help="Free-text search (title/terms/abstract). Repeatable.")
@click.option("--all-years", is_flag=True, help="Include inactive projects (default: active only).")
@click.option("--out-dir", default="results_search", show_default=True)
def search(po_names, opps, texts, all_years, out_dir):
    base = global_criteria({"active_only": not all_years})
    specs = []
    for name in po_names:
        parts = name.split()
        po = {"first_name": " ".join(parts[:-1]), "last_name": parts[-1]}
        specs.append(QuerySpec(f"po_{slugify(parts[-1] + ' ' + ' '.join(parts[:-1]))}",
                               "program_officer", name, name, "", {**base, "po_names": [po]}))
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


@main.command("list-queries", help="Show the queries a config resolves to, without calling the API.")
@click.argument("config_yaml", type=click.Path(exists=True, dir_okay=False))
def list_queries(config_yaml):
    cfg = yaml.safe_load(Path(config_yaml).read_text())
    for q in build_queries(cfg["inputs"], cfg.get("filters")):
        click.echo(f"{q.query_id}\t{q.query_type}\t{q.value}\t{q.criteria}")


if __name__ == "__main__":
    main()
