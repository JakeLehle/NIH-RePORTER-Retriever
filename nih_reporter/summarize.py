"""Phase 2: summarise a checked grant file with a local LLM (Ollama).

Two steps, so portfolios of any size fit a local model's context window:

1. Grant digests - for each grant, extract the research question, aims,
   approach, methods, model systems, area and outputs (JSON). Cached per grant
   under ``cache_dir``, keyed on the model, prompt and grant text, so reruns
   after a data refresh only process new or changed grants.
2. Portfolio synthesis - all digests (plus exact counts from the grant file)
   go to the model in one call, which writes the landscape report: overview,
   themes, approaches, patterns, gaps and opportunities. If the digests are
   too long for one call they are split into parts, each part is summarised,
   and the part notes are combined in a final call.

Prompts live in workflow/prompts/*.txt and can be edited or replaced via the
config. A template has a "=== SYSTEM ===" and a "=== USER ===" section and
{{PLACEHOLDER}} fields.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .landscape import _sha256
from .llm import LLMError, OllamaClient
from .pipeline import read_json, write_json

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPTS = {
    "grant_extraction": REPO_ROOT / "workflow" / "prompts" / "grant_extraction.txt",
    "partial_synthesis": REPO_ROOT / "workflow" / "prompts" / "partial_synthesis.txt",
    "portfolio_synthesis": REPO_ROOT / "workflow" / "prompts" / "portfolio_synthesis.txt",
}
GRANT_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{2}[A-Z]{2}\d{6}\b")
DIGEST_KEYS = ["research_question", "specific_aims", "approach", "methods_and_technologies",
               "model_systems", "research_area", "outputs", "keywords"]


# ---------------------------------------------------------------- prompts

class Prompt:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        text = self.path.read_text(encoding="utf-8")
        if "=== USER ===" not in text:
            raise ValueError(f"{path}: prompt needs '=== SYSTEM ===' and '=== USER ===' sections")
        system, user = text.split("=== USER ===", 1)
        self.system = system.replace("=== SYSTEM ===", "").strip()
        self.user = user.strip()
        self.digest = hashlib.sha256(text.encode()).hexdigest()[:16]

    def render(self, **fields: Any) -> tuple[str, str]:
        user = self.user
        for key, value in fields.items():
            user = user.replace("{{" + key.upper() + "}}", "" if value is None else str(value))
        return self.system, user


def load_prompts(cfg: dict[str, Any]) -> dict[str, Prompt]:
    paths = {**DEFAULT_PROMPTS, **{k: Path(v) for k, v in (cfg.get("prompts") or {}).items() if v}}
    resolved = {}
    for name, path in paths.items():
        if not path.is_absolute() and not path.exists() and (REPO_ROOT / path).exists():
            path = REPO_ROOT / path  # relative to the repository rather than the cwd
        resolved[name] = Prompt(path)
    return resolved


# ---------------------------------------------------------------- step 1: digests

def _pi(grant: dict[str, Any]) -> str:
    pis = grant.get("principal_investigators") or []
    return "; ".join(p["name"] for p in pis) or "n/a"


def grant_digest(client: OllamaClient, prompt: Prompt, grant: dict[str, Any],
                 cache_dir: Path | None) -> dict[str, Any]:
    fields = {
        "grant_id": grant["core_project_num"],
        "title": grant["title"],
        "activity_code": grant["activity_code"],
        "institute": (grant.get("institute") or {}).get("abbreviation"),
        "pi": _pi(grant),
        "organization": (grant.get("organization") or {}).get("name"),
        "start": grant["dates"].get("project_start"),
        "end": grant["dates"].get("project_end"),
        "abstract": grant.get("abstract") or "(no abstract available)",
        "phr": grant.get("public_health_relevance") or "(none)",
    }
    key = hashlib.sha256(json.dumps(
        [client.model, client.options, prompt.digest, fields], sort_keys=True).encode()).hexdigest()

    cache_file = cache_dir / f"{grant['core_project_num']}.json" if cache_dir else None
    if cache_file and cache_file.exists():
        cached = read_json(cache_file)
        if cached.get("cache_key") == key:
            return cached["digest"]

    if not grant.get("abstract"):
        digest = {k: "" if k in ("research_question", "approach") else [] for k in DIGEST_KEYS}
        digest["research_question"] = "(no abstract available - see title)"
    else:
        system, user = prompt.render(**fields)
        raw = client.chat_json(system, user)
        digest = {k: raw.get(k, "" if k in ("research_question", "approach") else []) for k in DIGEST_KEYS}
    if cache_file:
        write_json({"cache_key": key, "model": client.model, "digest": digest}, cache_file)
    return digest


def _as_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return [str(v)] if v else []


def digest_text(grant: dict[str, Any], digest: dict[str, Any]) -> str:
    """Compact text block for one grant, used as synthesis input."""
    lines = [
        f"[{grant['core_project_num']}] {grant['title']}",
        f"  {grant['activity_code']} | PI: {_pi(grant)} | {(grant.get('organization') or {}).get('name')} | "
        f"{grant['dates'].get('project_start')} to {grant['dates'].get('project_end')}",
        f"  Question: {digest.get('research_question', '')}",
    ]
    aims = _as_list(digest.get("specific_aims"))
    if aims:
        lines.append("  Aims: " + " | ".join(aims))
    lines.append(f"  Approach: {digest.get('approach', '')}")
    for label, key in [("Methods", "methods_and_technologies"), ("Model systems", "model_systems"),
                       ("Area", "research_area"), ("Outputs", "outputs")]:
        vals = _as_list(digest.get(key))
        if vals:
            lines.append(f"  {label}: {'; '.join(vals)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- step 2: synthesis

def overview_text(doc: dict[str, Any]) -> str:
    ov = doc["overview"]
    s = doc["search"]

    def top(d: dict[str, int], n: int = 12) -> str:
        return "; ".join(f"{k} ({v})" for k, v in list(d.items())[:n]) or "n/a"

    lines = [
        f"- Search: {s['query_type']} = {s['searched_for']} (retrieved {s.get('retrieved_at')})",
        f"- Active grants: {ov['n_grants']}",
        f"- Total award, latest fiscal year: ${ov['total_award_latest_fy']:,.0f}",
        f"- Activity codes: {top(ov['activity_codes'])}",
        f"- Institutes: {top(ov['institutes'])}",
        f"- Program officers: {top(ov['program_officers'])}",
        f"- Funding opportunities: {top(ov['funding_opportunities'])}",
        f"- Project end years: {top(ov['project_end_years'])}",
        f"- Top organizations: {top(ov['organizations'], 8)}",
        f"- NIH spending categories: {top(ov['spending_categories'])}",
    ]
    return "\n".join(lines)


def chunk_blocks(blocks: list[str], max_chars: int) -> list[list[str]]:
    chunks: list[list[str]] = [[]]
    size = 0
    for b in blocks:
        if chunks[-1] and size + len(b) > max_chars:
            chunks.append([])
            size = 0
        chunks[-1].append(b)
        size += len(b) + 2
    return [c for c in chunks if c]


def synthesize(client: OllamaClient, prompts: dict[str, Prompt], doc: dict[str, Any],
               blocks: list[str], max_chars: int) -> tuple[str, int]:
    name = doc["search"].get("label") or doc["search"]["searched_for"]
    ptype = doc["search"]["query_type"].replace("_", " ")
    chunks = chunk_blocks(blocks, max_chars)
    if len(chunks) == 1:
        heading, material = "GRANT DIGESTS (one per funded grant):", "\n\n".join(blocks)
    else:
        notes = []
        for i, chunk in enumerate(chunks, 1):
            log.info("Synthesis part %d/%d (%d grants)", i, len(chunks), len(chunk))
            system, user = prompts["partial_synthesis"].render(
                portfolio=name, n_grants=len(chunk), part=i, n_parts=len(chunks),
                digests="\n\n".join(chunk))
            notes.append(f"### Notes on part {i} of {len(chunks)}\n\n" + client.chat(system, user))
        heading = "ANALYST NOTES (the portfolio was read in parts; notes cite grant IDs):"
        material = "\n\n".join(notes)
    system, user = prompts["portfolio_synthesis"].render(
        portfolio=name, portfolio_type=ptype, overview=overview_text(doc),
        material_heading=heading, material=material)
    log.info("Final synthesis for %s", name)
    return client.chat(system, user), len(chunks)


# ---------------------------------------------------------------- outputs

def _md_cell(text: Any) -> str:
    return " ".join(str(text or "").split()).replace("|", "/")


def render_markdown(doc: dict[str, Any], digests: dict[str, dict[str, Any]], synthesis: str,
                    meta: dict[str, Any], unknown_ids: list[str]) -> str:
    s, ov = doc["search"], doc["overview"]
    lines = [
        f"# Portfolio summary: {s.get('label') or s['searched_for']}",
        "",
        f"- **Search:** {s['query_type']} = {s['searched_for']}",
        f"- **Active grants:** {ov['n_grants']} · **Total award (latest FY):** ${ov['total_award_latest_fy']:,.0f}",
        f"- **Data retrieved:** {s.get('retrieved_at')} · **Summarised:** {meta['generated_at']} "
        f"with `{meta['model']}` (local, via Ollama)",
        f"- **Source:** `{meta['source_file']}`",
        "",
        "> Generated by a language model from grant abstracts. Check claims against the cited "
        "grants (links in the table at the end) before relying on them.",
        "",
    ]
    if unknown_ids:
        lines += [f"> ⚠️ The model cited IDs that are not in this portfolio: {', '.join(unknown_ids)}. "
                  "Treat those statements with caution.", ""]
    lines += [synthesis.strip(), "", "---", "", "## Appendix: grant digests", "",
              "| Grant | Title | PI | Ends | Research question | Approach |",
              "|---|---|---|---|---|---|"]
    for g in doc["grants"]:
        d = digests.get(g["core_project_num"], {})
        lines.append(
            f"| [{g['core_project_num']}]({g.get('reporter_url') or ''}) | {_md_cell(g['title'])} | "
            f"{_md_cell(_pi(g))} | {g['dates'].get('project_end')} | "
            f"{_md_cell(d.get('research_question'))} | {_md_cell(d.get('approach'))} |")
    return "\n".join(lines) + "\n"


def summarize_portfolio(grant_file: str | Path, out_json: str | Path, out_md: str | Path,
                        cfg: dict[str, Any], cache_dir: str | Path | None = None,
                        manifest: str | Path | None = None,
                        client: OllamaClient | None = None,
                        progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    grant_file = Path(grant_file)
    if manifest is not None:
        m = read_json(manifest)
        entry = next((f for f in m["files"] if Path(f["file"]).name == grant_file.name), None)
        if m.get("status") != "ok" or entry is None:
            raise ValueError(f"{grant_file.name} is not listed as checked in {manifest}")
        if entry.get("sha256") != _sha256(grant_file):
            raise ValueError(f"{grant_file.name} changed after it was checked; rerun check_grant_files")

    ollama_cfg = cfg.get("ollama") or {}
    client = client or OllamaClient.from_config(ollama_cfg)
    client.check()
    prompts = load_prompts(cfg)
    doc = read_json(grant_file)
    grants = doc["grants"]
    cache = Path(cache_dir) / re.sub(r"[^A-Za-z0-9._-]+", "_", client.model) if cache_dir else None

    digests: dict[str, dict[str, Any]] = {}
    for i, g in enumerate(grants, 1):
        log.info("Digest %d/%d: %s", i, len(grants), g["core_project_num"])
        try:
            digests[g["core_project_num"]] = grant_digest(client, prompts["grant_extraction"], g, cache)
        except (LLMError, ValueError) as err:
            log.warning("Could not digest %s (%s); using title only", g["core_project_num"], err)
            digests[g["core_project_num"]] = {"research_question": "(digest failed)", "approach": ""}
        if progress:
            progress(i, len(grants))

    if grants:
        blocks = [digest_text(g, digests[g["core_project_num"]]) for g in grants]
        synthesis, n_parts = synthesize(client, prompts, doc, blocks,
                                        int(cfg.get("max_chars_per_call", 60000)))
    else:
        synthesis, n_parts = "_This search matched no active grants, so there is nothing to summarise._", 0

    known = {g["core_project_num"] for g in grants}
    unknown = sorted(set(GRANT_ID_RE.findall(synthesis)) - known)
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": client.model,
        "ollama_options": client.options,
        "prompts": {k: str(p.path) for k, p in prompts.items()},
        "source_file": str(grant_file),
        "source_sha256": _sha256(grant_file),
        "synthesis_parts": n_parts,
    }
    result = {
        "schema_version": 1,
        "portfolio": doc["search"],
        "overview": doc["overview"],
        "summary": meta,
        "cited_ids_not_in_portfolio": unknown,
        "synthesis_markdown": synthesis,
        "grant_digests": [
            {"core_project_num": g["core_project_num"], "title": g["title"],
             **digests[g["core_project_num"]]} for g in grants
        ],
    }
    write_json(result, out_json)
    Path(out_md).write_text(render_markdown(doc, digests, synthesis, meta, unknown), encoding="utf-8")
    return result
