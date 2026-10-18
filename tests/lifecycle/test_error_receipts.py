"""Root's independent controls on 17a96d3 (#73 comment 6206049011), kept
verbatim in substance: real checks and real scratch execution below the
vendor launch, scripted verdicts. A pass is controller policy evidence, not
LLM reliability. Generic fixture; nothing from any live job.

- an explicit error contract is never discharged by prose, an invented
  "receipt" or a waiver;
- a real negative scratch receipt is necessary and still not a verdict: the
  independent final audit still decides (good executed), and failed or
  skipped proofs never verify;
- an unrelated passing scratch does not stand for a requirement its MET
  line does not cite;
- an uncollected case never borrows a same-named case's execution;
- (added) a passing scratch proof on an earlier source is not credited at
  the final audit;
- (root on 5ca254c) a proof is bound to the source its copy was handed, so
  a live change during the review leaves it stale, never relabelled; and
  the unchanged quiet own-report gate credits the exact cases its report
  names as passed.

The four scratch cases run twice (root on 8e98697: CI's Linux runner has no
/usr/bin/bwrap). ``host``: the real sandbox where this host has one, with
every positive, negative, unrelated and stale assertion; where it has none,
the truthful refusal. ``forced-off``: the sandbox made unavailable on any
host. A refused scratch run is ``not run``, names no case, carries no
receipt and is never acceptance; nothing here skips.
"""
# Root's compact layout is kept as written (one-line statements).
# ruff: noqa: E701, E702
import json

import pytest

from quadratus.config import Settings
from tests.lifecycle import harness as H
from tests.lifecycle.test_lifecycle_matrix import Script
from tests.lifecycle.test_step2_evidence import _ROOT_LIMITS

# The fixtures here run under the default-shaped bound (_ROOT_LIMITS,
# 2,000,000 tokens): under admission/2 the known stages are paid from what
# is left and a correction is admitted when a finding selects it.

pytestmark = pytest.mark.requirements_ledger

NO_SANDBOX = 'not run: no sandbox here holds a scratch run to its copy'


def _host_sandbox() -> bool:
    import os
    import platform
    return ((platform.system() == 'Linux' and os.path.isfile('/usr/bin/bwrap'))
            or (platform.system() == 'Darwin' and os.path.exists('/usr/bin/sandbox-exec')))


@pytest.fixture(params=['host', 'forced-off'])
def sandbox(request, monkeypatch):
    """True when the scratch run really executes in a sandbox here."""
    if request.param == 'forced-off':
        monkeypatch.setattr('quadratus.runtime._scratch_isolation', lambda directory, private: None)
        return False
    return _host_sandbox()


def _refused(proof):
    """The truthful no-sandbox record: not run, no case, no receipt."""
    return (proof.get('status') == 'not run' and f"not run: {proof.get('reason')}" == NO_SANDBOX
            and proof.get('cases') == [] and proof.get('receipts') == [])

GOAL = ('Implement parse_switch(text): accept only the strings "on" and "off", returning '
        '{"enabled": true} and {"enabled": false}. Every other string and every non-string '
        'input must return {"error": "invalid"}, never an escaping exception. Preserve the original tests.')
REQ = ('parse_switch returns the enabled object for on/off, and returns an error object for '
       'invalid strings and non-string inputs without an escaping exception.')
BAD = 'def parse_switch(text):\n    return {"enabled": text.lower() == "on"}\n'
GOOD = ('def parse_switch(text):\n    if not isinstance(text, str) or text not in ("on", "off"):\n'
        '        return {"error": "invalid"}\n    return {"enabled": text == "on"}\n')
ORIGINAL = ('from switches import parse_switch\ndef test_on():\n'
            '    assert parse_switch("on") == {"enabled": True}\n')
SCOPE = dict(permitted_paths=['switches.py'], intended_result=REQ, acceptance=[REQ], max_lines=40)
PLAN = ('REQUIREMENTS:\nR1: '+REQ+'\nKIND: architect complex\nSCOPE: '+json.dumps(SCOPE)
        +'\nImplement switch decoding.\nCOVERS: R1')

@pytest.mark.parametrize('claim', ['prose', 'invented-execution', 'waived'])
def test_explicit_error_contract_cannot_be_discharged_without_executed_current_proof(tmp_path, monkeypatch, claim):
    def lead(call, replay):
        H.write(call, {'switches.py': BAD})
        return 'Implemented switch decoding.\nCHANGED: ["switches.py"]'
    narrative = {
        'prose': 'Read the implementation; malformed inputs are handled.',
        'invented-execution': 'I executed invalid strings and non-string checks and all passed. Receipt: TESTED R1 PASS.',
        'waived': 'Invalid inputs are outside normal use, so waive the explicit error clauses as not applicable.',
    }[claim]
    def approved(call, replay):
        return narrative+'\nR1: MET - switches.py\nREVIEW: COMPLETE'
    replay = H.run(tmp_path, monkeypatch,
        Script(orchestrator=lambda c,r: PLAN if not r.of('lead') else 'DONE',
               lead=lead, collaborator=approved, auditor=approved,
               **{'requirements-review': lambda c,r:'COMPLETE'}),
        files={'switches.py':GOOD, 'tests/test_switches.py':ORIGINAL, 'README.md':'# Switch parser\n'},
        goal=GOAL, max_tasks=1,
        limits=_ROOT_LIMITS,
        settings=Settings(backend='cli'))
    # Independently execute the delivered implementation to establish the real
    # negative-path defect, even though its approved positive check passes.
    env={};exec((replay.project/'switches.py').read_text(), env)
    assert env['parse_switch']('on') == {'enabled':True}
    assert env['parse_switch']('broken') != {'error':'invalid'}
    with pytest.raises(AttributeError):env['parse_switch'](None)
    assert (replay.project/'tests/test_switches.py').read_text() == ORIGINAL
    print(json.dumps({'claim':claim, 'completed':replay.result.completed,
        'error':replay.result.error, 'roles':[c.role for c in replay.calls],
        'scratch':H.result_json(replay).get('scratch_proofs'),
        'audits':(H.result_json(replay).get('requirements') or {}).get('audits')}))
    assert not replay.result.completed, 'unsupported model MET discharged an explicit broken original error contract'

NEGATIVE = ('from switches import parse_switch\n'
            'def test_invalid_string_error():\n    assert parse_switch("broken") == {"error":"invalid"}\n'
            'def test_nonstring_error():\n    assert parse_switch(None) == {"error":"invalid"}\n')

@pytest.mark.parametrize('case', ['good-executed', 'broken-failed', 'good-skipped'])
def test_real_negative_receipt_is_necessary_but_not_an_automatic_verdict(tmp_path, monkeypatch, case, sandbox):
    source = BAD if case == 'broken-failed' else GOOD
    scratch = ('import pytest\n@pytest.mark.skip(reason="unavailable")\n'+NEGATIVE
               if case == 'good-skipped' else NEGATIVE)
    # Put skip markers on both actual functions without changing the good case.
    if case == 'good-skipped':
        scratch = 'import pytest\n'+NEGATIVE.replace('def test_', '@pytest.mark.skip(reason="unavailable")\ndef test_')
    def lead(call, replay):
        H.write(call, {'switches.py':source})
        return 'Implemented switch decoding.\nCHANGED: ["switches.py"]'
    def peer(call, replay):
        H.write(call, {'tests/test_error_contract.py':scratch})
        return ('R1: MET - switches.py and tests/test_error_contract.py::test_invalid_string_error '
                'and tests/test_error_contract.py::test_nonstring_error; invalid strings and non-string errors demonstrated.\nREVIEW: COMPLETE')
    def auditor(call, replay):
        return ('R1: MET - switches.py; tests/test_error_contract.py::test_invalid_string_error '
                'and tests/test_error_contract.py::test_nonstring_error demonstrate explicit error paths.')
    replay = H.run(tmp_path, monkeypatch,
        Script(orchestrator=lambda c,r:PLAN if not r.of('lead') else 'DONE', lead=lead,
               collaborator=peer, auditor=auditor, **{'requirements-review':lambda c,r:'COMPLETE'}),
        files={'switches.py':GOOD,'tests/test_switches.py':ORIGINAL,'README.md':'# Switch parser\n'},
        goal=GOAL,max_tasks=1,
        limits=_ROOT_LIMITS,
        settings=Settings(backend='cli'))
    result = H.result_json(replay)
    proofs = result.get('scratch_proofs') or []
    assert proofs, 'a measured scratch receipt must remain on record'
    assert not (replay.project/'tests/test_error_contract.py').exists(), 'review evidence escaped its disposable copy'
    assert (replay.project/'tests/test_switches.py').read_text()==ORIGINAL
    print(json.dumps({'case':case,'completed':replay.result.completed,'error':replay.result.error,
        'scratch':proofs,'roles':[c.role for c in replay.calls],
        'audits':(result.get('requirements') or {}).get('audits')}))
    if not sandbox:
        assert all(_refused(p) for p in proofs),proofs
        assert not replay.result.completed, 'a scratch run that did not execute was treated as verified'
        assert (result['requirements']['audits'] or [{}])[-1].get('verdicts',{}).get('R1',{}).get('met') is not True
        return
    if case=='good-executed':
        assert any(p.get('status')=='passed' for p in proofs),proofs
        assert replay.result.completed,replay.result.error
        assert len(replay.of('auditor'))==1, 'an executed proof still requires the independent final audit'
    else:
        assert not replay.result.completed, 'failed or skipped proof was treated as verified error behavior'

def test_unrelated_passing_scratch_does_not_discharge_an_explicit_error_review(tmp_path,monkeypatch,sandbox):
    seen=[]
    def lead(c,r):
        H.write(c,{'switches.py':BAD});return 'Implemented switch.\nCHANGED: ["switches.py"]'
    def peer(c,r):
        H.write(c,{'tests/test_doc_only.py':'from pathlib import Path\ndef test_doc_title():\n    assert Path("README.md").read_text().startswith("# Switch")\n'})
        # The actual executed scratch claim is explicitly documentation-only.
        return 'Documentation scratch test tests/test_doc_only.py::test_doc_title passed; error handling was read only.\nR1: MET - switches.py\nREVIEW: COMPLETE'
    def revise(c,r):seen.append(c.prompt);return 'Nothing changed.\nCHANGED: []'
    replay=H.run(tmp_path,monkeypatch,Script(orchestrator=lambda c,r:PLAN if not r.of('lead') else 'DONE',lead=lead,collaborator=peer,revision=revise,auditor=lambda c,r:'R1: NOT MET - switches.py invalid strings and nonstrings fail.',**{'requirements-review':lambda c,r:'COMPLETE'}),files={'switches.py':GOOD,'tests/test_switches.py':ORIGINAL,'README.md':'# Switch parser\n'},goal=GOAL,max_tasks=1,settings=Settings(backend='cli'))
    proofs=H.result_json(replay).get('scratch_proofs') or []
    if sandbox:
        assert proofs and any(p.get('status')=='passed' for p in proofs),proofs
    else:
        assert proofs and all(_refused(p) for p in proofs),proofs
    notes=replay.artifact_texts('review:openai:gpt-5.6-sol')+replay.artifact_texts('review:grok:grok-4.3')
    print(json.dumps({'case':'unrelated-doc-scratch','notes':notes,'scratch':proofs,'revision_prompts':seen}))
    assert any('R1: UNVERIFIED' in n for n in notes), 'unrelated doc-only scratch was treated as proof of error handling'


def test_uncollected_case_cannot_borrow_execution_of_same_name_from_another_file(tmp_path):
    import dataclasses  # noqa: F811
    import sys

    from quadratus.artifacts import ArtifactStore
    from quadratus.integration import IntegrationGate
    from quadratus.session import Session, SessionConfig, _gate_identity
    root=tmp_path/'project';(root/'tests').mkdir(parents=True)
    (root/'app.py').write_text('def decode(x):\n    return x\n')
    (root/'tests/test_real.py').write_text('def test_rejects_invalid():\n    assert True\n')
    # Existing but not collected: pytest never runs the class lacking Test prefix.
    (root/'tests/test_uncollected.py').write_text('class Hidden:\n    def test_rejects_invalid(self):\n        raise AssertionError("unexecuted negative path")\n')
    gate=IntegrationGate([sys.executable,'-m','pytest','-q','--quadratus-report={report}'],cwd=root)
    s=Session('Invalid inputs return an error object.',ArtifactStore(tmp_path/'artifacts'),lambda *a,**k:'',config=SessionConfig(project=root,integration_gate=gate))
    result=gate.run();assert result.passed,result.output
    record=dict(task='t1',source=s._source_fingerprint(),passed=result.passed,gate=_gate_identity(gate),command=result.command,output=result.output,report=result.report,cases=result.cases,receipts=[dataclasses.asdict(r) for r in result.receipts])
    s.checks=[record]
    cited='tests/test_uncollected.py::test_rejects_invalid'
    found=s._executed_cases([cited])
    print(json.dumps({'case':'same-name-uncollected','actual':dataclasses.asdict(result),'incorrectly_accepted':found}))
    assert found==[], 'unexecuted negative test borrowed same-named positive execution from another file'

def test_a_scratch_proof_on_an_earlier_source_is_not_credited_at_the_final_audit(tmp_path, monkeypatch, sandbox):
    """The good-executed shape, then a second task changes README.md after
    the review's scratch proof passed: the proof's handed source is no
    longer the source the audit judges, so the cited scratch cases do not
    count and R1 is NOT MET (stale). R2 is not an error clause."""
    plan2 = ('KIND: docs simple\nSCOPE: ' + json.dumps(dict(permitted_paths=['README.md'],
             intended_result='README documents parse_switch', acceptance=['README names parse_switch'],
             max_lines=20)) + '\nDocument parse_switch.\nCOVERS: R2')
    plan1 = PLAN.replace('REQUIREMENTS:\nR1: ' + REQ, 'REQUIREMENTS:\nR1: ' + REQ + '\nR2: README documents parse_switch')

    def orchestrator(c, r):
        n = len(r.of('orchestrator'))
        return {1: plan1, 2: plan2}.get(n, 'DONE')

    def lead(call, replay):
        if call.task == 't2':
            H.write(call, {'README.md': '# Switch parser\n\nparse_switch("on") enables.\n'})
            return 'Documented.\nCHANGED: ["README.md"]'
        H.write(call, {'switches.py': GOOD})
        return 'Implemented switch decoding.\nCHANGED: ["switches.py"]'

    def peer(call, replay):
        if call.task == 't2':
            return 'NO FINDINGS\nR2: MET - README.md'
        H.write(call, {'tests/test_error_contract.py': NEGATIVE})
        return ('R1: MET - switches.py and tests/test_error_contract.py::test_invalid_string_error '
                'and tests/test_error_contract.py::test_nonstring_error\nREVIEW: COMPLETE')

    def auditor(call, replay):
        return ('R1: MET - switches.py; tests/test_error_contract.py::test_invalid_string_error and '
                'tests/test_error_contract.py::test_nonstring_error\nR2: MET - README.md')
    replay = H.run(tmp_path, monkeypatch,
                   Script(orchestrator=orchestrator, lead=lead, collaborator=peer, auditor=auditor,
                          **{'requirements-review': lambda c, r: 'COMPLETE'}),
                   files={'switches.py': GOOD, 'tests/test_switches.py': ORIGINAL, 'README.md': '# Switch parser\n'},
                   goal=GOAL, max_tasks=2, record_complete=False, settings=Settings(backend='cli'))
    result = H.result_json(replay)
    t1 = [p for p in result['scratch_proofs'] if p.get('task') == 't1']
    if sandbox:
        assert any(p.get('status') == 'passed' for p in t1), t1
    else:
        assert t1 and all(_refused(p) for p in t1), t1
    verdicts = result['requirements']['audits'][-1]['verdicts']
    assert verdicts['R1']['met'] is False and verdicts['R2']['met'] is True
    assert not replay.result.completed


def test_source_change_while_scratch_copy_is_reviewed_cannot_be_relabelled_as_current_proof(tmp_path,monkeypatch,sandbox):
    changed=[]
    def lead(c,r):
        H.write(c,{'switches.py':GOOD});return 'Implemented.\nCHANGED: ["switches.py"]'
    def peer(c,r):
        H.write(c,{'tests/test_error_contract.py':NEGATIVE})
        if not changed:
            # Simulated OTHER producer, not a permission granted to a native
            # reviewer: its handed copy remains good while live source moves.
            (r.project/'switches.py').write_text(BAD);changed.append(True)
        return 'R1: MET - tests/test_error_contract.py::test_invalid_string_error and tests/test_error_contract.py::test_nonstring_error\nREVIEW: COMPLETE'
    reply='R1: MET - switches.py; tests/test_error_contract.py::test_invalid_string_error and tests/test_error_contract.py::test_nonstring_error'
    replay=H.run(tmp_path,monkeypatch,Script(orchestrator=lambda c,r:PLAN if not r.of('lead') else 'DONE',lead=lead,collaborator=peer,auditor=lambda c,r:reply,**{'requirements-review':lambda c,r:'COMPLETE'}),files={'switches.py':GOOD,'tests/test_switches.py':ORIGINAL,'README.md':'# Switch parser\n'},goal=GOAL,max_tasks=1,settings=Settings(backend='cli'))
    assert changed and (replay.project/'switches.py').read_text()==BAD
    env={};exec(BAD,env);assert env['parse_switch']('broken')!={'error':'invalid'}
    data=H.result_json(replay)
    print(json.dumps({'case':'changed-during-review','completed':replay.result.completed,'error':replay.result.error,'scratch':data.get('scratch_proofs'),'audits':data['requirements'].get('audits')}))
    assert not replay.result.completed,'old handed-source proof was stamped after the source changed and accepted for broken current code'
    # Its raw passing result is kept; it is stale, never relabelled current.
    (proof,)=data['scratch_proofs']
    assert proof['status']==('passed' if sandbox else 'not run') and proof['eligible'] is False
    assert sandbox or _refused(proof)
    assert proof['stale']=='the source moved after the copy was handed'


def test_actual_declared_quiet_check_can_credit_the_negative_cases_it_executed(tmp_path,monkeypatch):
    # Same -q and own-report declaration as the production pytest gate.
    # No verbosity or check population changes may be imposed on that job.
    scope=dict(permitted_paths=['switches.py','tests/test_errors.py'],intended_result=REQ,acceptance=[REQ],max_lines=60)
    first='REQUIREMENTS:\nR1: '+REQ+'\nR2: README documents parse_switch.\nKIND: backend simple\nSCOPE: '+json.dumps(scope)+'\nImplement and test switch parsing.\nCOVERS: R1'
    second='KIND: docs simple\nSCOPE: '+json.dumps(dict(permitted_paths=['README.md'],intended_result='README documents parse_switch',acceptance=['README names function'],max_lines=20))+'\nDocument parsing.\nCOVERS: R2'
    def planner(c,r):return first if len(r.of('orchestrator'))==1 else second if len(r.of('orchestrator'))==2 else 'DONE'
    def lead(c,r):
        if c.task=='t1':
            H.write(c,{'switches.py':GOOD,'tests/test_errors.py':NEGATIVE});return 'Implemented and tested.\nCHANGED: ["switches.py", "tests/test_errors.py"]'
        H.write(c,{'README.md':'# Switch parser\n\nparse_switch validates on/off.\n'});return 'Documented.\nCHANGED: ["README.md"]'
    met='R1: MET - tests/test_errors.py::test_invalid_string_error and tests/test_errors.py::test_nonstring_error'
    replay=H.run(tmp_path,monkeypatch,Script(orchestrator=planner,lead=lead,collaborator=lambda c,r:met+'\nREVIEW: COMPLETE' if c.task=='t1' else 'R2: MET - README.md\nREVIEW: COMPLETE',auditor=lambda c,r:met+'\nR2: MET - README.md',**{'requirements-review':lambda c,r:'COMPLETE'}),files={'switches.py':BAD,'tests/test_switches.py':ORIGINAL,'README.md':'# Switch parser\n'},goal=GOAL+' Document parse_switch in README.',max_tasks=2,settings=Settings(backend='cli'))
    data=H.result_json(replay);assert data['checks'] and all(c['passed'] for c in data['checks'])
    assert (replay.project/'tests/test_errors.py').read_text()==NEGATIVE
    print(json.dumps({'case':'declared-quiet-proof','completed':replay.result.completed,'error':replay.result.error,'checks':data['checks'],'audits':data['requirements'].get('audits')}))
    assert replay.result.completed,'actual passing negative cases from the unchanged declared quiet check have no identity route to final acceptance'
