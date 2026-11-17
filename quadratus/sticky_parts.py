"""Which parts of a Python file a kept audit verdict rests on.

The staged audit keeps a requirement's MET while the files its line named
are unchanged. A whole-file comparison asks the requirement again whenever
anything in a widely cited file changes. This module narrows that to the
definitions the line cites (``file.py::name``) and everything in the same
file they reach.

Static only: the file is parsed, never imported or run. Anything this module
cannot read for certain returns None, and the caller compares the whole file.
"""

from __future__ import annotations

import ast
import hashlib
import re
from typing import Dict, List, Optional, Set, Tuple

#: A file larger than this is compared whole.
MAX_SOURCE_BYTES = 2_000_000


def _segment(lines: List[str], node: ast.AST) -> str:
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return "".join(lines[start - 1:node.end_lineno])


def _names_used(node: ast.AST) -> Set[str]:
    """Every name a definition mentions: plain names, attribute names and its
    own parameters (a pytest fixture is requested by parameter name)."""
    used: Set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            used.add(child.id)
        elif isinstance(child, ast.Attribute):
            used.add(child.attr)
        elif isinstance(child, ast.arg):
            used.add(child.arg)
    return used


def _targets(node: ast.AST) -> Set[str]:
    targets = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    return {name.id for target in targets for name in ast.walk(target) if isinstance(name, ast.Name)}


def _parts(source: str) -> Optional[Tuple[Dict[str, Tuple[str, Set[str]]], str, Dict[str, Set[str]]]]:
    """The file as named parts: ``{key: (text, names used)}``, the text of
    everything at module level that is not a named part, and for each bare
    name the keys it can mean. A method's key is ``Class.method``."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    lines = source.splitlines(keepends=True)
    parts: Dict[str, Tuple[str, Set[str]]] = {}
    meaning: Dict[str, Set[str]] = {}
    rest: List[str] = []

    def add(key: str, names: Set[str], text: str, used: Set[str]) -> None:
        if key in parts:
            # Defined twice: both texts belong to the part.
            text, used = parts[key][0] + text, parts[key][1] | used
        parts[key] = (text, used)
        for name in names:
            meaning.setdefault(name, set()).add(key)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node.name, {node.name}, _segment(lines, node), _names_used(node))
        elif isinstance(node, ast.ClassDef):
            head: List[str] = []
            members = []
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    members.append(child)
                else:
                    head.append(_segment(lines, child))
            # The class statement itself, its bases and its non-method body.
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            body_start = node.body[0].lineno if node.body else node.end_lineno
            opening = "".join(lines[first - 1:max(first, body_start) - 1]) or lines[first - 1]
            used = set()
            for base in node.bases + node.decorator_list + [k.value for k in node.keywords]:
                used |= _names_used(base)
            for child in node.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    used |= _names_used(child)
            add(node.name, {node.name}, opening + "".join(head), used)
            for child in members:
                key = f"{node.name}.{child.name}"
                # A method rests on its class's statement and attributes.
                add(key, {key, child.name}, _segment(lines, child), _names_used(child) | {node.name})
        elif _targets(node):
            names = _targets(node)
            add("=" + ",".join(sorted(names)), names, _segment(lines, node), _names_used(node) - names)
        else:
            rest.append(_segment(lines, node))
    return parts, "".join(rest), meaning


_CITED = re.compile(r"((?:::[A-Za-z_][A-Za-z0-9_.]*(?:\[[^\]\s]*\])?)+)")


def cited_names(line: str, path: str) -> Optional[List[str]]:
    """The definitions ``line`` cites in ``path`` as ``path::name`` or
    ``path::Class::method``, or None when any mention of the path stands
    alone: a file named alone is a file compared whole."""
    names: List[str] = []
    start = 0
    found = False
    while True:
        at = (line or "").find(path, start)
        if at < 0:
            break
        found = True
        start = at + len(path)
        match = _CITED.match(line, start)
        if match is None:
            return None
        pieces = [piece.split("[")[0] for piece in match.group(1).split("::") if piece]
        names.append(".".join(pieces))
        start = match.end()
    return names if found and names else None


def part_digest(source: str, names) -> Optional[str]:
    """A digest of the parts of ``source`` that the cited ``names`` rest on,
    or None when the whole file must be compared.

    The parts are every cited definition, everything in the file those
    definitions mention, and so on, plus all module-level code that is not a
    definition (imports, guards). A definition added or changed elsewhere in
    the file leaves the digest as it was; a cited one, or one those reach,
    changes it. A name the file does not define means the citation cannot be
    placed, and the whole file is compared.
    """
    if not isinstance(source, str) or len(source.encode("utf-8", "replace")) > MAX_SOURCE_BYTES:
        return None
    read = _parts(source)
    if read is None:
        return None
    parts, rest, meaning = read
    chosen: Set[str] = set()
    for name in names or ():
        keys = meaning.get(name)
        if not keys:
            return None
        chosen |= keys
    if not chosen:
        return None
    pending = list(chosen)
    while pending:
        _text, used = parts[pending.pop()]
        for name in used:
            for key in meaning.get(name, ()):
                if key not in chosen:
                    chosen.add(key)
                    pending.append(key)
    digest = hashlib.sha256()
    digest.update(rest.encode("utf-8", "replace"))
    for key in sorted(chosen):
        digest.update(b"\0" + key.encode("utf-8", "replace") + b"\0")
        digest.update(parts[key][0].encode("utf-8", "replace"))
    return "parts:" + digest.hexdigest()
