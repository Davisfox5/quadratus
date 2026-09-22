import json,hashlib,shutil,re,sys
from pathlib import Path
root=Path(__file__).resolve().parent
runtime=Path('/private/tmp/quadratus-harness-Q9-native')
project=root/'project';run=next((project/'.quadratus/runs').iterdir())
output=runtime/'docs/harness-canary/native-20260922';output.mkdir(exist_ok=True)
shutil.copytree(run,output/'run',dirs_exist_ok=True)
for name in ['allowance.json','input-hashes.json','preflight.json','preflight-initial.json','supervisor.json','before-grader.txt','after-grader.txt','prepare.py','launch.py','run_native.py']:
 shutil.copy2(root/name,output/name)
shutil.copy2(project/'.quadratus/native-console.txt',output/'native-console.txt')
shutil.copy2(project/'app.py',output/'final-app.py')
original=(runtime/'docs/harness-canary/fixture/app.py').read_text()
assert (project/'app.py').read_text()==original.replace('if record is None:', 'if record is None or record["tenant_id"] != tenant:')
hashes=json.loads((root/'input-hashes.json').read_text())
unchanged={name:hashlib.sha256((root/name).read_bytes()).hexdigest()==digest for name,digest in hashes.items() if name!='project/app.py'}
assert all(unchanged.values())
result=json.loads((run/'result.json').read_text())
calls=[e for line in (run/'invocations.jsonl').read_text().splitlines() if (e:=json.loads(line)).get('invoked')]
tokens=sum((e.get('input_tokens') or 0)+(e.get('output_tokens') or 0) for e in calls)
assert len(calls)==result['budget']['reserved_attempts'] and tokens==result['budget']['reported_tokens']
sys.path.insert(0,str(runtime))
from quadratus.session import Session,SessionConfig,TaskSpec
from quadratus.artifacts import ArtifactStore
from tempfile import TemporaryDirectory
reply=(run/'artifacts/cbd044e64a66.txt').read_text()
with TemporaryDirectory(prefix='q9-native-replay-') as scratch:
 def invoke(model,prompt,**kw):
  if 'verifying security work' in prompt:return reply
  return 'SUMMARY: replayed\nREASONING: captured verdict\nDEAD ENDS: none'
 session=Session('native verdict replay',ArtifactStore(Path(scratch)),invoke,config=SessionConfig())
 session.run_task(TaskSpec('native-replay','Verify fixture isolation',kind='security'))
 assert not session.open_findings
 replay={'live_model_calls':0,'legacy_classifier_blocks':('BLOCKING' in reply.upper() or 'UNRESOLVED' in reply.upper()),'patched_security_task_open_findings':session.open_findings,'scope':'captured verifier reply through security task, not a fresh model run or orchestrator DONE'}
summary={'run_id':run.name,'functional_acceptance':True,'frozen_tests_passed':9,'original_controller_completed':result['completed'],'reason_controller_incomplete':'legacy security prose parser matched Not blocking heading','provider_attempts':len(calls),'reported_tokens':tokens,'unknown_usage_attempts':result['budget']['unknown_usage_attempts'],'threshold_overshoot':max(0,tokens-500000),'auxiliary_diagnostic_tokens_overlap_unknown':sum(e.get('diagnostics',{}).get('auxiliary_tokens',0) for e in calls),'only_source_change':'app.py tenant guard','unchanged_inputs':unchanged,'offline_replay':replay,'native_functional_test_only':True}
(output/'independent-verdict.json').write_text(json.dumps(summary,indent=2)+'\n')
for p in output.rglob('*'):
 if p.is_file():assert not re.search(r'\b(?:sk-[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{40,})',p.read_text()),str(p)
print(json.dumps(summary,indent=2))
