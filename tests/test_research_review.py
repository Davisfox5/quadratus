import base64
import copy
import json
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from tools import research_review as inbox

DAY = "2026-10-16"


def pr():
    return {"number": 100, "state": "open", "draft": True,
            "title": f"AI research digest: {DAY}", "user": {"id": inbox.AUTHOR_ID},
            "base": {"ref": "main"},
            "head": {"ref": inbox.PREFIX + DAY, "sha": "a" * 40,
                     "repo": {"full_name": inbox.REPO}},
            "html_url": "https://github.com/Davisfox5/quadratus/pull/100"}


def encoded(value):
    return {"type": "file", "encoding": "base64",
            "content": base64.b64encode(json.dumps(value).encode()).decode()}


def test_digest_contract_retains_markdown_as_data():
    data = {"version": 1, "date": DAY, "markdown": "$(touch unexpected)\nhttps://arxiv.org/"}
    markdown, digest = inbox.decode_digest(encoded(data), DAY)
    assert markdown == data["markdown"]
    assert len(digest) == 64
    assert inbox.digest_date(pr()) == DAY


@pytest.mark.parametrize("field,value", [
    ("user", {"id": 123}), ("draft", False), ("state", "closed"),
    ("base", {"ref": "development"}), ("title", "unrelated PR"),
    ("head", {"ref": inbox.PREFIX + DAY, "repo": {"full_name": "attacker/quadratus"}}),
    ("head", {"ref": "research/digest-2026-99-99", "repo": {"full_name": inbox.REPO}}),
])
def test_untrusted_or_non_digest_pr_refused(field, value):
    candidate = pr()
    candidate[field] = value
    with pytest.raises(ValueError):
        inbox.digest_date(candidate)


@pytest.mark.parametrize("data", [
    {"version": True, "date": DAY, "markdown": "text"},
    {"version": 1, "date": "2026-10-09", "markdown": "text"},
    {"version": 1, "date": DAY, "markdown": ""},
    {"version": 1, "date": DAY, "markdown": "text", "command": "run me"},
    {"version": 1, "date": DAY, "markdown": "a" * 60_001},
])
def test_invalid_payload_refused(data):
    with pytest.raises(ValueError):
        inbox.decode_digest(encoded(data), DAY)


def test_only_three_research_paths_allowed():
    good = [{"filename": path, "status": "added"} for path in inbox.paths_for(DAY)]
    inbox.validate_files(good, DAY)
    for bad in ([{"filename": "quadratus/session.py", "status": "modified"}],
                [{"filename": inbox.paths_for(DAY)[0], "status": "removed"}], good[1:]):
        with pytest.raises(ValueError):
            inbox.validate_files(bad, DAY)


def test_publish_refuses_changed_head_before_mutation(monkeypatch):
    fresh = copy.deepcopy(pr())
    fresh["head"]["sha"] = "b" * 40
    calls = []

    def fake_api(path, *args, **kwargs):
        calls.append(path)
        return fresh

    monkeypatch.setattr(inbox, "api", fake_api)
    monkeypatch.setattr(inbox, "load_digest", lambda _: (DAY, "text", "digest"))
    with pytest.raises(RuntimeError, match="changed during review"):
        inbox.publish(pr(), DAY, "digest", {"report": "unused"})
    assert calls == ["pulls/100"]


def test_publish_atomically_updates_only_report_and_receipt(monkeypatch):
    calls = []

    def fake_api(path, payload=None, **kwargs):
        calls.append((path, payload, kwargs))
        if path == "pulls/100":
            return pr()
        if path.startswith("git/commits/"):
            return {"tree": {"sha": "tree0"}}
        return {"sha": "newsha"}

    monkeypatch.setattr(inbox, "api", fake_api)
    monkeypatch.setattr(inbox, "load_digest", lambda _: (DAY, "text", "digest"))
    inbox.publish(pr(), DAY, "digest", {"report": "report", "phase": "ready", "status": "incomplete"})
    tree = next(payload for path, payload, _ in calls if path == "git/trees")
    assert {entry["path"] for entry in tree["tree"]} == set(inbox.paths_for(DAY)[1:])
    assert calls[-1][1] == {"sha": "newsha", "force": False}


def prepare(monkeypatch):
    monkeypatch.setattr(inbox, "load_digest", lambda _: (DAY, "text", "digest"))
    monkeypatch.setattr(inbox, "api", lambda *a, **kw: [
        {"filename": inbox.paths_for(DAY)[0], "status": "added"}])
    monkeypatch.setattr(inbox, "command", lambda *a, **kw: "c" * 40)


def test_crashed_run_is_not_automatically_repeated(tmp_path, monkeypatch):
    prepare(monkeypatch)
    directory = tmp_path / "100-digest"
    directory.mkdir()
    inbox.save(directory / "review.json", {"phase": "running"})
    monkeypatch.setattr(inbox, "bounded_review", lambda *a: pytest.fail("duplicate model call"))
    inbox.process(pr(), tmp_path)


def test_failed_engine_publishes_incomplete_and_does_not_retry(tmp_path, monkeypatch):
    prepare(monkeypatch)
    calls = []

    def fail(*args):
        calls.append("engine")
        raise RuntimeError("not authenticated")

    published = []
    monkeypatch.setattr(inbox, "bounded_review", fail)
    monkeypatch.setattr(inbox, "publish", lambda *args: published.append(args[-1].copy()))
    inbox.process(pr(), tmp_path)
    inbox.process(pr(), tmp_path)
    assert calls == ["engine"]
    assert len(published) == 1
    assert published[0]["status"] == "incomplete"
    assert "not authenticated" in published[0]["report"]


def test_publication_retry_reuses_completed_result(tmp_path, monkeypatch):
    prepare(monkeypatch)
    calls = []
    monkeypatch.setattr(inbox, "bounded_review", lambda *a: calls.append("engine") or
                        ("assessment", True, {"verdict": "unverified"}))

    def publish(*args):
        calls.append("publish")
        if calls.count("publish") == 1:
            raise RuntimeError("network unavailable")

    monkeypatch.setattr(inbox, "publish", publish)
    with pytest.raises(RuntimeError):
        inbox.process(pr(), tmp_path)
    inbox.process(pr(), tmp_path)
    assert calls == ["engine", "publish", "publish"]


@pytest.mark.parametrize("completed,verdict,status", [
    (True, "verified", "review-completed"),
    (True, "unverified", "incomplete"),
    (True, "not-met", "incomplete"),
    (False, "verified", "incomplete"),
])
def test_status_requires_engine_and_independent_truth(tmp_path, monkeypatch, completed, verdict, status):
    prepare(monkeypatch)
    monkeypatch.setattr(inbox, "bounded_review", lambda *a: ("assessment", completed, {"verdict": verdict}))
    published = []
    monkeypatch.setattr(inbox, "publish", lambda *a: published.append(a[-1].copy()))
    inbox.process(pr(), tmp_path)
    assert published[0]["status"] == status
    assert published[0]["engine_completed"] == completed
    assert published[0]["truth_check"]["verdict"] == verdict


@pytest.mark.skipif(sys.platform != "darwin" and sys.platform != "linux", reason="POSIX process groups")
def test_outer_cleanup_stops_detached_vendor_group(tmp_path):
    # Real subprocesses, no models: a vendor double in its own session ignores TERM.
    marker = tmp_path / "vendor.json"
    vendor_code = ("import os,signal,time; "
                   "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                   f"open({str(marker)!r},'w').write(str(os.getpid())); "
                   "time.sleep(60)")
    engine_code = ("import subprocess,sys,time; "
                   f"subprocess.Popen([sys.executable,'-c',{vendor_code!r}],start_new_session=True); "
                   "time.sleep(60)")
    child = subprocess.Popen([sys.executable, "-c", engine_code], start_new_session=True)
    vendor = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        vendor = int(marker.read_text())
        inbox.stop_engine_tree(child, grace=.05)
        assert child.poll() is not None
        # A dead grandchild may briefly remain a zombie until the OS reaps it.
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(vendor)],
                               capture_output=True, text=True).stdout.strip()
        assert not state or state.startswith("Z")
    finally:
        for pid in (vendor, child.pid):
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
        child.wait(timeout=5)


def test_remote_receipt_prevents_repeat_after_reinstall(tmp_path, monkeypatch):
    prepare(monkeypatch)

    def fake_api(path, **kwargs):
        if "/files?" in path:
            return [{"filename": path, "status": "added"} for path in inbox.paths_for(DAY)]
        return encoded({"digest_sha256": "digest", "review_version": inbox.VERSION})

    monkeypatch.setattr(inbox, "api", fake_api)
    monkeypatch.setattr(inbox, "bounded_review", lambda *a: pytest.fail("duplicate model call"))
    inbox.process(pr(), tmp_path)


def test_real_engine_adapter_keeps_cli_transport_and_readonly_scope(tmp_path, monkeypatch):
    from quadratus import project_run
    from quadratus.session import OperatorInputNeeded
    from tools import truth_check

    observed = {}

    def fake_run(goal, project, settings, **kwargs):
        observed.update(kwargs)
        assert project.root != inbox.ROOT
        assert (project.root / "pyproject.toml").exists()
        assert all(settings.backend_for(name) == "cli" for name in ("claude", "openai", "grok"))
        assert settings.supplied_context_only is True
        assert settings.lead_max_turns == 4
        assert "BEGIN UNTRUSTED DIGEST DATA" in goal
        with pytest.raises(OperatorInputNeeded):
            kwargs["ask_operator"]("May I implement this?")
        return SimpleNamespace(report="assessment", completed=True, diff="", run_dir=tmp_path)

    monkeypatch.setenv("LLM_BACKEND", "api")
    monkeypatch.setenv("OPENAI_BACKEND", "api")
    monkeypatch.setattr(project_run, "run_project", fake_run)
    monkeypatch.setattr(truth_check, "judge", lambda *a, **kw: {"verdict": "unverified"})
    sha = inbox.command(["git", "rev-parse", "HEAD"]).strip()
    report, complete, truth = inbox.run_review(DAY, "paper", sha, tmp_path)
    assert report == "assessment" and complete
    assert truth["verdict"] == "unverified"
    assert observed["allow_writes"] is False
    assert observed["decider"] == "rule"
    assert observed["run_limits"].max_calls == 36
    assert observed["run_limits"].reserve_tokens_per_call == 50_000
    assert observed["run_limits"].max_tokens_per_call == 150_000
    assert observed["run_limits"].max_reported_tokens == 750_000


def test_retry_preserves_previous_receipt_and_runs_once(tmp_path, monkeypatch):
    prepare(monkeypatch)
    directory = tmp_path / "100-digest"
    directory.mkdir()
    previous = {"phase": "published", "status": "incomplete", "report": "old outcome"}
    inbox.save(directory / "review.json", previous)
    calls = []
    monkeypatch.setattr(inbox, "bounded_review", lambda *a: calls.append("engine") or
                        ("new assessment", False, {"verdict": "not-met"}))
    monkeypatch.setattr(inbox, "publish", lambda *a: calls.append("publish"))
    inbox.process(pr(), tmp_path, retry=True)
    inbox.process(pr(), tmp_path)
    assert calls == ["engine", "publish"]
    preserved = list((directory / "attempts").glob("*/review.json"))
    assert len(preserved) == 1 and json.loads(preserved[0].read_text()) == previous


def test_explicit_retry_cannot_replace_a_running_assessment(tmp_path, monkeypatch):
    prepare(monkeypatch)
    directory = tmp_path / "100-digest"
    directory.mkdir()
    inbox.save(directory / "review.json", {"phase": "running"})
    monkeypatch.setattr(inbox, "bounded_review", lambda *a: pytest.fail("duplicate call"))
    with pytest.raises(RuntimeError, match="running"):
        inbox.process(pr(), tmp_path, retry=True)
