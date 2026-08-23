"""Tests for the product map: partitioning, graph, survey, staleness, prompts.

The design under test: no project work on an existing codebase until it has
been surveyed into a verified reference; the reading is parallel across
vendors; every section is verified across vendor lines; sections go stale by
fingerprint the moment their code changes; nothing is paid for twice.
"""

from __future__ import annotations

import threading

import pytest

from multi_llm.artifacts import ArtifactStore
from multi_llm.product_map import (
    ProductMap,
    build_units,
    import_graph,
    run_survey,
    survey_estimate,
)
from multi_llm.registry import resolve
from multi_llm.session import Session, SessionConfig


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


def _repo(tmp_path):
    root = tmp_path / "proj"
    (root / "core").mkdir(parents=True)
    (root / "api").mkdir()
    (root / "core" / "engine.py").write_text("def run():\n    return 1\n")
    (root / "core" / "state.py").write_text("STATE = {}\n")
    (root / "api" / "server.py").write_text(
        "from core import engine\n\ndef serve():\n    return engine.run()\n"
    )
    (root / "main.py").write_text("from api import server\nserver.serve()\n")
    return root


def _survey(root, store, pm, script=None, calls=None):
    calls = calls if calls is not None else []

    def invoke(model, prompt):
        calls.append({"model": model, "prompt": prompt})
        if "You are its verifier" in prompt or "verifier: re-read" in prompt:
            return "VERIFIED"
        if "Write the product map's overview" in prompt:
            return "WHAT THIS SYSTEM IS: a demo.\nARCHITECTURE: core+api."
        return (script or {}).get(model) or (
            "PURPOSE: does the thing.\nKEY COMPONENTS: engine.run.\n"
            "PUBLIC INTERFACES: run().\nBEHAVIOUR & DATA FLOW: simple.\n"
            "CONVENTIONS: none.\nRISKS & GOTCHAS: none seen.\nTESTS: UNKNOWN"
        )

    return run_survey(root, invoke=invoke, store=store, product_map=pm), calls


# -- partitioning and the mechanical graph -------------------------------------


def test_units_follow_directories_and_root_files(tmp_path):
    units = build_units(_repo(tmp_path))
    names = [u.name for u in units]
    assert set(names) == {"core", "api", "(root)"}
    core = next(u for u in units if u.name == "core")
    assert len(core.files) == 2 and core.lines > 0


def test_an_oversized_area_is_split_before_surveying(tmp_path):
    root = tmp_path / "big"
    (root / "pkg" / "sub_a").mkdir(parents=True)
    (root / "pkg" / "sub_b").mkdir()
    big = "x = 1\n" * 2000
    (root / "pkg" / "sub_a" / "a.py").write_text(big)
    (root / "pkg" / "sub_b" / "b.py").write_text(big)
    names = [u.name for u in build_units(root)]
    assert "pkg" not in names
    assert "pkg/sub_a" in names and "pkg/sub_b" in names


def test_the_dependency_graph_is_parsed_from_the_code(tmp_path):
    root = _repo(tmp_path)
    units = build_units(root)
    graph = import_graph(root, units)
    assert "core" in graph["api"]        # api/server.py imports core
    assert "api" in graph["(root)"]      # main.py imports api
    assert graph["core"] == []           # core imports nothing internal


def test_fingerprints_change_only_when_the_code_does(tmp_path):
    root = _repo(tmp_path)
    before = {u.name: u.fingerprint for u in build_units(root)}
    (root / "core" / "engine.py").write_text("def run():\n    return 2\n")
    after = {u.name: u.fingerprint for u in build_units(root)}
    assert before["core"] != after["core"]
    assert before["api"] == after["api"]


# -- the survey ----------------------------------------------------------------


def test_survey_writes_verified_sections_and_an_overview(tmp_path, store):
    root = _repo(tmp_path)
    pm = ProductMap(tmp_path / "pm.json", root=root,
                    md_path=tmp_path / "product_map.md")
    written, calls = _survey(root, store, pm)
    assert written == 3
    assert set(pm.sections) == {"core", "api", "(root)"}
    assert pm.overview and "WHAT THIS SYSTEM IS" in pm.overview["content"]
    # Reader and verifier never share a vendor: the Blitzy property.
    for name, section in pm.sections.items():
        a, v = resolve(section["author"]), resolve(section["verifier"])
        assert a is not None and v is not None
        assert a.provider != v.provider, name
    # The reviewable document exists on disk with every section.
    md = (tmp_path / "product_map.md").read_text()
    assert "## core" in md and "## Overview" in md
    # Full sections are fetchable artifacts.
    assert all(store.get(s["artifact"]) for s in pm.sections.values())


def test_resurvey_pays_only_for_what_changed(tmp_path, store):
    root = _repo(tmp_path)
    pm = ProductMap(tmp_path / "pm.json", root=root)
    first, _ = _survey(root, store, pm)
    assert first == 3
    again, calls = _survey(root, store, pm, calls=[])
    assert again == 0 and calls == []    # unchanged repo costs nothing
    (root / "api" / "server.py").write_text("def serve():\n    return 9\n")
    third, calls = _survey(root, store, pm, calls=[])
    assert third == 1                    # only the changed area is re-read
    reader_prompts = [c for c in calls if "surveying one area" in c["prompt"]]
    assert len(reader_prompts) == 1 and "api" in reader_prompts[0]["prompt"]


def test_verifier_corrections_are_appended_with_attribution(tmp_path, store):
    root = _repo(tmp_path)
    pm = ProductMap(tmp_path / "pm.json", root=root)

    def invoke(model, prompt):
        if "You are its verifier" in prompt:
            return "- run() returns 2 -> it returns 1"
        if "Write the product map's overview" in prompt:
            return "WHAT THIS SYSTEM IS: a demo."
        return "PURPOSE: engine.\nKEY COMPONENTS: run() returns 2."

    run_survey(root, invoke=invoke, store=store, product_map=pm)
    core = pm.sections["core"]["content"]
    assert "VERIFIER CORRECTIONS" in core
    assert "it returns 1" in core


def test_survey_cost_estimate_is_two_calls_per_area_plus_overview():
    assert survey_estimate(0) == 0
    assert survey_estimate(5) == 11


def test_survey_reads_areas_in_parallel(tmp_path, store):
    root = _repo(tmp_path)
    pm = ProductMap(tmp_path / "pm.json", root=root)
    barrier = threading.Barrier(3, timeout=10)

    def invoke(model, prompt):
        if "surveying one area" in prompt:
            barrier.wait()  # all three readers must be live at once
        if "verifier" in prompt:
            return "VERIFIED"
        if "overview" in prompt:
            return "WHAT THIS SYSTEM IS: parallel."
        return "PURPOSE: x."

    assert run_survey(root, invoke=invoke, store=store, product_map=pm) == 3


# -- living reference: staleness and prompt presence ---------------------------


def test_a_changed_file_marks_its_section_stale_everywhere(tmp_path, store):
    root = _repo(tmp_path)
    pm = ProductMap(tmp_path / "pm.json", root=root,
                    md_path=tmp_path / "product_map.md")
    _survey(root, store, pm)
    assert pm.stale_names() == []
    (root / "core" / "state.py").write_text("STATE = {'changed': True}\n")
    assert pm.stale_names() == ["core"]
    pm.refresh()
    block = pm.render_block()
    assert "core [STALE]" in block
    assert "do not rely" in block.lower() or "resurvey" in block
    assert "STALE" in pm.render_markdown()


def test_the_map_rides_in_orchestrator_and_lead_prompts(tmp_path, store):
    root = _repo(tmp_path)
    pm = ProductMap(tmp_path / "pm.json", root=root)
    _survey(root, ArtifactStore(tmp_path / "survey-artifacts"), pm)

    prompts = []

    def rec(model, prompt, system=None):
        prompts.append(prompt)
        if "Name the next wave of tasks" in prompt:
            return "TASK general simple: tidy the api"
        if "The task is finished" in prompt:
            return "SUMMARY: tidied\nREASONING: fine"
        return f"[{model}] output"

    s = Session("Improve the demo", store, rec,
                config=SessionConfig(product_map=pm))
    s.run_task(s.next_wave()[0])
    orch = next(p for p in prompts if "Name the next wave of tasks" in p)
    lead = next(p for p in prompts if "You are leading this task" in p)
    for prompt in (orch, lead):
        assert "Product map (the standing codebase reference)" in prompt
        assert "FETCH" in prompt


def test_sections_are_fetchable_through_the_session_channel(tmp_path, store):
    root = _repo(tmp_path)
    pm = ProductMap(tmp_path / "pm.json", root=root)
    _survey(root, store, pm)  # sections stored in the session's own store
    artifact_id = pm.sections["core"]["artifact"]

    state = {"fetched": False}

    def rec(model, prompt, system=None):
        if "Name the next wave of tasks" in prompt:
            if f"artifact {artifact_id}" in prompt and not state["fetched"]:
                state["fetched"] = True
                return f"FETCH: {artifact_id}"
            return "DONE"
        return f"[{model}] output"

    s = Session("Improve the demo", store, rec,
                config=SessionConfig(product_map=pm))
    assert s.next_wave() is None
    assert state["fetched"]  # the index led to a real full-text fetch


# -- from scratch: the map is designed, then earned ----------------------------


def _design(tmp_path, store, review="NO CONCERNS"):
    from multi_llm.product_map import draft_design

    root = tmp_path / "empty-proj"
    root.mkdir()
    pm = ProductMap(tmp_path / "pm.json", root=root,
                    md_path=tmp_path / "product_map.md")
    calls = []

    def invoke(model, prompt):
        calls.append({"model": model, "prompt": prompt})
        if "designated skeptic" in prompt:
            return review
        return (
            "WHAT THIS SYSTEM IS: a recipe box.\n"
            "ARCHITECTURE: storage, search, ui.\nKEY FLOWS: add; find.\n"
            "BUILD, TEST, RUN: pytest.\nRISKS: scope creep.\n"
            "OPEN DESIGN QUESTIONS: none."
        )

    draft_design("Build a recipe box.", invoke=invoke, store=store,
                 product_map=pm)
    return pm, calls, root


def test_a_from_scratch_map_is_designed_and_cross_vendor_reviewed(tmp_path, store):
    pm, calls, _root = _design(tmp_path, store,
                               review="- search is underspecified")
    assert pm.overview["origin"] == "design"
    assert "DESIGN REVIEW" in pm.overview["content"]
    assert "search is underspecified" in pm.overview["content"]
    architect = resolve(calls[0]["model"])
    reviewer = resolve(calls[1]["model"])
    assert architect.provider != reviewer.provider
    md = (tmp_path / "product_map.md").read_text()
    assert "design — written before the code" in md
    assert store.get(pm.overview["artifact"])  # the design is a real artifact


def test_a_clean_design_review_is_not_appended(tmp_path, store):
    pm, _calls, _root = _design(tmp_path, store, review="NO CONCERNS")
    assert "DESIGN REVIEW" not in pm.overview["content"]


def test_a_design_only_map_still_rides_in_prompts(tmp_path, store):
    pm, _calls, _root = _design(tmp_path, store)
    block = pm.render_block()
    assert "Product map (the standing codebase reference)" in block
    assert "approved design" in block
    assert "recipe box" in block


def test_newly_built_areas_are_flagged_until_surveyed(tmp_path, store):
    pm, _calls, root = _design(tmp_path, store)
    (root / "storage").mkdir()
    (root / "storage" / "db.py").write_text("RECIPES = []\n")
    pm.refresh()
    block = pm.render_block()
    assert "not yet surveyed" in block
    assert "storage" in block


# -- one document: design + order + progress -----------------------------------


def test_the_design_prompt_asks_for_a_build_order(tmp_path, store):
    _pm, calls, _root = _design(tmp_path, store)
    assert "BUILD ORDER" in calls[0]["prompt"]


def test_plan_and_progress_live_in_the_same_document(tmp_path, store):
    pm, _calls, _root = _design(tmp_path, store)
    ref = store.put("1. storage\n2. search\n3. ui", kind="build-plan",
                    author="orchestrator")
    pm.set_plan(content="1. storage\n2. search\n3. ui",
                author="orchestrator", artifact_id=ref.id)
    pm.record_progress("t1", "built the storage layer\nwith sqlite")
    pm.record_progress("t2", "added search")
    md = (tmp_path / "product_map.md").read_text()
    assert "## Build plan" in md and "2. search" in md
    assert "## Build progress" in md
    assert "- t1: built the storage layer" in md  # first line only
    assert "sqlite" not in md.split("## Build progress")[1]
    block = pm.render_block()
    assert "Build plan" in block and f"artifact {ref.id}" in block


def test_progress_is_recorded_by_the_harness_as_tasks_close(tmp_path, store):
    pm, _calls, _root = _design(tmp_path, store)

    def rec(model, prompt, system=None):
        if "Name the next wave of tasks" in prompt:
            return rec.waves.pop(0) if rec.waves else "DONE"
        if "The task is finished" in prompt:
            return "SUMMARY: shipped the storage layer\nREASONING: fine"
        return f"[{model}] output"
    rec.waves = ["TASK general simple: build storage", "DONE"]

    s = Session("Build a recipe box", store, rec,
                config=SessionConfig(product_map=pm))
    s.run()
    assert pm.progress == [{"task": "t1", "note": "shipped the storage layer"}]
    assert "shipped the storage layer" in (tmp_path / "product_map.md").read_text()


def test_refresh_runs_after_every_wave(tmp_path, store):
    class FakeMap:
        def __init__(self):
            self.refreshes = 0
        def render_block(self):
            return "## Product map (the standing codebase reference)\n- x"
        def refresh(self):
            self.refreshes += 1

    fake = FakeMap()

    def rec(model, prompt, system=None):
        if "Name the next wave of tasks" in prompt:
            return rec.waves.pop(0) if rec.waves else "DONE"
        if "The task is finished" in prompt:
            return "SUMMARY: ok\nREASONING: ok"
        return f"[{model}] output"
    rec.waves = ["TASK general simple: a", "TASK general simple: b", "DONE"]

    s = Session("Goal", store, rec, config=SessionConfig(product_map=fake))
    s.run()
    assert fake.refreshes == 2  # once per executed wave
