# Codex on #35 (comment 5923216190): shared-budget reproducer, kept verbatim.
"""Independent shared-budget reproductions; SDK and CLI replies are scripted."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quadratus.decisions import JevDecider
from quadratus.run_budget import RunLimits
from tests.lifecycle import harness as H
from tests.lifecycle.test_decider import UNLABELLED, _lead
from tests.lifecycle.test_lifecycle_matrix import FILES, Script


@pytest.mark.parametrize('boundary', ['calls', 'tokens'])
def test_jev_respects_shared_run_boundary(tmp_path, monkeypatch, boundary):
    sdk_calls = []
    class SDK:
        def system_one(self, **request):
            sdk_calls.append(request)
            name = next(iter(request['questions']))
            label = 'docs' if name == 'task_kind' else 'rote'
            return SimpleNamespace(model='jev-1.13.0',
                usage=SimpleNamespace(input_tokens=300, output_tokens=0),
                choices={name:SimpleNamespace(choice=label,confidence=1.0,probabilities={label:1.0})})
    client = SDK()
    monkeypatch.setattr(JevDecider, 'available', lambda self: '')
    monkeypatch.setattr(JevDecider, '_sdk_client', lambda self: client)
    limits = RunLimits(max_calls=1 if boundary == 'calls' else 120,
                       max_reported_tokens=1 if boundary == 'tokens' else 6_000_000,
                       wall_seconds=600,max_concurrent_workers=2)
    replay = H.run(tmp_path, monkeypatch, Script(lead=_lead), files=FILES,
                   max_tasks=1, limits=limits, tasks=[UNLABELLED], decider='jev')
    run = Path(replay.result.run_dir)
    receipt = {'boundary':boundary,'sdk_calls':len(sdk_calls),'cli_calls':len(replay.calls),
               'budget':json.loads((run/'budget.json').read_text()),'error':replay.result.error}
    print(json.dumps(receipt))
    # Either boundary must reject a second call after the first SDK response.
    assert len(sdk_calls) + len(replay.calls) <= 1, receipt
