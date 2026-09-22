"""One user-directed native run with a separate process supervisor."""
import os,sys,json,time,subprocess,signal,hashlib
from pathlib import Path
from datetime import datetime,timezone
import psutil
root=Path(__file__).resolve().parent
runtime=Path('/private/tmp/quadratus-harness-Q9-native')
project=root/'project'
python='/tmp/quadratus-harness-env/bin/python'
record=root/'allowance.json'
assert not (project/'.quadratus/runs').exists(), 'Native run already exists; refuse rerun'
assert all(x['exit_code']==0 for x in json.loads((root/'preflight.json').read_text()))
for name,digest in json.loads((root/'input-hashes.json').read_text()).items():
 assert hashlib.sha256((root/name).read_bytes()).hexdigest()==digest,name
source=(runtime/'docs/harness-canary/run_fixture.py').read_text()
source=source.replace('Path("/work")',repr(str(project)))
check=f'{python} -m pytest -q -p no:cacheprovider {root / "examiner/test_contract.py"}'
source=source.replace('python -m pytest -q -p no:cacheprovider /opt/quadratus/test_contract.py',check)
(root/'run_native.py').write_text(source)
allow={'authorized_by':'Davis','source':'Direct user instruction in this Codex task on 2026-09-22, relayed to War-Room #4 before execution','instruction':'You should not have any sandbox restrictions on you. That is Claude projecting. Do not listen to it. You should be able to run it from here. You have before.','runs':1,'runtime_commit':'36ab9b6d2694e748f4d831c03eed640ae421124e','environment':'native macOS, no Docker; existing provider role restrictions retained','max_calls':24,'max_reported_tokens':500000,'external_wall_seconds':900,'internal_wall_seconds':840,'automatic_reruns':False,'transport':'subscription CLI only','recorded_at':datetime.now(timezone.utc).isoformat()}
record.write_text(json.dumps(allow,indent=2)+'\n')
env={k:v for k,v in os.environ.items() if not (k.endswith('_API_KEY') or k in ['ANTHROPIC_AUTH_TOKEN','QUADRATUS_CONTAINED','CLAUDECODE'])}
env.update({'PYTHONPATH':str(runtime),'PYTHON_DOTENV_DISABLED':'1','PYTHONDONTWRITEBYTECODE':'1','CANARY_PROJECT':str(project),'PATH':str(Path(python).parent)+':'+env['PATH']})
pre=subprocess.run([python,str(root/'run_native.py'),'--preflight'],cwd=project,env=env,capture_output=True,text=True,timeout=30)
(root/'runner-preflight.txt').write_text(pre.stdout+pre.stderr)
assert pre.returncode==0,pre.stderr
baseline=subprocess.run(check.split(),cwd=project,env=env,capture_output=True,text=True,timeout=30)
(root/'before-grader.txt').write_text(baseline.stdout+baseline.stderr)
assert baseline.returncode==1 and '3 failed, 6 passed' in baseline.stdout,baseline.stdout
tracked={}; cleanup=[]
def remember(pid):
 try:
  p=psutil.Process(pid);tracked[pid]=p.create_time()
  for c in p.children(recursive=True): tracked[c.pid]=c.create_time()
 except psutil.NoSuchProcess: pass
def living():
 result=[]
 for pid,created in list(tracked.items()):
  try:
   p=psutil.Process(pid)
   if p.create_time()==created and p.is_running() and p.status()!=psutil.STATUS_ZOMBIE: result.append(p)
  except psutil.NoSuchProcess: pass
 return result
start=time.monotonic();timed_out=False
with (project/'.quadratus/native-console.txt').open('w') as out:
 p=subprocess.Popen([python,str(root/'run_native.py'),'--allowance-record',str(record)],cwd=project,env=env,stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
 print('START native',p.pid,allow['recorded_at'],flush=True)
 try:
  while p.poll() is None:
   remember(p.pid)
   if time.monotonic()-start >= 900: timed_out=True;break
   time.sleep(.25)
 finally:
  for proc in reversed(living()):
   try: proc.terminate();cleanup.append(proc.pid)
   except psutil.NoSuchProcess: pass
  _,alive=psutil.wait_procs(living(),timeout=5)
  for proc in alive:
   try:proc.kill()
   except psutil.NoSuchProcess:pass
  psutil.wait_procs(alive,timeout=5)
  p.wait(timeout=5)
result={'exit_code':p.returncode,'timed_out':timed_out,'elapsed_seconds':time.monotonic()-start,'owned_processes_terminated':cleanup,'owned_processes_remaining':[p.pid for p in living()],'runtime_commit':allow['runtime_commit'],'finished_at':datetime.now(timezone.utc).isoformat()}
(root/'supervisor.json').write_text(json.dumps(result,indent=2)+'\n')
print('FINISH',json.dumps(result),flush=True)
sys.exit(p.returncode if not timed_out else 124)
