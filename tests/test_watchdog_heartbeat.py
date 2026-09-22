"""A run that is working must be allowed to finish; one that is stuck must not.

Attempt 10 of the blind acceptance set ``run_isolated``'s ceiling equal to the
run's own limit, both 900 seconds. The supervisor therefore killed the
container before ``run_project`` wrote anything, and the run lost
``result.json``, ``report.md``, ``ledger.md``, ``delegation.md``,
``changes.diff`` and every vendor session file. The work survived only because
/work is a bind mount.

Raising the ceiling alone would trade that for a different problem: a wedged
container would then be waited on for even longer. So the ceiling and the
liveness check are separated -- the workload touches a heartbeat as it makes
progress, and a run that stops making progress is killed on the stall window
rather than on the ceiling.
"""
import os
import time

import pytest

from quadratus.isolated_run import run_isolated

DOCKER = pytest.mark.skipif(os.environ.get('QUADRATUS_TEST_DOCKER') != '1',
                            reason='Set QUADRATUS_TEST_DOCKER=1 for real container checks')


def _trees(tmp_path):
    work, runtime = tmp_path / 'work', tmp_path / 'runtime'
    work.mkdir()
    runtime.mkdir()
    (runtime / 'note.txt').write_text('nothing to run from here\n')
    return work, runtime


def _image():
    """The immutable ID of the local base image, as the other Docker tests do.

    check_output rather than a swallowed run: Docker Desktop intermittently
    loses this tag from its index, and a blank ID would otherwise surface as a
    confusing "use a locally inspected image ID" from deep inside the runner.
    """
    import subprocess
    return subprocess.check_output(
        ['docker', 'image', 'inspect', 'python:3.12-slim', '--format', '{{.Id}}'],
        text=True).strip()


# -- the argument contract --------------------------------------------------

def test_a_stall_window_without_a_heartbeat_is_refused(tmp_path):
    """Nothing to watch means nothing to conclude; guessing liveness is worse
    than not claiming it."""
    work, runtime = _trees(tmp_path)
    with pytest.raises(ValueError, match='needs a heartbeat'):
        run_isolated(image='sha256:' + '0' * 64, work=work, runtime=runtime,
                     command=['true'], stall_seconds=5)


@pytest.mark.parametrize('bad', [0, -1, float('nan'), float('inf'), True, 'soon'])
def test_a_nonsense_stall_window_is_refused(tmp_path, bad):
    work, runtime = _trees(tmp_path)
    with pytest.raises(ValueError, match='stall_seconds'):
        run_isolated(image='sha256:' + '0' * 64, work=work, runtime=runtime,
                     command=['true'], stall_seconds=bad, heartbeat=tmp_path / 'beat')


# -- and what it does against a real container ------------------------------

@DOCKER
def test_a_run_that_keeps_working_is_not_killed_by_the_stall_window(tmp_path):
    """The heartbeat advances, so the short stall window must never fire."""
    work, runtime = _trees(tmp_path)
    beat = work / 'budget.json'
    result = run_isolated(
        image=_image(), work=work, runtime=runtime,
        command=['/bin/sh', '-c',
                 'for i in 1 2 3 4 5 6; do echo $i > /work/budget.json; sleep 1; done; '
                 'echo done > /work/finished'],
        wall_seconds=120, heartbeat=beat, stall_seconds=4)
    assert result.outcome == 'success', result
    assert (work / 'finished').exists(), 'the workload must have run to completion'


@DOCKER
def test_a_run_that_stops_progressing_is_killed_on_the_stall_not_the_ceiling(tmp_path):
    """The point of the whole change: a wedged container dies promptly.

    The workload beats once and then sleeps far past the ceiling. Without the
    heartbeat the supervisor would wait the full ``wall_seconds``; with it, the
    run ends on the stall window instead, and says so.
    """
    work, runtime = _trees(tmp_path)
    beat = work / 'budget.json'
    started = time.monotonic()
    result = run_isolated(
        image=_image(), work=work, runtime=runtime,
        command=['/bin/sh', '-c', 'echo 1 > /work/budget.json; sleep 600'],
        wall_seconds=300, heartbeat=beat, stall_seconds=8)
    elapsed = time.monotonic() - started
    assert result.outcome == 'stalled', result
    assert result.idle_seconds is not None and result.idle_seconds >= 8
    assert elapsed < 120, f'a stalled run must not be waited out to the ceiling ({elapsed:.0f}s)'


@DOCKER
def test_partial_work_survives_a_stall_kill(tmp_path):
    """Same guarantee a wall_deadline already carried: the tree is evidence."""
    work, runtime = _trees(tmp_path)
    result = run_isolated(
        image=_image(), work=work, runtime=runtime,
        command=['/bin/sh', '-c',
                 'echo 1 > /work/budget.json; echo partial > /work/kept.txt; sleep 600'],
        wall_seconds=300, heartbeat=work / 'budget.json', stall_seconds=8)
    assert result.outcome == 'stalled'
    assert (work / 'kept.txt').read_text().strip() == 'partial'


@DOCKER
def test_without_a_heartbeat_the_old_behaviour_is_unchanged(tmp_path):
    """Every existing caller passes no heartbeat and must be unaffected."""
    work, runtime = _trees(tmp_path)
    result = run_isolated(image=_image(), work=work, runtime=runtime,
                          command=['/bin/sh', '-c', 'echo ok > /work/out'], wall_seconds=60)
    assert result.outcome == 'success' and result.idle_seconds is None
    assert (work / 'out').read_text().strip() == 'ok'


# -- liveness is not the same question as "wrote recently" ------------------

def test_a_callable_heartbeat_is_accepted_in_place_of_a_path(tmp_path):
    work, runtime = _trees(tmp_path)
    with pytest.raises(ValueError, match='stall_seconds'):
        run_isolated(image='sha256:' + '0' * 64, work=work, runtime=runtime,
                     command=['true'], stall_seconds=0, heartbeat=lambda: 1)


@DOCKER
def test_a_busy_run_that_writes_nothing_is_not_killed(tmp_path):
    """Attempt 11's false positive, as a test.

    The run writes its budget at each *call boundary*, so a healthy lead call
    -- measured at 228 to 263 seconds across three runs -- looks silent for
    minutes. Watching the file killed it at 240. The caller knows a call is
    outstanding, so it answers the liveness question instead.
    """
    work, runtime = _trees(tmp_path)
    busy = {'in_flight': 1}

    def liveness():
        # Never touches the filesystem: exactly the case a path cannot cover.
        return ('busy', time.monotonic()) if busy['in_flight'] else ('idle', 0)

    result = run_isolated(
        image=_image(), work=work, runtime=runtime,
        command=['/bin/sh', '-c', 'sleep 12; echo done > /work/finished'],
        wall_seconds=90, heartbeat=liveness, stall_seconds=4)
    assert result.outcome == 'success', result
    assert (work / 'finished').exists()


@DOCKER
def test_a_probe_that_raises_never_kills_a_live_run(tmp_path):
    """Our bug is not evidence about the workload; the ceiling is the backstop."""
    work, runtime = _trees(tmp_path)

    def broken():
        raise RuntimeError('probe is broken')

    result = run_isolated(image=_image(), work=work, runtime=runtime,
                          command=['/bin/sh', '-c', 'sleep 10; echo ok > /work/out'],
                          wall_seconds=90, heartbeat=broken, stall_seconds=3)
    assert result.outcome == 'success'
    assert (work / 'out').read_text().strip() == 'ok'


@DOCKER
def test_a_run_that_goes_idle_is_still_caught(tmp_path):
    """The detector must still do its job once the caller says it is idle."""
    work, runtime = _trees(tmp_path)

    def liveness():
        return ('idle', 0)

    result = run_isolated(image=_image(), work=work, runtime=runtime,
                          command=['/bin/sh', '-c', 'sleep 600'],
                          wall_seconds=200, heartbeat=liveness, stall_seconds=6)
    assert result.outcome == 'stalled'
    assert result.idle_seconds >= 6
