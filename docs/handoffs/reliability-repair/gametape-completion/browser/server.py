"""Serve the selected source with disposable synthetic browser-acceptance data."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ['GAMETAPE_ROOT']).resolve()
sys.path.insert(0, str(ROOT))
import app as application

DATA = Path(tempfile.mkdtemp(prefix='gametape-bulk-browser-'))
application.DATA_DIR = str(DATA)
application.PROJECTS_FILE = str(DATA / 'projects.json')
application.VIDEOS_DIR = str(DATA / 'videos')
application.RECORDINGS_DIR = str(DATA / 'recordings')
for path in [application.VIDEOS_DIR, application.RECORDINGS_DIR]:
    os.makedirs(path, exist_ok=True)
video = Path(application.VIDEOS_DIR) / 'synthetic.mp4'
subprocess.run(['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i',
                'color=c=darkgreen:s=960x540:d=12:r=15', '-c:v', 'libx264',
                '-pix_fmt', 'yuv420p', '-y', str(video)], check=True)
application.app.config['TESTING'] = True
application.app.jinja_env.auto_reload = True
fixtures = {}
with application.app.test_client() as client:
    for name in ['Bulk QA Match', 'Other QA Match']:
        project = client.post('/api/projects', json={'name': name}).get_json()
        pid = project['id']
        player = client.post(f'/api/projects/{pid}/players', json={'name': 'Alex QA', 'number': '9'}).get_json()
        clips = []
        for index, (tag, label) in enumerate([('Pass', 'Left channel'), ('Pass', 'Right channel'), ('Shot', 'Goal attempt')]):
            clip = client.post(f'/api/projects/{pid}/clips', json={
                'tag_type': tag, 'start': index * 3, 'end': index * 3 + 2,
                'label': label, 'players': [player['id']],
            }).get_json()
            clips.append(clip['id'])
        fixtures[name] = {'project_id': pid, 'player_id': player['id'], 'clip_ids': clips}
    for tag in ['Pass', 'Goal']:
        pid = fixtures['Bulk QA Match']['project_id']
        client.post(f'/api/projects/{pid}/filter_presets', json={'name': 'QA ' + tag, 'tag_type': tag})
    projects = application._load_projects()
    for project in projects.values():
        project['video_filename'] = video.name
    application._save_projects(projects)
Path(__file__).with_name('fixtures.json').write_text(json.dumps(fixtures, indent=2))
print(json.dumps({'url': 'http://127.0.0.1:5088', 'data': str(DATA), 'fixtures': fixtures}), flush=True)
application.app.run(host='127.0.0.1', port=5088, threaded=True, debug=False)
