"""Offline tests: no network calls. Fixtures in tests/data are trimmed real
RePORTER responses."""

import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from nih_reporter import api
from nih_reporter.database import build_database
from nih_reporter.normalize import flatten_project
from nih_reporter.queries import build_queries, global_criteria
from nih_reporter.reports import write_reports

DATA = Path(__file__).parent / "data"
RAWS = [DATA / "raw_po.json", DATA / "raw_opportunity.json"]


def test_build_queries(tmp_path):
    (tmp_path / "po.tsv").write_text(
        "# comment\nfirst_name\tlast_name\tgroup\nMichael\tPazin\tG1\nJane\tO'Brien\t\n")
    (tmp_path / "opp.csv").write_text("opportunity_number,label\nrfa-hg-20-043,IGVF\n")
    (tmp_path / "txt.tsv").write_text("search_text\nIGVF\nIGVF\n")
    qs = build_queries(
        {"program_officers": "po.tsv", "opportunities": "opp.csv", "text_searches": "txt.tsv"},
        {"active_only": True, "activity_codes": ["R01"]}, base_dir=tmp_path)
    ids = [q.query_id for q in qs]
    # the duplicated IGVF row is dropped
    assert ids == ["po_pazin_michael", "po_o_brien_jane", "opp_rfa_hg_20_043", "txt_igvf"]
    po = qs[0]
    assert po.group == "G1"
    assert po.criteria == {"include_active_projects": True, "activity_codes": ["R01"],
                           "po_names": [{"first_name": "Michael", "last_name": "Pazin"}]}
    assert po.complete_grants is True
    assert po.filter_criteria() == {"include_active_projects": True, "activity_codes": ["R01"]}
    assert qs[2].criteria["foa"] == ["RFA-HG-20-043"]
    assert qs[3].criteria["advanced_text_search"]["search_text"] == "IGVF"


def test_build_queries_requires_input(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_queries({"opportunities": "missing.tsv"}, base_dir=tmp_path)
    with pytest.raises(ValueError):
        build_queries({}, base_dir=tmp_path)


def test_global_criteria_inactive():
    assert "include_active_projects" not in global_criteria({"active_only": False, "fiscal_years": [2024]})


def test_flatten_project():
    rec = json.loads((DATA / "raw_po.json").read_text())["results"][0]
    row = flatten_project(rec)
    assert row["core_project_num"] == rec["core_project_num"]
    assert row["program_officers"] == "Michael J Pazin"
    assert row["project_start_date"] and len(row["project_start_date"]) == 10


def test_build_database_and_reports(tmp_path):
    paths = build_database(RAWS, tmp_path / "db")
    records = pd.read_csv(paths["records"], sep="\t")
    portfolio = pd.read_csv(paths["portfolio"], sep="\t")
    assert records["appl_id"].is_unique
    assert portfolio["core_project_num"].is_unique

    # UM1HG012010 has a FY2025 main award and a FY2026 supplement (award type 3);
    # collapsed to one grant whose latest year / award / number come from the main award.
    g = portfolio.set_index("core_project_num").loc["UM1HG012010"]
    assert g["n_records"] == 2
    assert g["latest_fiscal_year"] == 2025
    assert g["latest_project_num"] == "5UM1HG012010-05"
    assert g["award_amount_latest_fy"] == 1890246

    jsonl = [json.loads(l) for l in paths["jsonl"].read_text().splitlines()]
    assert len(jsonl) == len(portfolio)
    assert all(r["title"] for r in jsonl)

    with sqlite3.connect(paths["sqlite"]) as con:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert {"queries", "records", "portfolio", "query_hits",
            "principal_investigators", "program_officers"} <= tables

    write_reports(tmp_path / "db", tmp_path / "reports")
    summary = pd.read_csv(tmp_path / "reports" / "program_officer_summary.tsv", sep="\t")
    pazin = summary.set_index("program_officer").loc["Michael J Pazin"]
    assert bool(pazin["queried_directly"])
    md = (tmp_path / "reports" / "portfolios" / "po_pazin_michael.md").read_text()
    assert "UM1HG012010" in md and "## Abstracts" in md


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.text = status, payload, json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Serves 1,200 fake records in pages, failing once with a 500 first."""

    def __init__(self, total=1200):
        self.total, self.calls, self.headers = total, [], {}

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        if len(self.calls) == 1:
            return FakeResponse(500, {"error": "boom"})
        off, lim = json["offset"], json["limit"]
        page = [{"appl_id": i} for i in range(off, min(off + lim, self.total))]
        return FakeResponse(200, {"meta": {"total": self.total, "search_id": "abc"}, "results": page})


def test_client_paging_and_retry(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    session = FakeSession()
    client = api.ReporterClient(min_interval=0, session=session)
    out = client.search({"foa": ["X"]})
    assert out["total"] == 1200
    assert [r["appl_id"] for r in out["results"]] == list(range(1200))
    assert [c["offset"] for c in session.calls] == [0, 0, 500, 1000]


def test_client_too_many(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    client = api.ReporterClient(min_interval=0, session=FakeSession(total=20000))
    with pytest.raises(api.TooManyResultsError):
        client.search({"foa": ["X"]})


def test_empty_query(tmp_path):
    raw = json.loads((DATA / "raw_opportunity.json").read_text())
    raw["results"], raw["total"] = [], 0
    raw["query"]["query_id"] = "opp_empty"
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps(raw))
    build_database([empty], tmp_path / "db")
    write_reports(tmp_path / "db", tmp_path / "reports")
    md = (tmp_path / "reports" / "portfolios" / "opp_empty.md").read_text()
    assert "No grants matched" in md
    # an empty query mixed with real ones must not break anything either
    build_database(RAWS + [empty], tmp_path / "db2")
    write_reports(tmp_path / "db2", tmp_path / "reports2")


def test_grant_file_and_check(tmp_path):
    from nih_reporter.landscape import build_grant_file, check_grant_files

    out = tmp_path / "grants"
    doc = build_grant_file(DATA / "raw_po.json", out / "po.json")
    raw = json.loads((DATA / "raw_po.json").read_text())
    assert doc["search"]["api_records"] == len(raw["results"])
    assert doc["overview"]["n_grants"] == len(doc["grants"]) == 3
    g = {x["core_project_num"]: x for x in doc["grants"]}["UM1HG012010"]
    assert len(g["records"]) == 2
    assert g["funding"]["latest_fiscal_year"] == 2025  # FY2026 is only a supplement
    assert g["funding"]["award_amount_latest_fy"] == 1890246
    assert g["funding"]["award_amount_by_fiscal_year"] == {"2025": 1890246, "2026": 1701223}
    assert g["abstract"] and g["terms"] and g["program_officers"] == ["Michael J Pazin"]
    # file is pretty-printed (human readable), not one long line
    assert len((out / "po.json").read_text().splitlines()) > 100

    build_grant_file(DATA / "raw_opportunity.json", out / "opp.json")
    manifest = check_grant_files([out / "po.json", out / "opp.json"],
                                 [DATA / "raw_po.json", DATA / "raw_opportunity.json"],
                                 out / "manifest.json")
    assert manifest["status"] == "ok" and manifest["n_files"] == 2
    assert (out / "manifest.json").exists()


def test_check_grant_files_fails(tmp_path):
    from nih_reporter.landscape import build_grant_file, check_grant_files

    good = tmp_path / "po.json"
    build_grant_file(DATA / "raw_po.json", good)
    broken = json.loads(good.read_text())
    broken["grants"].pop()  # now n_grants and record counts disagree
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(broken))
    manifest = tmp_path / "manifest.json"
    with pytest.raises(ValueError, match="n_grants"):
        check_grant_files([good, bad], None, manifest)
    with pytest.raises(ValueError, match="missing or empty"):
        check_grant_files([tmp_path / "nope.json"], None, manifest)
    assert not manifest.exists()


def test_parse_person_name_and_name_column(tmp_path):
    from nih_reporter.queries import parse_person_name

    assert parse_person_name("Pazin, Michael J") == ("Michael", "Pazin")
    assert parse_person_name("Michael J. Pazin") == ("Michael", "Pazin")
    assert parse_person_name("PAZIN, MICHAEL") == ("MICHAEL", "PAZIN")
    (tmp_path / "po.tsv").write_text("name\nPazin, Michael J\nMichael Pazin\n")
    qs = build_queries({"program_officers": "po.tsv"}, {"complete_grants": False}, base_dir=tmp_path)
    assert len(qs) == 1  # both spellings are the same PO
    assert qs[0].criteria["po_names"] == [{"first_name": "Michael", "last_name": "Pazin"}]
    assert qs[0].complete_grants is False


def test_groups_of(tmp_path):
    from nih_reporter.queries import groups_of

    (tmp_path / "opp.tsv").write_text("opportunity_number\tgroup\nRFA-1\tIGVF\nRFA-2\tIGVF\nRFA-3\tSolo\n")
    qs = build_queries({"opportunities": "opp.tsv"}, base_dir=tmp_path)
    assert groups_of(qs) == {"group_igvf": ["opp_rfa_1", "opp_rfa_2"]}  # single-query groups skipped


class LookupSession:
    """Search returns 2 records of grant A; lookup by project number returns 1 more."""

    def __init__(self):
        self.calls, self.headers = [], {}

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        crit = json["criteria"]
        if "project_nums" in crit:
            assert crit["project_nums"] == ["R01AA000001"]
            assert "foa" not in crit and crit["include_active_projects"] is True
            res = [{"appl_id": 2, "core_project_num": "R01AA000001"},
                   {"appl_id": 3, "core_project_num": "R01AA000001"}]
        else:
            res = [{"appl_id": 1, "core_project_num": "R01AA000001"},
                   {"appl_id": 2, "core_project_num": "R01AA000001"}]
        return FakeResponse(200, {"meta": {"total": len(res), "search_id": "s"}, "results": res})


def test_fetch_query_completes_grants(tmp_path, monkeypatch):
    from nih_reporter import pipeline
    from nih_reporter.queries import QuerySpec

    session = LookupSession()
    monkeypatch.setattr(pipeline, "ReporterClient",
                        lambda **kw: api.ReporterClient(min_interval=0, session=session))
    spec = QuerySpec("opp_x", "opportunity", "RFA-X", "RFA-X", "",
                     {"include_active_projects": True, "foa": ["RFA-X"]})
    pipeline.write_spec(spec, tmp_path / "spec.json")
    n = pipeline.fetch_query(tmp_path / "spec.json", tmp_path / "raw.json")
    raw = json.loads((tmp_path / "raw.json").read_text())
    assert n == 3
    assert raw["search_records"] == 2 and raw["grant_lookup_records"] == 1
    assert {r["appl_id"]: r["_found_by"] for r in raw["results"]} == {1: "search", 2: "search", 3: "grant_lookup"}


def test_group_grant_file(tmp_path):
    from nih_reporter.landscape import build_grant_file, check_grant_files

    doc = build_grant_file(RAWS, tmp_path / "group_test.json", group="Test group")
    assert doc["search"]["query_type"] == "group"
    assert len(doc["search"]["member_queries"]) == 2
    assert all("found_by_queries" in g for g in doc["grants"])
    m = check_grant_files([tmp_path / "group_test.json"], [RAWS], tmp_path / "manifest.json")
    assert m["files"][0]["portfolio_id"] == "group_test"


def test_supplement_does_not_define_latest_year():
    from nih_reporter.landscape import grant_entry

    base = {"core_project_num": "UM1HG000001", "subproject_id": None, "is_active": True,
            "project_title": "Main grant", "abstract_text": "main abstract"}
    recs = [
        {**base, "appl_id": 1, "fiscal_year": 2025, "award_type": "5", "award_amount": 1000,
         "project_num": "5UM1HG000001-05"},
        {**base, "appl_id": 2, "fiscal_year": 2026, "award_type": "3", "award_amount": 50,
         "project_num": "3UM1HG000001-05S1", "project_title": "Supplement", "abstract_text": "supp"},
    ]
    g = grant_entry(recs)
    assert g["title"] == "Main grant" and g["abstract"] == "main abstract"
    assert g["latest_project_num"] == "5UM1HG000001-05"
    assert g["funding"]["latest_fiscal_year"] == 2025
    assert g["funding"]["award_amount_latest_fy"] == 1000
    assert g["funding"]["award_amount_by_fiscal_year"] == {"2025": 1000, "2026": 50}
