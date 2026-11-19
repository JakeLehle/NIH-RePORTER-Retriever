"""Summarisation tests against a fake Ollama server (no model needed)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from nih_reporter.landscape import build_grant_file, check_grant_files
from nih_reporter.llm import LLMError, OllamaClient, parse_json_reply
from nih_reporter.summarize import chunk_blocks, summarize_portfolio

DATA = Path(__file__).parent / "data"


class FakeOllama(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *args):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({"models": [{"name": "fake-model:latest"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.calls.append(req)
        user = req["messages"][-1]["content"]
        if req.get("format") == "json":
            content = "<think>hmm</think>" + json.dumps({
                "research_question": "How do variants change regulation?",
                "specific_aims": ["Aim 1", "Aim 2"], "approach": "MPRA and CRISPR screens",
                "methods_and_technologies": ["MPRA"], "model_systems": ["iPSC"],
                "research_area": ["genomics"], "outputs": ["catalog"], "keywords": ["enhancer"]})
        elif "ANALYST NOTES" in user or "GRANT DIGESTS" in user:
            ids = [w.strip("[]") for w in user.split() if w.startswith("[") and w.endswith("]")]
            content = f"## Portfolio overview\nCites {ids[0] if ids else ''} and [R01ZZ999999].\n"
        else:
            content = "### partial notes"
        self._send({"message": {"role": "assistant", "content": content}})


@pytest.fixture
def ollama():
    FakeOllama.calls = []
    server = HTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


def _grant_file(tmp_path):
    gf = tmp_path / "grants" / "po_test.json"
    build_grant_file(DATA / "raw_po.json", gf)
    check_grant_files([gf], [DATA / "raw_po.json"], tmp_path / "grants" / "manifest.json")
    return gf


def test_summarize_portfolio(tmp_path, ollama):
    gf = _grant_file(tmp_path)
    cfg = {"ollama": {"model": "fake-model", "port": ollama}, "max_chars_per_call": 60000}
    out = tmp_path / "summaries"
    res = summarize_portfolio(gf, out / "po_test.json", out / "po_test.md", cfg,
                              cache_dir=out / "cache", manifest=tmp_path / "grants" / "manifest.json")
    n = len(res["grant_digests"])
    assert n == 3
    assert all(d["approach"] == "MPRA and CRISPR screens" for d in res["grant_digests"])
    assert res["cited_ids_not_in_portfolio"] == ["R01ZZ999999"]
    md = (out / "po_test.md").read_text()
    assert "## Portfolio overview" in md and "R01ZZ999999" in md and "Appendix" in md
    assert len(FakeOllama.calls) == n + 1  # n digests + 1 synthesis

    # second run: digests come from the cache, only the synthesis is re-run
    FakeOllama.calls = []
    summarize_portfolio(gf, out / "po_test.json", out / "po_test.md", cfg,
                        cache_dir=out / "cache", manifest=tmp_path / "grants" / "manifest.json")
    assert len(FakeOllama.calls) == 1


def test_summarize_in_parts(tmp_path, ollama):
    gf = _grant_file(tmp_path)
    cfg = {"ollama": {"model": "fake-model", "port": ollama}, "max_chars_per_call": 10}
    out = tmp_path / "summaries"
    res = summarize_portfolio(gf, out / "a.json", out / "a.md", cfg)
    assert res["summary"]["synthesis_parts"] == 3
    # 3 digests + 3 partial notes + 1 final
    assert len(FakeOllama.calls) == 7


def test_summarize_refuses_unchecked_file(tmp_path, ollama):
    gf = _grant_file(tmp_path)
    gf.write_text(gf.read_text().replace('"schema_version": 1', '"schema_version": 1 '))
    cfg = {"ollama": {"model": "fake-model", "port": ollama}}
    with pytest.raises(ValueError, match="changed after it was checked"):
        summarize_portfolio(gf, tmp_path / "x.json", tmp_path / "x.md", cfg,
                            manifest=tmp_path / "grants" / "manifest.json")


def test_missing_model_and_server(ollama):
    with pytest.raises(LLMError, match="ollama pull other"):
        OllamaClient("other", port=ollama).check()
    with pytest.raises(LLMError, match="Cannot reach Ollama"):
        OllamaClient("x", port=1).check()


def test_parse_json_reply_and_chunks():
    assert parse_json_reply('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_reply('Sure! {"a": 2} hope that helps') == {"a": 2}
    assert chunk_blocks(["aaaa", "bbbb", "cc"], 9) == [["aaaa"], ["bbbb", "cc"]]
