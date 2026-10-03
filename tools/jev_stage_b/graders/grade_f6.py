"""f6-download-guard: path guard on /videos/<name> and /recordings/<name>."""
import logging
import os

import pytest
from support import run_baseline_regressions


def _outside_file(application, name="secret.txt", content=b"outside"):
    outside = os.path.join(str(application.__grader_data__), name)
    with open(outside, "wb") as handle:
        handle.write(content)
    return outside


def _assert_denied(rv, what):
    assert rv.status_code == 403, f"{what}: expected 403, got {rv.status_code}"
    body = rv.get_json(silent=True)
    assert isinstance(body, dict) and body.get("error") == "denied", f"{what}: body {rv.data[:200]!r}"


@pytest.mark.parametrize("prefix,dir_attr", [("/videos", "VIDEOS_DIR"), ("/recordings", "RECORDINGS_DIR")])
def test_R1_outside_paths_are_refused_with_403_denied(application, client, prefix, dir_attr):
    _outside_file(application)
    _assert_denied(client.get(f"{prefix}/../secret.txt"), f"{prefix} traversal")
    inside = os.path.join(getattr(application, dir_attr), "link.txt")
    os.symlink(_outside_file(application, "target.txt"), inside)
    _assert_denied(client.get(f"{prefix}/link.txt"), f"{prefix} symlink out")


@pytest.mark.parametrize("prefix,dir_attr", [("/videos", "VIDEOS_DIR"), ("/recordings", "RECORDINGS_DIR")])
def test_R2_every_refused_shape_reaches_the_guard(application, client, prefix, dir_attr):
    _outside_file(application)
    _assert_denied(client.get(f"{prefix}/../secret.txt"), f"{prefix} '..' segment")
    _assert_denied(client.get(f"{prefix}/%2e%2e/secret.txt"), f"{prefix} encoded dots")
    _assert_denied(client.get(f"{prefix}/sub/../../secret.txt"), f"{prefix} nested traversal")
    inside = os.path.join(getattr(application, dir_attr), "out.txt")
    os.symlink(_outside_file(application, "target2.txt"), inside)
    _assert_denied(client.get(f"{prefix}/out.txt"), f"{prefix} symlink out")
    link_dir = os.path.join(getattr(application, dir_attr), "dirlink")
    os.symlink(str(application.__grader_data__), link_dir)
    _assert_denied(client.get(f"{prefix}/dirlink/secret.txt"), f"{prefix} symlinked directory out")


def test_R3_a_refusal_is_logged_once_with_the_client_and_without_the_name(application, client, caplog):
    _outside_file(application, "logged-secret.txt")
    logger = application.app.logger
    caplog.set_level(logging.WARNING, logger=logger.name)
    logger.propagate = True
    rv = client.get("/videos/../logged-secret.txt", environ_base={"REMOTE_ADDR": "203.0.113.9"})
    _assert_denied(rv, "logged traversal")
    records = [r for r in caplog.records if r.levelno >= logging.WARNING and "denied" in r.getMessage().lower()]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    message = records[0].getMessage()
    assert "203.0.113.9" in message, message
    assert "logged-secret" not in message and ".." not in message, message


@pytest.mark.parametrize("prefix,dir_attr", [("/videos", "VIDEOS_DIR"), ("/recordings", "RECORDINGS_DIR")])
def test_R4_regular_files_are_served_and_missing_names_are_404(application, client, prefix, dir_attr):
    path = os.path.join(getattr(application, dir_attr), "ok.bin")
    payload = bytes(range(256)) * 4
    with open(path, "wb") as handle:
        handle.write(payload)
    rv = client.get(f"{prefix}/ok.bin")
    assert rv.status_code == 200, rv.status_code
    assert rv.data == payload
    rv = client.get(f"{prefix}/missing.bin")
    assert rv.status_code == 404, rv.status_code


def test_R5_baseline_tests_still_pass(project_root, tmp_path):
    failures = run_baseline_regressions(project_root, tmp_path)
    assert failures == [], "\n\n".join(failures)
