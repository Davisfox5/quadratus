"""New diagnostic lane: external disposable storage, seeded once, no reset route."""
import json, os, shutil, signal, sys, tempfile
from pathlib import Path
from werkzeug.serving import make_server
root=Path(sys.argv[1]).resolve()
seed=json.loads(Path(sys.argv[2]).read_text())
sys.path.insert(0,str(root)); os.chdir(root)
import app as application
storage=Path(tempfile.mkdtemp(prefix="mvp-ui-preview-"))
application.DATA_DIR=str(storage)
application.PROJECTS_FILE=str(storage/"projects.json")
application.VIDEOS_DIR=str(storage/"videos")
application.RECORDINGS_DIR=str(storage/"recordings")
for p in (application.VIDEOS_DIR,application.RECORDINGS_DIR): Path(p).mkdir()
application.app.config["TESTING"]=True
with application.app.test_client() as client:
    for spec in seed:
        created=client.post('/api/projects',json={'name':spec['name']})
        assert created.status_code==201,created.data
        pid=created.get_json()['id']
        for player in spec.get('players',[]):
            assert client.post(f'/api/projects/{pid}/players',json=player).status_code==201
        for clip in spec.get('clips',[]):
            assert client.post(f'/api/projects/{pid}/clips',json=clip).status_code==201
server=make_server('127.0.0.1',int(sys.argv[3]) if len(sys.argv)>3 else 0,application.app,threaded=True)
def interrupted(*unused): raise SystemExit(143)
signal.signal(signal.SIGTERM,interrupted)
print(json.dumps({'port':server.server_port,'data':str(storage),'seeded_projects':len(seed)}),flush=True)
try: server.serve_forever()
finally:
    server.server_close()
    shutil.rmtree(storage,ignore_errors=True)
