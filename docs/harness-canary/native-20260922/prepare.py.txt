import os,json,subprocess,sys,hashlib,re
from pathlib import Path
root=Path(__file__).resolve().parent
runtime=Path('/private/tmp/quadratus-harness-Q9-native')
project=root/'project'; project.mkdir(exist_ok=True)
(project/'.quadratus').mkdir(exist_ok=True)
examiner=root/'examiner';examiner.mkdir(exist_ok=True)
source=runtime/'docs/harness-canary'
assert not (project/'.quadratus/runs').exists(), 'Do not prepare over a live run'
for name in ['app.py','README.md']:
    data=(source/'fixture'/name).read_text()
    if name=='README.md': data=data.replace('/opt/quadratus/test_contract.py',str(examiner/'test_contract.py')).replace('python -m pytest', '/tmp/quadratus-harness-env/bin/python -m pytest')
    (project/name).write_text(data)
(examiner/'test_contract.py').write_bytes((source/'test_contract.py').read_bytes())
policy=json.loads((source/'policy.json').read_text())
policy['gates'][1]['argv'][0]='/tmp/quadratus-harness-env/bin/python'
policy['gates'][1]['argv'][-1]=str(examiner/'test_contract.py')
(project/'.quadratus/policy.json').write_text(json.dumps(policy,indent=2)+'\n')
env={k:v for k,v in os.environ.items() if not (k.endswith('_API_KEY') or k in ['OPENAI_API_KEY','ANTHROPIC_AUTH_TOKEN','QUADRATUS_CONTAINED','CLAUDECODE'])}
env['PYTHON_DOTENV_DISABLED']='1';env['PYTHONDONTWRITEBYTECODE']='1'
env['PATH']='/tmp/quadratus-harness-env/bin:'+env['PATH']
os.environ.clear();os.environ.update(env)
sys.path.insert(0,str(runtime))
from quadratus.cli_providers import CLI_SPECS
results=[]
for mode in ['read-only','workspace-write']:
    command=['codex','sandbox','-c','sandbox_mode='+mode,'--','/bin/cat',str(project/'app.py')]
    p=subprocess.run(command,cwd=project,env=env,capture_output=True,text=True,timeout=30)
    results.append({'kind':'native-read','mode':mode,'argv':command,'exit_code':p.returncode,'matches_fixture':p.stdout== (project/'app.py').read_text(),'stderr':p.stderr[-500:]})
probe=project/'.quadratus/native-write-probe'
command=['codex','sandbox','-c','sandbox_mode=workspace-write','--','/bin/sh','-c','printf native-write-ok > .quadratus/native-write-probe']
p=subprocess.run(command,cwd=project,env=env,capture_output=True,text=True,timeout=30)
results.append({'kind':'native-write','argv':command,'exit_code':p.returncode,'verified':probe.exists() and probe.read_text()=='native-write-ok','stderr':p.stderr[-500:]})
probe.unlink(missing_ok=True)
for vendor,spec in CLI_SPECS.items():
    command=[spec.resolved_binary(env),*(spec.auth_check_args if spec.auth_check_args else ['auth','status'])]
    p=subprocess.run(command,cwd=project,env=env,capture_output=True,text=True,timeout=30)
    output=p.stdout+'\n'+p.stderr
    if vendor=='claude': output='logged in' if json.loads(p.stdout).get('loggedIn') else 'not signed in'
    ok=p.returncode==0 and (not spec.auth_ok_pattern or bool(re.search(spec.auth_ok_pattern,output))) and (not spec.auth_failure_pattern or not re.search(spec.auth_failure_pattern,output))
    results.append({'kind':'auth-status','vendor':vendor,'argv':command,'exit_code':p.returncode,'signed_in':ok})
checks={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [project/'app.py',project/'README.md',project/'.quadratus/policy.json',examiner/'test_contract.py']}
(root/'input-hashes.json').write_text(json.dumps(checks,indent=2)+'\n')
(root/'preflight.json').write_text(json.dumps(results,indent=2)+'\n')
print(json.dumps(results,indent=2))
assert all(x['exit_code']==0 and x.get('matches_fixture',True) and x.get('verified',True) and x.get('signed_in',True) for x in results), 'preflight failed'
