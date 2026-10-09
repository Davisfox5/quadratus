"""The Project UI carries the CLI's capture, further-check and readiness inputs,
and the GUI serves on a chosen, free port (Codex installation assessment on
4a273a3)."""

import socket

import pytest

from quadratus import gui
from quadratus.config import Settings


def test_the_ui_passes_the_capture_profile_further_checks_and_readiness_to_the_runner(tmp_path, monkeypatch):
    seen = {}

    class Result:
        report, diff, run_dir = "report", "", tmp_path

    def fake_run_project(goal, folder, settings, **kw):
        seen.update(kw)
        return Result()
    monkeypatch.setattr("quadratus.project_run.run_project", fake_run_project)
    outputs = list(gui.run_project_ui("goal", str(tmp_path), True, "pytest -q", "adversarial", 3, Settings(),
                                      capture_profile="/p/profile.json",
                                      extra_checks=["node --test tests/a.test.js"],
                                      readiness="/p/ready.json"))
    assert outputs[-1][0] == "report"
    assert seen["capture_profile"] == "/p/profile.json"
    assert seen["extra_checks"] == ("node --test tests/a.test.js",)
    assert seen["readiness"] == "/p/ready.json"
    seen.clear()
    list(gui.run_project_ui("goal", str(tmp_path), True, "", "adversarial", 3, Settings()))
    assert seen["capture_profile"] is None and seen["extra_checks"] == () and seen["readiness"] is None


def test_the_interface_has_the_capture_and_check_inputs():
    pytest.importorskip("gradio")
    demo = gui.build_interface(Settings())
    labels = {getattr(block, "label", None) for block in demo.blocks.values()}
    assert "Capture profile file (JSON, --capture-profile)" in labels
    assert "Further required checks (one command per line, --extra-check)" in labels
    assert "Readiness probes file (JSON, --readiness)" in labels


@pytest.mark.parametrize("argv, env, expected", [
    ([], None, 7860),
    (["--port", "8123"], None, 8123),
    ([], "8124", 8124),
    (["--port", "8125"], "8124", 8125),
])
def test_the_gui_port_comes_from_the_flag_then_the_environment(monkeypatch, argv, env, expected):
    monkeypatch.delenv("QUADRATUS_GUI_PORT", raising=False)
    if env:
        monkeypatch.setenv("QUADRATUS_GUI_PORT", env)
    assert gui.gui_port(argv) == expected


@pytest.mark.parametrize("argv", [["--port", "80"], ["--port", "abc"], ["--port"], ["--port", "70000"]])
def test_a_bad_port_is_refused_by_name(monkeypatch, argv):
    monkeypatch.delenv("QUADRATUS_GUI_PORT", raising=False)
    with pytest.raises(ValueError, match="--port must be a port number"):
        gui.gui_port(argv)


def test_a_busy_port_is_reported_with_the_way_out(monkeypatch, capsys):
    pytest.importorskip("gradio")
    monkeypatch.delenv("QUADRATUS_GUI_PORT", raising=False)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        if port < 1024:
            pytest.skip("ephemeral port below the allowed range")
        assert gui.main(["--port", str(port)]) == 2
    err = capsys.readouterr().err
    assert f"Port {port} on 127.0.0.1 is already in use" in err and "--port" in err


def test_a_file_given_as_the_project_folder_is_named_as_a_file(tmp_path):
    from quadratus.project import Project
    (tmp_path / "notes.txt").write_text("x")
    with pytest.raises(ValueError, match="is a file, not a folder"):
        Project(tmp_path / "notes.txt")
    with pytest.raises(ValueError, match="does not exist"):
        Project(tmp_path / "nowhere")


def test_a_missing_or_malformed_readiness_file_is_named(tmp_path):
    from quadratus.readiness import probes_from
    with pytest.raises(ValueError, match="readiness probes file could not be read: .*missing.json"):
        probes_from(str(tmp_path / "missing.json"), tmp_path)
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(ValueError, match="readiness probes file is not valid JSON: .*bad.json"):
        probes_from(str(tmp_path / "bad.json"), tmp_path)
