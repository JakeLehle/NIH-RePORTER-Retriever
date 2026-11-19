"""Matchmaker selection + run tests. Offline: the PO fixture is a real Matchmaker
response (names and counts only) for a functional-genomics aims paragraph."""

import json
from pathlib import Path

import pandas as pd
import pytest

from nih_reporter import matchmaker
from nih_reporter.matchmaker import MatchmakerClient, MatchmakerError, knee_count, select_officers
from nih_reporter.queries import build_queries

DATA = Path(__file__).parent / "data"
PO = json.loads((DATA / "matchmaker_po.json").read_text())
COUNTS = [r["projects_counts"] for r in PO["results"]]


def test_real_distribution_shape():
    assert len(COUNTS) == 150 and sorted(COUNTS)[75] == 2 and COUNTS.count(1) == 73


@pytest.mark.parametrize("method,value,expected_t,expected_n", [
    ("knee", None, 4, 26),
    ("above_median", None, 3, 38),
    ("min_projects", 5, 5, 18),
    ("top_n", 10, 6, 13),      # 10th-ranked has 6; all officers tied at 6 are kept
    ("coverage", 0.5, 4, 26),  # top officers managing >= 50% of matched projects
])
def test_selection_methods(method, value, expected_t, expected_n):
    t, why = select_officers(COUNTS, method, value)
    assert t == expected_t, why
    assert sum(c >= t for c in COUNTS) == expected_n


def test_floor_and_cap():
    t, _ = select_officers(COUNTS, "top_n", 140, min_projects=2)
    assert t == 2  # floor removes singletons even if top_n would reach them
    t, why = select_officers(COUNTS, "knee", max_officers=5)
    assert sum(c >= t for c in COUNTS) <= 5 and "capped" in why
    with pytest.raises(ValueError):
        select_officers(COUNTS, "bogus")


def test_knee_edge_cases():
    assert knee_count([3, 3, 3]) == 3
    assert knee_count([]) == 0
    assert knee_count([50, 40, 2, 1, 1, 1, 1, 1, 1, 1]) in (2, 40)


class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload = status, payload
        self.text = text or json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, captcha=False):
        self.headers, self.calls, self.captcha = {}, [], captcha

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        if self.captcha:
            return FakeResponse(200, None, "<html>captcha</html>")
        if url.endswith("ProjectPO"):
            return FakeResponse(200, PO)
        assert json["limit"] == 500 and json["offset"] == 0
        page = [{"core_project_num": f"R01XX{i:06d}", "project_title": "t", "is_active": "Y"}
                for i in range(500)]
        return FakeResponse(200, {"meta": {"total": 500}, "results": page})


class FakeAPI:
    """Stands in for the RePORTER API: grant i is managed by officer i % 7, except
    that Matchmaker's top officer (Gilchrist) has handed every grant to 'New Officer'."""

    def __init__(self):
        self.calls = []

    def search(self, criteria):
        self.calls.append(criteria)
        assert criteria["include_active_projects"] is True
        res = []
        for core in criteria["project_nums"]:
            i = int(core[-6:])
            if i % 10 == 9:  # some similar grants are no longer active -> not returned
                continue
            name = ["NEW OFFICER", "MICHAEL J PAZIN", "AMANDA JOY PRICE", "ALISON Q YAO",
                    "ERIN EMILY GRAY", "LISA CHADWICK", "DOUGLAS S KIM"][i % 7]
            res.append({"appl_id": i, "core_project_num": core, "subproject_id": None, "award_type": "5",
                        "fiscal_year": 2026, "program_officers": [{"full_name": name}],
                        "agency_ic_admin": {"abbreviation": "NHGRI"}, "project_title": "t"})
        return {"results": res}


def test_run_matchmaker_current_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(matchmaker.time, "sleep", lambda s: None)
    session, api = FakeSession(), FakeAPI()
    client = MatchmakerClient(min_interval=0, session=session)
    stats = matchmaker.run_matchmaker("enhancer variants MPRA " * 20, tmp_path, client=client,
                                      api_client=api, group="Aims v1", method="top_n", value=3)
    payload = session.calls[0][1]
    assert payload["criteria"]["include_active_projects"] is True
    assert payload["criteria"]["text"].startswith("enhancer variants")
    assert len(session.calls) == 2               # PO table + all 500 similar projects
    assert len(api.calls) == 5                   # 500 grants looked up in batches of 100
    assert stats["similar_grants_resolved"] == 450

    ranked = pd.read_csv(tmp_path / "program_officers_all.tsv", sep="\t")
    top = ranked.set_index("name")
    # Gilchrist tops Matchmaker's list but manages none of the grants now
    assert "Daniel A Gilchrist" in stats["stale_matchmaker_officers"]
    assert "Daniel A Gilchrist" not in set(ranked.loc[ranked["selected"], "name"])
    assert top.loc["New Officer", "current_count"] > 0 and top.loc["New Officer", "matchmaker_count"] == 0
    assert top.loc["Michael J Pazin", "matchmaker_count"] == 26

    selected = pd.read_csv(tmp_path / "program_officers.tsv", sep="\t")
    assert len(selected) == 7  # all 7 current officers tie on ~64 grants; ties kept
    sim = pd.read_csv(tmp_path / "similar_projects.tsv", sep="\t")
    assert len(sim) == 500 and sim["currently_active"].sum() == 450
    assert "Daniel A Gilchrist" in (tmp_path / "selection.md").read_text()

    # the selected table is directly usable as a pipeline input
    specs = build_queries({"program_officers": str(tmp_path / "program_officers.tsv")})
    assert len(specs) == 7 and all(s.group == "Aims v1" for s in specs)
    assert {"first_name": "Michael", "last_name": "Pazin"} in [s.criteria["po_names"][0] for s in specs]


def test_run_matchmaker_website_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(matchmaker.time, "sleep", lambda s: None)
    client = MatchmakerClient(min_interval=0, session=FakeSession())
    stats = matchmaker.run_matchmaker("text", tmp_path, client=client, api_client=FakeAPI(),
                                      count_source="matchmaker")
    # duplicate Matchmaker rows (e.g. 'CHADWICK, LISA' / 'CHADWICK, LISA ') are merged
    ranked = pd.read_csv(tmp_path / "program_officers_all.tsv", sep="\t")
    assert len(ranked) == 136 and ranked["name"].is_unique
    assert ranked.loc[ranked["name"] == "Courtney Rose Pinard", "matchmaker_count"].item() == sum(
        r["projects_counts"] for r in PO["results"] if r["full_name"].startswith("PINARD, COURTNEY"))
    assert stats["stale_matchmaker_officers"] == []
    specs = build_queries({"program_officers": str(tmp_path / "program_officers.tsv")})
    assert specs[0].criteria["po_names"] == [{"first_name": "Daniel", "last_name": "Gilchrist"}]


def test_matchmaker_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(matchmaker.time, "sleep", lambda s: None)
    client = MatchmakerClient(min_interval=0, session=FakeSession(captcha=True))
    with pytest.raises(MatchmakerError, match="captcha"):
        matchmaker.run_matchmaker("some text", tmp_path, client=client, api_client=FakeAPI())
    with pytest.raises(MatchmakerError, match="15,000"):
        matchmaker.run_matchmaker("x" * 15001, tmp_path, client=client)
    with pytest.raises(MatchmakerError, match="empty"):
        matchmaker.run_matchmaker("   ", tmp_path, client=client)
