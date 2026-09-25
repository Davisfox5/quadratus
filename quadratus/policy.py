"""Offline repository policy loading and deterministic task-family resolution.

Cards and schemas are pinned package data. A repository may narrow capability,
never grant it through a fact or a model tier. Resolution does not run checks.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator

from .repo_scan import detect_adapters, scan_repo
from .scope import TaskScope, _matches


class PolicyError(ValueError):
    pass


def _json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PolicyError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise PolicyError(f"Invalid JSON number: {value}")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, TypeError) as exc:
        raise PolicyError(str(exc)) from exc


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _schema(document, schema):
    errors = sorted(Draft202012Validator(schema).iter_errors(document),
                    key=lambda e: str(list(e.absolute_path)))
    if errors:
        error = errors[0]
        raise PolicyError(f"{'.'.join(map(str, error.absolute_path)) or '$'}: {error.message}")


def _path(value, root=None, *, directory=False):
    if (not isinstance(value, str) or not value or value != value.strip()
            or '\\' in value or '\x00' in value or value.startswith(('~', '/'))
            or '..' in PurePosixPath(value).parts or re.match(r'^[A-Za-z]:', value)
            or (value in ('.', './') and not directory)):
        raise PolicyError(f"Expected a project-relative path: {value!r}")
    value = value.removeprefix('./') or '.'
    if root is not None:
        # Check the concrete prefix even when the suffix is a glob. resolve()
        # follows existing and dangling symlinks and raises on link loops.
        prefix = re.split(r'[*?\[]', value, maxsplit=1)[0]
        try:
            target = (root / prefix).resolve()
        except (OSError, RuntimeError) as exc:
            raise PolicyError(f"Cannot resolve path: {value}") from exc
        if not target.is_relative_to(root):
            raise PolicyError(f"Path leaves the project: {value}")
    return value


def _unique(rows, field, label):
    values = [row[field] for row in rows]
    if len(values) != len(set(values)):
        raise PolicyError(f"Duplicate {label}")


def load_library():
    """Load from installed resources, without reaching into the source checkout."""
    base = files('quadratus').joinpath('harness_library')
    origin = _json(base.joinpath('origin.json').read_text(encoding='utf-8'))
    measured = {}
    for name, expected in origin['files'].items():
        _path(name)
        measured[name] = hashlib.sha256(base.joinpath(name).read_bytes()).hexdigest()
        if measured[name] != expected:
            raise PolicyError(f"Packaged library content differs: {name}")
    if _hash(measured) != origin['digest']:
        raise PolicyError('Packaged library digest differs')
    schemas = {kind: _json(base.joinpath(f'schema/{kind}.schema.json').read_text('utf-8'))
               for kind in ('policy', 'family')}
    cards = {}
    for entry in sorted(base.joinpath('families').iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        card = _json(entry.joinpath('spec.json').read_text('utf-8'))
        _schema(card, schemas['family'])
        if card['id'] != entry.name:
            raise PolicyError(f"Family directory does not match id: {entry.name}")
        for name, key in [('checklist', 'id'), ('required_inputs', 'name'),
                          ('adapter_fields', 'name')]:
            _unique(card[name], key, f'{card["id"]}.{name}')
        _path(card['eval_cases_ref'])
        for check in card['checklist']:
            if check.get('gate_id') and check['gate_id'] not in card['gate_ids']:
                raise PolicyError(f"Unknown checklist gate: {check['gate_id']}")
        cards[card['id']] = card
    return origin, schemas, cards


def validate_policy(document, schema, cards, root):
    _schema(document, schema)
    # JSON Schema treats 1.0 as an integer. The public version contract uses an int.
    if type(document['schema_version']) is not int:
        raise PolicyError('schema_version must be integer 1')
    for path in document.get('instructions', []):
        _path(path, root)
    if 'lock_file' in document['library']:
        _path(document['library']['lock_file'], root)
    capability = document['capability_policy']
    for path in capability['deny_write']:
        _path(path, root)
    for sensitive in capability['sensitive']:
        for path in sensitive['paths']:
            _path(path, root)
    families = {document['defaults']['family']}
    overlays = {o for card in cards.values() for o in card['overlays_compatible']}
    for rule in document['path_rules']:
        for path in rule['paths']:
            _path(path, root)
        families.update(rule['families'])
        if set(rule.get('overlays', [])) - overlays:
            raise PolicyError('Unknown overlay in path_rules')
    if families - cards.keys():
        raise PolicyError('Unknown family: ' + ', '.join(sorted(families - cards.keys())))
    _unique(document['gates'], 'id', 'gate id')
    _unique(document['decisions'], 'id', 'decision id')
    gates = {gate['id']: gate for gate in document['gates']}
    if set(document['defaults']['required_gates']) - gates.keys():
        raise PolicyError('Required gate is not defined')
    for gate in gates.values():
        if gate['runner'] == 'command' and not gate.get('argv'):
            raise PolicyError(f"Command gate needs argv: {gate['id']}")
        if gate['runner'] != 'command' and 'argv' in gate:
            raise PolicyError(f"Only command gates take argv: {gate['id']}")
        if gate['runner'] == 'builtin:grep' and not gate.get('pattern'):
            raise PolicyError(f"Grep gate needs pattern: {gate['id']}")
        _path(gate.get('cwd', '.'), root, directory=True)
        if any(re.search(r'[|&;<>`$]', arg) and not arg.startswith('-')
               for arg in gate.get('argv', [])):
            raise PolicyError(f"Gate argv cannot be a shell expression: {gate['id']}")
    bindings = document.get('gate_bindings', {})
    family_gates = {g for card in cards.values() for g in card['gate_ids']}
    for name, binding in bindings.items():
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name) or name not in family_gates:
            raise PolicyError(f"Unknown family gate binding: {name}")
        if name in gates:
            raise PolicyError(f"Redundant binding for existing gate: {name}")
        if ('gate' in binding) == ('absent' in binding):
            raise PolicyError(f"Binding must name a gate or an absence: {name}")
        if 'gate' in binding and binding['gate'] not in gates:
            raise PolicyError(f"Binding target is not defined: {name}")
        if 'absent' in binding and not binding.get('because', '').strip():
            raise PolicyError(f"Absent gate needs a reason: {name}")
    for family in families:
        for name in cards[family]['gate_ids']:
            if name not in gates and name not in bindings:
                raise PolicyError(f"Family {family} gate {name} has no explicit binding")


def _overlap(left, right):
    """Conservative intersection for declared directory/glob scopes.

    Shared literal prefixes may overlap even before files exist. Narrow the
    declaration when that conservative result selects an unwanted rule.
    """
    if _matches(left, right) or _matches(right, left):
        return True
    a, b = (re.split(r'[*?\[]', p, maxsplit=1)[0].rstrip('/') for p in (left, right))
    return not a or not b or a == b or a.startswith(b + '/') or b.startswith(a + '/')


def _applies(declared, pattern, root):
    """Concrete filenames use glob matching; broad declarations may overlap."""
    path = root / declared
    broad = (any(c in declared for c in '*?[') or declared.endswith('/')
             or path.is_dir())
    return _overlap(declared, pattern) if broad else _matches(declared, pattern)


@dataclass
class RepositoryPolicy:
    root: Path
    document: dict
    cards: dict
    library: dict
    adapters: dict
    explicit: bool
    forbid: tuple = ()

    def role_packet(self, scope, role, conventions=''):
        """A bounded shared contract, without another reviewer's conversation.

        Required scope/checklist text is never silently cut. Oversized contracts
        stop before dispatch; only reference notes may be excerpted.
        """
        plan = self.resolve(scope.permitted_paths if scope else ())
        context = self.document.get('context', {})
        limit = min(24_000, context.get('reference_bytes_limit', 12_000))
        parts = ['## Role packet', f'Role: {role}',
                 scope.render() if scope else 'Scope: no task scope declared.',
                 'Families: ' + ', '.join(plan['families']),
                 'Overlays: ' + (', '.join(plan['overlays']) or 'none')]
        for family in plan['families']:
            card = self.cards[family]
            parts.append(f'Family checklist ({family}):')
            for check in card['checklist']:
                if role in check.get('applies_to', ['lead', 'reviewer', 'verifier']):
                    parts.append(f"- {check['rule']} Because: {check['because']}")
            parts.append('Required inputs: ' + json.dumps(card['required_inputs']))
            parts.append('Stop conditions: ' + json.dumps(card['output_contract']['stop_conditions']))
        # Gates by name and requirement only. A command can name a file seats
        # must not open (the Q9-v2 grader path reached every lead here).
        visible_gates = [{k: g[k] for k in ('id', 'runner', 'required', 'minimum_tests') if k in g}
                         for g in plan['gates']]
        parts.append('Check configuration: ' + json.dumps({
            'gates': visible_gates, 'absent': plan['absent_gates'],
            'skipped': plan['skipped_gates'], 'adapters': plan['adapters']}, sort_keys=True))
        parts.append('Repository decisions: ' + json.dumps(self.document.get('decisions', [])))
        contract = '\n'.join(parts)
        heading = '\nConventions notes (reference only; these do not grant permissions):\n'
        marker = '\n[Reference notes truncated to packet byte limit]'
        room = limit - len((contract + heading + marker).encode())
        if room < 0:
            raise PolicyError(f'Required role packet exceeds {limit} bytes; narrow the task')
        notes = conventions.encode()[:room + 1]
        for name in self.document.get('instructions', ['AGENTS.md', 'CLAUDE.md']):
            if len(notes) > room:
                break
            path = self.root / _path(name, self.root)
            if not path.is_file():
                continue
            with path.open('rb') as source:
                notes += ('\n' + name + ':\n').encode() + source.read(room - len(notes) + 1)
        truncated = len(notes) > room
        return contract + heading + notes[:room].decode('utf-8', errors='ignore') + (marker if truncated else '')

    def resolve(self, paths=(), *, writing=False):
        paths = sorted({_path(p, self.root) for p in paths})
        doc = self.document
        rules = [r for r in doc['path_rules']
                 if any(_applies(p, pattern, self.root) for p in paths for pattern in r['paths'])]
        families = list(dict.fromkeys(f for r in rules for f in r['families']))
        if not families:
            families = [doc['defaults']['family']]
        overlays = list(dict.fromkeys(o for r in rules for o in r.get('overlays', [])))
        blocked = []
        for rule in rules:
            supported = {o for f in (rule['families'] or families)
                         for o in self.cards[f]['overlays_compatible']}
            for overlay in rule.get('overlays', []):
                if overlay not in supported:
                    blocked.append(f"Overlay {overlay} is incompatible with rule families {rule['families']}")
        capability = doc['capability_policy']
        deny = list(dict.fromkeys([*capability['deny_write'], *self.forbid]))
        sensitive = capability['sensitive']
        if writing:
            for path in paths:
                # Also check canonical spelling; a model tier cannot grant a write.
                prefix = re.split(r'[*?\[]', path, maxsplit=1)[0]
                canonical = (self.root / prefix).resolve().relative_to(self.root).as_posix()
                for pattern in deny:
                    if _applies(path, pattern, self.root) or _applies(canonical, pattern, self.root):
                        blocked.append(f"Write denied: {path} ({pattern})")
                for item in sensitive:
                    if any(_applies(path, p, self.root) or _applies(canonical, p, self.root) for p in item['paths']):
                        blocked.append(f"Sensitive path {path} needs an operator ruling: "
                                       + ', '.join(item['requires']))
        gate_map = {g['id']: g for g in doc['gates']}
        wanted = list(dict.fromkeys([*doc['defaults']['required_gates'],
                                    *(g for f in families for g in self.cards[f]['gate_ids'])]))
        selected, absent = [], []
        for name in wanted:
            binding = doc.get('gate_bindings', {}).get(name, {'gate': name})
            if 'absent' in binding:
                absent.append({'id': name, 'status': 'not_configured', 'because': binding['because']})
            else:
                gate = gate_map.get(binding['gate'])
                if gate is None:
                    blocked.append(f"Gate {name} is not configured")
                elif not any(g['id'] == gate['id'] for g in selected):
                    selected.append(dict(gate))
        skipped = [dict(id=g['id'], status='skipped', because=f"Runner {g['runner']} is not configured")
                   for g in selected if not g['required']
                   and g['id'] not in doc['defaults']['required_gates']
                   and g['runner'] not in ('command', 'builtin:scope', 'builtin:diff-size')]
        adapters = {}
        for family in families:
            adapters[family] = {
                field['name']: dict(self.adapters.get(field['name'],
                    {'status': 'not_configured', 'value': None, 'source': None}),
                    required=field['required'])
                for field in self.cards[family]['adapter_fields']
            }
        result = dict(schema_version=1, repository_id=doc['repository_id'],
                      policy_source='.quadratus/policy.json' if self.explicit else 'built-in',
                      policy_hash=_hash(doc), library_version=self.library['version'],
                      library_digest=self.library['digest'], declared_paths=paths,
                      primary_family=families[0], families=families, overlays=overlays,
                      gates=selected, absent_gates=absent, skipped_gates=skipped, adapters=adapters,
                      deny_write=deny, sensitive=sensitive, defaults=doc['defaults'],
                      capability_policy=capability, blocked=list(dict.fromkeys(blocked)))
        result['hash'] = _hash(result)
        return result

    def scope(self, scope):
        """Intersect policy and operator limits with the existing scope checks."""
        limit = self.document['defaults']['scope_max_lines']
        deny = self.document['capability_policy']['deny_write'] + list(self.forbid)
        deny += [p for entry in self.document['capability_policy']['sensitive'] for p in entry['paths']]
        if scope is None:
            return TaskScope(max_lines=limit, forbidden_paths=tuple(deny),
                             overrun_ratio=min(1.5, self.document['defaults']['overrun_ratio']))
        from dataclasses import replace
        return replace(scope, max_lines=min(scope.max_lines or limit, limit),
                       overrun_ratio=min(scope.overrun_ratio, self.document['defaults']['overrun_ratio']),
                       forbidden_paths=tuple(dict.fromkeys([*scope.forbidden_paths, *deny])))


def load_policy(root, *, forbid=(), scan=None):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise PolicyError(f"Project folder does not exist: {root}")
    origin, schemas, cards = load_library()
    path = root / '.quadratus/policy.json'
    explicit = path.exists() or path.is_symlink()
    scan = scan or scan_repo(root)
    if explicit:
        _path('.quadratus/policy.json', root)
        try:
            document = _json(path.read_text(encoding='utf-8'))
        except OSError as exc:
            raise PolicyError(f"Cannot read repository policy: {exc}") from exc
        validate_policy(document, schemas['policy'], cards, root)
        if document['library']['version'] != origin['version']:
            raise PolicyError('Repository policy library version does not match the installed library')
        if document['library'].get('digest', origin['digest']) != origin['digest']:
            raise PolicyError('Repository policy library digest does not match the installed library')
    else:
        gates = [{'id': 'scope', 'runner': 'builtin:scope', 'required': True}]
        bindings = {'unit-tests': {'absent': True, 'because': 'No test command detected'}}
        if scan.check_command:
            gates.append({'id': 'unit-tests', 'runner': 'command', 'required': True,
                          'argv': scan.check_command, 'cwd': '.', 'minimum_tests': 1})
            bindings = {}
        document = dict(kind='policy', schema_version=1, repository_id='local/project',
                        library={'version': origin['version']},
                        defaults=dict(family='pure-logic', required_gates=['scope'],
                                      scope_max_lines=100, overrun_ratio=1.5, worker_depth=1),
                        capability_policy=dict(deny_write=['.git/**', '.env', '.env.*', '.quadratus/**'],
                                               external_effects='deny', native_delegation='deny', sensitive=[]),
                        path_rules=[], gates=gates, gate_bindings=bindings, decisions=[])
    return RepositoryPolicy(root, document, cards, origin, detect_adapters(root, scan),
                            explicit, tuple(_path(p, root) for p in forbid))


def preview_policy(root, paths=(), *, forbid=(), writing=False):
    return load_policy(root, forbid=forbid).resolve(paths, writing=writing)


def render_preview(plan):
    lines = [f"Family: {plan['primary_family']}",
             'Composed families: ' + ', '.join(plan['families']),
             'Overlays: ' + (', '.join(plan['overlays']) or 'none'),
             'Checks to run: ' + (', '.join(g['id'] for g in plan['gates']) or 'none'),
             f"Plan hash: {plan['hash']}"]
    lines += ['Blocked: ' + reason for reason in plan['blocked']]
    lines += [f"Not configured: {g['id']} ({g['because']})" for g in plan['absent_gates']]
    lines += [f"Skipped: {g['id']} ({g['because']})" for g in plan['skipped_gates']]
    for family, fields in plan['adapters'].items():
        missing = [name for name, value in fields.items() if value['status'] == 'not_configured']
        if missing:
            lines.append(f"{family} adapters not configured: " + ', '.join(missing))
    lines.append('Preview only. No model calls or check commands ran.')
    return '\n\n'.join(lines)


def task_gate(policy, plan, existing, *, exclude=()):
    """Bind policy checks to Q3 and retain operator checks without weakening them."""
    if not policy.explicit:
        return existing
    from . import integration
    commands = []
    required_ids = set(policy.document['defaults']['required_gates'])
    for gate in plan['gates']:
        required = gate['required'] or gate['id'] in required_ids
        if gate['runner'] in ('builtin:scope', 'builtin:diff-size'):
            continue  # The existing task scope measurement supplies these checks.
        if gate['runner'] != 'command':
            if required:
                raise PolicyError(f"Required runner is not configured: {gate['id']} ({gate['runner']})")
            continue
        if gate.get('side_effects') == 'external':
            raise PolicyError(f"Gate {gate['id']} needs a separate external-effect ruling")
        commands.append(integration.GateCommand(
            id=gate['id'], argv=tuple(gate['argv']), cwd=gate.get('cwd', '.'),
            timeout=gate.get('timeout_seconds', 600), required=required,
            minimum_tests=gate.get('minimum_tests') or None))
    if isinstance(existing, integration.GateSuite):
        extra = list(existing.commands)
    elif isinstance(existing, integration.IntegrationGate):
        # Matching argv is concrete command identity, not guessed gate-name equivalence.
        matches = [g for g in commands if tuple(existing.command) == g.argv
                   and (policy.root / g.cwd).resolve() == Path(existing.cwd).resolve()]
        if matches:
            from dataclasses import replace
            commands = [replace(g, timeout=min(g.timeout, existing.timeout), required=True)
                        if g in matches else g for g in commands]
            extra = []
        else:
            extra = [integration.GateCommand('operator-check', tuple(existing.command),
                     cwd=Path(existing.cwd).resolve().relative_to(policy.root).as_posix(),
                     timeout=existing.timeout)]
    elif existing is None:
        extra = []
    else:
        raise PolicyError('Cannot combine a custom operator gate with policy gates')
    for command in extra:
        same_id = [g for g in commands if g.id == command.id]
        if same_id and same_id[0] != command:
            raise PolicyError(f"Operator and policy gates disagree on {command.id}")
        if not same_id:
            commands.append(command)
    return integration.GateSuite(commands, cwd=policy.root, exclude=exclude) if commands else None
