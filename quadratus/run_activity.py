"""Best-effort operator activity records. Never a completion or liveness gate."""

import json
import threading
from datetime import datetime, timezone


class RunActivity:
    def __init__(self, run_dir, callback=None):
        self.path = run_dir / "activity.jsonl"
        self.callback = callback
        self.lock = threading.Lock()

    def record(self, kind, **fields):
        event = dict(fields, kind=kind, at=datetime.now(timezone.utc).isoformat())
        try:
            with self.lock, self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event) + "\n")
        except Exception:  # reporting must not change execution
            pass

    def __call__(self, message):
        self.record("note", message=str(message)[:2000])
        if self.callback:
            try:
                self.callback(message)
            except Exception:  # a broken viewer must not stop work
                pass
