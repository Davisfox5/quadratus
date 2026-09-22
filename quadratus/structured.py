"""Best-effort structured output for providers that have no JSON mode.

Running entirely on subscription-backed CLIs means no provider-side schema
enforcement: every structured phase (convergence verdicts, routing decisions,
refusal classification, spec extraction) gets prose back and has to recover a
object from it. Empirically the models follow a requested *schema* well but
ignore requested *formatting negatives* -- asking for "no markdown fences"
still reliably returns a ```json fence -- so this module never trusts the
instruction and always parses defensively.

The strategy is a ladder, cheapest first:

1. Parse the whole response.
2. Pull the contents of a fenced code block.
3. Scan for the first balanced ``{...}`` / ``[...]`` span.
4. Apply conservative repairs (trailing commas, smart quotes) and retry.

If every rung fails, :func:`generate_structured` re-prompts the model with its
own malformed output and an explicit correction, up to a bounded number of
attempts, before giving up.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional, Sequence

log = logging.getLogger(__name__)

__all__ = [
    "StructuredError",
    "extract_json",
    "generate_structured",
    "schema_instruction",
]

#: Fenced block, optionally tagged (```json / ```JSON / ```).
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)

#: Characters models substitute for ASCII quotes when prose-formatting kicks in.
_SMART_QUOTES = {
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
}

_OPENERS = {"{": "}", "[": "]"}


class StructuredError(RuntimeError):
    """A structured response could not be recovered from a model's output."""


def _strip_smart_quotes(text: str) -> str:
    for bad, good in _SMART_QUOTES.items():
        text = text.replace(bad, good)
    return text


def _drop_trailing_commas(text: str) -> str:
    """Remove ``,`` immediately before a closing brace/bracket.

    Only touches commas outside string literals, so a value like ``"a,}"``
    survives untouched.
    """
    out = []
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if escaped:
            escaped = False
            out.append(ch)
            continue
        if ch == "\\" and in_string:
            escaped = True
            out.append(ch)
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if ch == "," and not in_string:
            rest = text[i + 1:]
            stripped = rest.lstrip()
            if stripped[:1] in ("}", "]"):
                continue  # drop this comma
        out.append(ch)
    return "".join(out)


def _balanced_spans(text: str):
    """Yield candidate balanced JSON spans, outermost-first.

    Quote- and escape-aware so that braces inside strings do not affect depth.
    """
    for start, ch in enumerate(text):
        closer = _OPENERS.get(ch)
        if closer is None:
            continue
        depth = 0
        in_string = False
        escaped = False
        for end in range(start, len(text)):
            c = text[end]
            if escaped:
                escaped = False
                continue
            if c == "\\" and in_string:
                escaped = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c in _OPENERS:
                depth += 1
            elif c in ("}", "]"):
                depth -= 1
                if depth == 0:
                    yield text[start:end + 1]
                    break


def _candidates(text: str):
    """Yield progressively more aggressive parse candidates."""
    text = text.strip()
    yield text

    for match in _FENCE_RE.finditer(text):
        body = match.group(2).strip()
        if body:
            yield body

    for span in _balanced_spans(text):
        yield span


def _try_load(candidate: str) -> Optional[Any]:
    for attempt in (candidate, _drop_trailing_commas(_strip_smart_quotes(candidate))):
        try:
            return json.loads(attempt)
        except (ValueError, TypeError):
            continue
    return None


def extract_json(
    text: str,
    *,
    required_keys: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Recover a JSON object from a model response.

    Args:
        text: The raw model output, which may include prose and code fences.
        required_keys: If given, a candidate is only accepted when it is a
            mapping containing all of these keys. This stops an unrelated
            inline snippet from being mistaken for the intended payload.

    Raises:
        StructuredError: if no candidate parses into a conforming object.
    """
    if not text or not text.strip():
        raise StructuredError("model returned an empty response")

    fallback: Optional[Dict[str, Any]] = None

    for candidate in _candidates(text):
        loaded = _try_load(candidate)
        if loaded is None:
            continue
        # A bare list is valid JSON but never satisfies required_keys; wrap it
        # only when the caller has no key expectations.
        if not isinstance(loaded, dict):
            if required_keys:
                continue
            return {"value": loaded}
        if not required_keys:
            return loaded
        if all(k in loaded for k in required_keys):
            return loaded
        if fallback is None:
            fallback = loaded

    if fallback is not None:
        missing = [k for k in (required_keys or ()) if k not in fallback]
        raise StructuredError(
            f"parsed an object but it is missing required keys: {', '.join(missing)}"
        )
    raise StructuredError("no JSON object could be recovered from the response")


def schema_instruction(schema: Dict[str, str]) -> str:
    """Render a compact schema description to append to a system prompt.

    ``schema`` maps field name to a short type/meaning description.
    """
    fields = "\n".join(f'  "{k}": {v}' for k, v in schema.items())
    return (
        "Respond with a single JSON object and nothing else, of the form:\n"
        "{\n" + fields + "\n}\n"
        "Every field is required. Do not add commentary outside the object."
    )


def generate_structured(
    provider,
    prompt: str,
    *,
    system: str,
    schema: Dict[str, str],
    history=None,
    max_attempts: int = 3,
) -> Dict[str, Any]:
    """Call ``provider`` and return a parsed object conforming to ``schema``.

    On a parse failure the model is re-prompted with its own malformed output
    and an explicit correction. Attempts are bounded because a model that has
    failed the contract twice rarely recovers on the third try, and each retry
    costs a full agent invocation.

    Raises:
        StructuredError: if no attempt produces a conforming object.
    """
    required = list(schema.keys())
    full_system = f"{system}\n\n{schema_instruction(schema)}"
    attempt_prompt = prompt
    last_error: Optional[str] = None
    last_raw = ""

    for attempt in range(1, max_attempts + 1):
        raw = provider.generate(attempt_prompt, system=full_system, history=history)
        last_raw = raw
        try:
            return extract_json(raw, required_keys=required)
        except StructuredError as exc:
            last_error = str(exc)
            log.warning(
                "%s structured output failed (attempt %d/%d): %s",
                getattr(provider, "label", "provider"),
                attempt,
                max_attempts,
                exc,
            )
            attempt_prompt = (
                f"{prompt}\n\n"
                "--- CORRECTION REQUIRED ---\n"
                f"Your previous reply could not be parsed ({exc}). It began:\n"
                f"{raw[:400]}\n\n"
                f"Reply again with ONLY the JSON object containing exactly these "
                f"keys: {', '.join(required)}."
            )

    raise StructuredError(
        f"{getattr(provider, 'label', 'provider')} did not return valid JSON after "
        f"{max_attempts} attempts ({last_error}). Last output began: {last_raw[:200]}"
    )


def parse_security_verdict(text: str, *, snapshot_hash: str, acceptance: Sequence[str]):
    """Strict v1 verdict. Never repair or search inside an authority-bearing reply."""
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError(f'duplicate key: {key}')
            obj[key] = value
        return obj

    def invalid_constant(value):
        raise ValueError(f'invalid JSON constant: {value}')

    try:
        doc = json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, TypeError) as exc:
        raise StructuredError(f'Invalid security verdict JSON: {exc}') from exc
    fields = {'schema_version', 'verdict', 'snapshot_hash', 'acceptance_results',
              'blocking_findings', 'limitations'}
    if not isinstance(doc, dict) or set(doc) != fields:
        raise StructuredError('Security verdict must contain exactly the v1 fields')
    if type(doc['schema_version']) is not int or doc['schema_version'] != 1:
        raise StructuredError('Unsupported security verdict schema_version')
    if doc['verdict'] not in ('accept', 'reject', 'insufficient_evidence'):
        raise StructuredError('Unknown security verdict')
    if doc['snapshot_hash'] != snapshot_hash:
        raise StructuredError('Security verdict snapshot_hash does not match the reviewed source')
    for key in ('blocking_findings', 'limitations'):
        if not isinstance(doc[key], list) or any(
                not isinstance(s, str) or not s.strip() for s in doc[key]):
            raise StructuredError(f'{key} must be a list of nonempty strings')
    results = doc['acceptance_results']
    if not isinstance(results, list) or len(results) != len(acceptance):
        raise StructuredError('Security verdict must cover every acceptance criterion')
    for result, criterion in zip(results, acceptance, strict=True):
        if (not isinstance(result, dict) or set(result) != {'criterion', 'status', 'evidence'}
                or result['criterion'] != criterion
                or result['status'] not in ('passed', 'failed', 'insufficient_evidence')
                or not isinstance(result['evidence'], str) or not result['evidence'].strip()):
            raise StructuredError('Invalid or missing acceptance evidence')
    if doc['verdict'] == 'accept' and (
            doc['blocking_findings'] or doc['limitations']
            or any(r['status'] != 'passed' for r in results)):
        raise StructuredError('Accept contradicts findings, limitations or acceptance results')
    if doc['verdict'] == 'reject' and not (
            doc['blocking_findings'] or any(r['status'] == 'failed' for r in results)):
        raise StructuredError('Reject must name a blocking finding or failed criterion')
    return doc
