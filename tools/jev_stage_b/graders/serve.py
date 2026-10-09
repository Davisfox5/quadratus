"""Grader-owned server for one Stage B cell.

    python serve.py <cell worktree> <seed.json>

Imports the cell's ``app.py``, points its data directories at a fresh temp
directory, creates the seed projects through the public API, binds a free
loopback port and prints one JSON line ``{"port", "data", "projects"}``,
then serves until killed. Nothing from the project's own tests is used.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    seed = json.loads(Path(sys.argv[2]).read_text()) if len(sys.argv) > 2 else []
    os.chdir(root)
    sys.path.insert(0, str(root))
    import app as application  # noqa: E402

    data = Path(tempfile.mkdtemp(prefix="stage-b-grader-"))
    application.DATA_DIR = str(data)
    application.PROJECTS_FILE = str(data / "projects.json")
    application.VIDEOS_DIR = str(data / "videos")
    application.RECORDINGS_DIR = str(data / "recordings")
    for path in (application.VIDEOS_DIR, application.RECORDINGS_DIR):
        os.makedirs(path, exist_ok=True)
    application.app.config["TESTING"] = True

    created = []
    with application.app.test_client() as client:
        for spec in seed:
            rv = client.post("/api/projects", json={"name": spec["name"]})
            assert rv.status_code == 201, rv.data
            pid = rv.get_json()["id"]
            for player in spec.get("players", []):
                assert client.post(f"/api/projects/{pid}/players", json=player).status_code == 201
            for clip in spec.get("clips", []):
                assert client.post(f"/api/projects/{pid}/clips", json=clip).status_code == 201
            created.append(client.get(f"/api/projects/{pid}").get_json())

    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, application.app, threaded=True)
    print(json.dumps(dict(port=server.server_port, data=str(data), projects=created)), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
