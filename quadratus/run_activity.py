"""Best-effort operator activity records. Never a completion or liveness gate."""

import json
import threading
from datetime import datetime, timezone

from .safe_io import append_private


class RunActivity:
    def __init__(self, run_dir, callback=None):
        self.path = run_dir / "activity.jsonl"
        self.callback = callback
        self.lock = threading.Lock()

    def record(self, kind, **fields):
        event = dict(fields, kind=kind, at=datetime.now(timezone.utc).isoformat())
        try:
            with self.lock:
                # Owner-only from creation: the first line holds the goal.
                append_private(self.path, json.dumps(event) + "\n")
        except Exception:  # reporting must not change execution
            pass

    def __call__(self, message):
        self.record("note", message=str(message)[:2000])
        if self.callback:
            try:
                self.callback(message)
            except Exception:  # a broken viewer must not stop work
                pass
