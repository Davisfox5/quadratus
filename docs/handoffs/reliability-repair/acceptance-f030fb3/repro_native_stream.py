"""Replay actual nested CLI delegation events without making vendor calls."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from quadratus.cli_providers import _extract_native_children  # noqa: E402

here = Path(__file__).resolve().parent
stream = (here / 'prior-native-stream-excerpt.jsonl').read_text()
children = _extract_native_children(stream)
print(json.dumps({
    'observed_stream_events': len(stream.splitlines()),
    'extracted_native_children': [child.__dict__ for child in children],
    'independently_verified_child_tokens': 135105,
    'note': 'The stream exposes native wait activity but lacks child IDs/usage; '
            'the original scoped session records supplied that separate evidence.',
}, indent=2))
