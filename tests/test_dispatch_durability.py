"""Independent dispatch and durability controls (root's cases on bcd2b52,
kept as written, with the engine's own additions). All providers are fake;
no native or model call."""
import json
import subprocess
from pathlib import Path

import pytest

from quadratus import checkpoint
from quadratus.config import Settings
from quadratus.delegation import DelegationLedger, invocation
from quadratus.providers import ProviderError
from quadratus.runtime import Fleet
from tests.conftest import write_codex_catalog
from tests.lifecycle import harness as H
from tests.lifecycle.test_effort_and_families import _effort, _script
from tests.lifecycle.test_lifecycle_matrix import FILES
from tests.lifecycle.test_proof_binding import _plan, _proofs, _scope
from tests.test_runtime import FakeCLIProvider


@pytest.fixture
def built(monkeypatch):
    made = {}

    def build(name, settings, **kw):
        if name not in made:
            made[name] = FakeCLIProvider(name)
        return made[name]
    monkeypatch.setattr('quadratus.runtime.build_provider', build)
    return made


# -- 1. an unknown or unusable effort ladder is not a vendor default -------------------------------

@pytest.mark.parametrize('efforts', [None, [], ['ultra'], ['future-unknown']])
def test_missing_or_unusable_model_ladder_is_not_a_vendor_default(tmp_path, monkeypatch, built, efforts):
    entry = {'slug': 'gpt-6.1-sol'}
    if efforts is not None:
        entry['supported_reasoning_levels'] = efforts
    p = write_codex_catalog(tmp_path / 'catalog.json', models=[entry])
    monkeypatch.setenv('QUADRATUS_CODEX_CATALOG', str(p))
    f = Fleet(Settings(backend='cli'))
    with invocation('a', 'auditor'):
        with pytest.raises(ProviderError, match="not usable"):
            f.invoke('openai:sol', 'audit')
    assert not built['openai'].calls
    assert not f.available('openai:sol')
    record = f.resolution_record()['resolutions']['openai:sol']
    assert record['status'] == 'unavailable' and ('no effort ladder' in record['reason']
                                                   or 'no usable effort' in record['reason'])


def test_a_pinned_slug_outside_the_catalogue_needs_a_declared_ladder(tmp_path, monkeypatch, built):
    monkeypatch.setenv('QUADRATUS_ALIAS_OPENAI_SOL', 'gpt-9-sol-preview')
    f = Fleet(Settings(backend='cli'))
    assert not f.available('openai:sol')
    reason = f.resolution_record()['resolutions']['openai:sol']['reason']
    assert 'QUADRATUS_EFFORT_LADDER_OPENAI_SOL' in reason and not built['openai'].calls
    monkeypatch.setenv('QUADRATUS_EFFORT_LADDER_OPENAI_SOL', 'low,medium,high,ultra')
    g = Fleet(Settings(backend='cli'))
    with invocation('a', 'auditor'):
        g.invoke('openai:sol', 'audit')
    assert built['openai'].calls[-1]['model'] == 'gpt-9-sol-preview'
    pinned = g.resolution_record()['resolutions']['openai:sol']
    assert pinned['supported'] == ['low', 'medium', 'high'], 'ultra dropped, the declaration recorded'
    assert pinned['provenance']['declared_in'] == 'QUADRATUS_EFFORT_LADDER_OPENAI_SOL'


# -- 2. one resolution per job, wherever it is first learned ----------------------------------------

def test_parallel_first_use_is_one_job_resolution_and_in_parent_record(tmp_path, monkeypatch, built):
    p = write_codex_catalog(tmp_path / 'catalog.json')
    monkeypatch.setenv('QUADRATUS_CODEX_CATALOG', str(p))
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'a.py').write_text('x=1\n')
    parent = Fleet(Settings(backend='cli'), project=project)
    left = parent.fork(project)
    right = parent.fork(project)
    a = left._resolution('openai:sol')
    write_codex_catalog(p, models=[{'slug': 'gpt-7-sol', 'supported_reasoning_levels': ['low', 'medium', 'high']}])
    b = right._resolution('openai:sol')
    assert a.wire == b.wire, (a.wire, b.wire)
    assert parent.resolution_record()['resolutions']['openai:sol']['wire'] == a.wire
    legacy = parent._resolution('openai:gpt-5.6-sol')
    assert legacy.wire == a.wire and legacy.key == 'openai:sol' and legacy.requested == 'openai:gpt-5.6-sol'
    assert set(parent.resolution_record()['resolutions']) == {'openai:sol'}, 'one family, one record'


# -- 3. the quiescent checkpoint carries the models the job called ----------------------------------

@pytest.mark.requirements_ledger
def test_quiescent_checkpoint_contains_the_models_the_job_actually_called(tmp_path, monkeypatch):
    replay = H.run(tmp_path, monkeypatch, _script(), files=FILES, max_tasks=1, record_complete=False)
    root = Path(replay.result.run_dir)
    result = json.loads((root / 'result.json').read_text())
    check = checkpoint.load(root)
    used = result['models']['resolutions']
    saved = check['state']['model_resolution']['resolutions']
    assert used and saved == used, (saved, used)


# -- 4. a spent family is spent under every spelling -------------------------------------------------

def test_legacy_spelling_cannot_bypass_exhausted_family(tmp_path, monkeypatch, built):
    f = Fleet(Settings(backend='cli'))
    f.mark_exhausted('openai:gpt-5.6-sol', 'window exhausted')
    assert not f.available('openai:sol') and not f.available('openai:gpt-5.6-sol')
    assert f.exhausted == {'openai:sol': 'window exhausted'}
    assert f.available('openai:luna'), 'one family, not the vendor'


def test_the_ledger_keeps_the_requested_spelling_of_a_family(tmp_path, built):
    ledger = DelegationLedger(path=tmp_path / 'ledger.jsonl')
    f = Fleet(Settings(backend='cli'), delegation_ledger=ledger)
    with invocation('t1', 'lead'):
        f.invoke('openai:gpt-5.6-sol', 'hello')
    row = [e for e in ledger.events if e.invoked][-1]
    assert (row.requested_model, row.canonical_model, row.wire_model) == ('openai:gpt-5.6-sol', 'openai:sol', 'gpt-6.1-sol')


# -- 5. risk outranks the size label -----------------------------------------------------------------

@pytest.mark.requirements_ledger
def test_security_kind_cannot_be_lowered_by_a_rote_label(tmp_path, monkeypatch):
    plan = _plan(_scope(proofs=_proofs('R1'))).replace('KIND: backend simple', 'KIND: security rote')
    script = _script()
    script.overrides['orchestrator'] = lambda c, r: plan if len(r.of('orchestrator')) == 1 else 'DONE'
    replay = H.run(tmp_path, monkeypatch, script, files=FILES, max_tasks=1, record_complete=False)
    leads = [c for c in replay.calls if c.role == 'lead']
    assert leads and all(_effort(c) == 'high' for c in leads), [(c.model, _effort(c)) for c in leads]


# -- 6. the bare factory path resolves the family too ------------------------------------------------

def test_direct_cli_factory_resolves_family_before_building_native_argv(tmp_path, monkeypatch):
    from quadratus.providers import build_provider
    monkeypatch.setattr('quadratus.cli_providers.shutil.which', lambda _: '/fake/codex')
    monkeypatch.setattr('quadratus.cli_providers.client_version', lambda *a: '0.163.0')
    provider = build_provider('openai', Settings(backend='cli'), workdir=tmp_path)
    try:
        argv = provider._build_argv('hello', '')
        assert argv[argv.index('--model') + 1] == 'gpt-6.1-sol', argv
    finally:
        provider.cleanup()


def test_direct_cli_factory_reports_an_unresolvable_family_instead_of_sending_its_name(tmp_path, monkeypatch):
    from quadratus.providers import build_provider
    monkeypatch.setattr('quadratus.cli_providers.shutil.which', lambda _: '/fake/codex')
    monkeypatch.setattr('quadratus.cli_providers.client_version', lambda *a: '0.163.0')
    monkeypatch.setenv('QUADRATUS_CODEX_CATALOG', str(tmp_path / 'absent.json'))
    provider = build_provider('openai', Settings(backend='cli'), workdir=tmp_path)
    try:
        assert not provider.available() and 'catalogue missing' in provider.status
    finally:
        provider.cleanup()


# -- 7. the retained closeout view records the effort it sends ---------------------------------------

def test_retained_closeout_view_records_the_effort_actually_sent(tmp_path, monkeypatch):
    from quadratus.cli_providers import GrokCLIProvider
    monkeypatch.setattr('quadratus.cli_providers.shutil.which', lambda _: '/fake/grok')
    monkeypatch.setattr('quadratus.cli_providers.client_version', lambda *a: '1.0.30')
    p = GrokCLIProvider('', workdir=tmp_path)
    monkeypatch.setattr('quadratus.runtime.build_provider', lambda *a, **kw: p)
    ledger = DelegationLedger(path=tmp_path / 'ledger.jsonl')
    f = Fleet(Settings(backend='cli'), delegation_ledger=ledger)
    sent = []

    def launch(argv, **kw):
        sent.append(argv[argv.index('--effort') + 1])
        return subprocess.CompletedProcess(argv, 0, json.dumps({'text': 'done', 'stopReason': 'end_turn',
                                                                 'usage': {'input_tokens': 3, 'output_tokens': 1}}), '')
    monkeypatch.setattr('quadratus.cli_providers._launch', launch)
    with invocation('t1', 'closeout'):
        f.invoke('grok:default', 'supplied record')
    assert sent == ['low'] and ledger.events[-1].effort == sent[-1], (sent, ledger.events[-1].effort)
    assert ledger.events[-1].effort_reasons == ['closeout summary view: fixed at low']


def test_a_probe_view_at_low_records_low(tmp_path, built):
    ledger = DelegationLedger(path=tmp_path / 'ledger.jsonl')
    f = Fleet(Settings(backend='cli'), delegation_ledger=ledger)
    with invocation('probe', 'auditor'):
        view = f.provider_for('claude:opus').for_seat('opus', effort='low')
        view.generate('probe')
    # The ledger row is written by the Fleet's _generate; a direct generate
    # on a view leaves no row, so check the view's own metadata instead.
    assert view.effort == 'low' and view.effort_decision.level == 'high'
    f._record_invocation('claude:opus', view, role='auditor', task='probe', origin='seat', seconds=0.0,
                         invoked=True, outcome='ok')
    row = ledger.events[-1]
    assert row.effort == 'low'
    assert row.effort_reasons == ["explicit view effort low; the seat's policy decision was high"]


# -- 8. the factory's family resolution never decides a vendor's transport availability ------------

def _fake_codex(monkeypatch):
    monkeypatch.setattr('quadratus.cli_providers.shutil.which', lambda _: '/fake/codex')
    monkeypatch.setattr('quadratus.cli_providers.client_version', lambda *a: '0.163.0')


def test_a_catalogue_without_the_default_family_leaves_the_other_families_usable(tmp_path, monkeypatch):
    """Root's isolation control on aab4fe7: real build_provider under the
    Fleet, a catalogue listing only Luna. Luna is usable, Sol is not, and
    the vendor's transport is available either way."""
    _fake_codex(monkeypatch)
    p = write_codex_catalog(tmp_path / 'catalog.json',
                            models=[{'slug': 'gpt-6-luna', 'supported_reasoning_levels': ['low', 'medium', 'high']}])
    monkeypatch.setenv('QUADRATUS_CODEX_CATALOG', str(p))
    fleet = Fleet(Settings(backend='cli'), project=tmp_path)
    try:
        assert fleet.available('openai:luna')
        assert not fleet.available('openai:sol')
        sol = fleet.resolution_record()['resolutions']['openai:sol']
        assert sol['status'] == 'unavailable' and sol['reason'] == 'no sol model in the catalogue'
        status = [line for line in fleet.status_lines() if line.startswith('openai')][0]
        assert 'unavailable' not in status.split(';')[0], status
        assert fleet._vendor_provider('openai').model in ('sol', 'luna'), 'the transport carries the tier name, never a resolved default'
    finally:
        fleet.close()


def test_a_held_family_survives_a_catalogue_that_later_drops_it(tmp_path, monkeypatch):
    _fake_codex(monkeypatch)
    p = write_codex_catalog(tmp_path / 'catalog.json')
    monkeypatch.setenv('QUADRATUS_CODEX_CATALOG', str(p))
    fleet = Fleet(Settings(backend='cli'), project=tmp_path)
    try:
        assert fleet.available('openai:luna') and fleet._resolution('openai:luna').wire == 'gpt-6-luna'
        p.unlink()
        child = fleet.fork(tmp_path)
        assert fleet.available('openai:luna') and child.available('openai:luna')
        assert child.provider_for('openai:luna').model == 'gpt-6-luna'
    finally:
        fleet.close()


def test_the_bare_factory_checks_the_catalogue_against_the_installed_client(tmp_path, monkeypatch):
    from quadratus.providers import build_provider
    _fake_codex(monkeypatch)
    p = write_codex_catalog(tmp_path / 'catalog.json', models=[
        {'slug': 'gpt-6.1-sol', 'supported_reasoning_levels': ['low', 'medium', 'high'], 'minimal_client_version': '0.170.0'},
        {'slug': 'gpt-6-sol', 'supported_reasoning_levels': ['low', 'medium', 'high']}])
    monkeypatch.setenv('QUADRATUS_CODEX_CATALOG', str(p))
    provider = build_provider('openai', Settings(backend='cli'), workdir=tmp_path)
    try:
        assert provider.available() and provider.model == 'gpt-6-sol', 'the release the installed client may use'
    finally:
        provider.cleanup()
    gated = build_provider('openai', Settings(backend='cli'), workdir=tmp_path, resolve_family=False)
    try:
        assert gated.available() and gated.model == 'sol', 'a Fleet binds the family itself'
    finally:
        gated.cleanup()
