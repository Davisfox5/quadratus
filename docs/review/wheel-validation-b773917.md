# Installed-wheel validation of b773917

Lane: Opus O-NEXT-03, 2026-09-28, 06:35 to 06:47 UTC. Independent review, no engine, tool or test edits.

## TL;DR

- **Verdict: cleared.** A wheel built offline from the exact b773917 archive installs, imports and runs entirely from its install directory. Nothing falls back to a source checkout.
- Every file under `quadratus/` in the archive ships in the wheel (104 of 104), including `_gate_producer/quadratus_gate_report.py` and all `harness_library` data.
- `tools/verify_workflow_wheel.py` from the archive passes with two runner interpreters.
- Both entry points resolve from the install. `quadratus --help` exits 0. `quadratus-gui` has no `--help`: calling `main()` starts Gradio on 127.0.0.1:7860, so the equivalent check was loading the entry point and building the interface without launching it.
- Not covered: no provider, vendor, browser or live workflow was run. No access to Davis's Mac or the Run19 source, so this says nothing about a source-copy match there.

## Identities

| Item | Value |
|---|---|
| Commit | `b773917efe6c7dd931cd3d5157782ce250b9c794` ("Assemble applicability engine with current and historical scorecard controls", 2026-09-28T02:19:14-04:00) |
| Tree (`git rev-parse b773917^{tree}`) | `ed49cb852136b695280f2338b50c806e5b49acc3` |
| Wheel | `quadratus-1.0.0-py3-none-any.whl`, 486586 bytes |
| Wheel sha256 | `a2a9f53c20cc7a274d13574f2dd78315a4e7cf78e4c1fe11ad1117891447751c` |
| Installed `.py` files | 51: 48 in `quadratus/` (including `__init__.py`), 1 in `quadratus/_gate_producer/`, plus `multi_model_workflow.py` and `chat_gui.py` |
| sha256 of sorted `sha256sum` list of installed `.py` (relative paths, `LC_ALL=C sort`) | `8b2a6f6192b5bb3e0c8212210217768c601c7b623eb640a31cd187f444b89570` |
| Installed files (excluding `__pycache__`) | 116 |
| RECORD check | 113 hashed entries match, 0 mismatch; 2 `bin/` entries skipped (see note) |
| Gate producer sha256 | `becc14d6ecf321c5c5c903e88ba1c826b7cc529038e1ee44d12f52ee6385568f` (same in my install and both verifier runs) |

Interpreters: system Python 3.11.15.

- Builder venv (`bvenv`): pip 24.0, setuptools 79.0.1, wheel 0.48.0, pytest 9.1.1.
- Runner venv (`rvenv`): runtime dependencies only, no quadratus: anthropic 1.8.0, openai 3.19.2, python-dotenv 1.2.3, colorama 0.4.6, gradio 6.28.0, playwright 1.63.0, jsonschema 4.26.0, pytest 9.1.1.

The venvs were populated from PyPI through the container proxy. That network use was for tooling and runtime dependencies only. The quadratus build and install used `--no-index` and `--no-build-isolation`. The Debian `install_layout` failure was avoided by building in a venv, not by touching project metadata. No project metadata or limits were changed.

## Commands

`W` is a scratch directory outside the repository.

```sh
# 1. Archive
git rev-parse b773917efe6c7dd931cd3d5157782ce250b9c794^{tree}
mkdir $W/src && git archive b773917efe6c7dd931cd3d5157782ce250b9c794 | tar -x -C $W/src

# 2. Build offline
python3 -m venv $W/bvenv
$W/bvenv/bin/python -m pip install "setuptools>=68" wheel pytest
$W/bvenv/bin/python -m pip wheel --no-deps --no-build-isolation --no-index --wheel-dir $W/wheels $W/src
sha256sum $W/wheels/quadratus-1.0.0-py3-none-any.whl

# 3. Install outside the source, delete the build copy
$W/bvenv/bin/python -m pip install --no-deps --no-index --target $W/inst $W/wheels/quadratus-1.0.0-py3-none-any.whl
rm -rf $W/src

python3 -m venv $W/rvenv
$W/rvenv/bin/python -m pip install "anthropic>=1.0.0" "openai>=1.40.0" "python-dotenv>=1.0.0" \
    "colorama>=0.4.6" "gradio>=6,<7" "playwright>=1.40" "jsonschema>=4.18" pytest

# third working directory
cd $W/run3 && $W/rvenv/bin/python -I $W/probe.py $W/inst
```

`probe.py` (scratch, not committed) asserts the working directory is not on `sys.path`, puts only the install dir on it, imports every module found by `pkgutil.walk_packages(quadratus.__path__)` plus the two top-level modules, then lists any `quadratus*` module whose `__file__` is outside the install. It reads package data through `importlib.resources.files('quadratus')` and loads the console-script entry points from the installed dist-info.

## Results

### Module resolution (`python -I`, third directory)

- `sys.flags.isolated == 1`.
- `quadratus.__file__` = `$W/inst/quadratus/__init__.py`; dist-info resolved at `$W/inst/quadratus-1.0.0.dist-info`.
- 49 modules imported (47 `quadratus.*` plus `multi_model_workflow`, `chat_gui`), 0 import failures, 0 modules outside the install.

### Package data

`pyproject.toml` declares `packages = ["quadratus"]`, `py-modules = ["multi_model_workflow", "chat_gui"]` and package data:

| Declared | Found in install |
|---|---|
| `_gate_producer/*.py` | `quadratus_gate_report.py`; `integration._PRODUCER_FILE` points at it inside the install |
| `harness_library/origin.json` | present |
| `harness_library/schema/*.json` | `family.schema.json`, `policy.schema.json` |
| `harness_library/families/*/*.json` and `*.md` | 13 families, 52 files (4 each: `spec.json`, `eval-cases.json`, `lead.md`, `review.md`) |

Cross-check against the archive: `find quadratus -type f` in the archive (104 files, no `__pycache__`) compared with the wheel's `quadratus/` entries. No file is in the archive but missing from the wheel, and none the other way.

`brand/` is not shipped. `quadratus/gui.py` documents this: `brand_asset()` returns `None` and the GUI starts without the mark. Observed: `brand_asset('favicon.svg')` is `None` in the install and `build_interface()` still succeeds.

### Entry points

`[project.scripts]`: `quadratus = quadratus.cli:main`, `quadratus-gui = quadratus.gui:main`. The installed `entry_points.txt` matches.

- `quadratus --help` (entry point loaded from the installed dist-info, `python -I`): exit 0, full argparse help printed, 0 `quadratus*` modules outside the install.
- `quadratus-gui`: `main()` takes no arguments and calls `demo.launch(server_name="127.0.0.1", server_port=7860, ...)`. My first attempt passed `--help`, which it ignores, so it started a local server; I stopped it. Equivalent check: load the entry point, confirm it is `quadratus.gui.main` from the install, call `build_interface()` without launch. Result: returns a Gradio `Blocks`, 0 modules outside the install.

### No fallback to a source checkout (decoy)

Working directory `$W/decoy` containing `quadratus/__init__.py` that raises `RuntimeError('DECOY quadratus imported from cwd')`.

| Case | Result |
|---|---|
| A. `-I`, install on `sys.path` | installed package loaded (`$W/inst/quadratus/__init__.py`), producer inside the install |
| B. `-I`, install not on `sys.path` | `ModuleNotFoundError: No module named 'quadratus'` (fails loudly, decoy not loaded) |
| C. control: no `-I`, install not on path | `RuntimeError: DECOY quadratus imported from cwd` (proves the decoy is live) |
| D. `-I -m quadratus.cli --help`, install not on path | `ModuleNotFoundError: No module named 'quadratus'` |
| E. `-I`, install on path, `quadratus.cli.main` with `--help` | exit 0 from the installed package |

### `tools/verify_workflow_wheel.py` from the archive

Run from a fresh archive of b773917 (`$W/src2`, byte-identical tool to the first archive), from a separate working directory:

```sh
$W/bvenv/bin/python -I $W/src2/tools/verify_workflow_wheel.py --source $W/src2 \
    --builder-python $W/bvenv/bin/python --runner-python $W/rvenv/bin/python
$W/bvenv/bin/python -I $W/src2/tools/verify_workflow_wheel.py --source $W/src2 \
    --builder-python $W/bvenv/bin/python --runner-python $W/bvenv/bin/python
```

Both exit 0 with:

```json
{
  "producer_sha256": "becc14d6ecf321c5c5c903e88ba1c826b7cc529038e1ee44d12f52ee6385568f",
  "assertion_product": true,
  "runner_fault_product": false,
  "setup_fault_product": false,
  "shadow_ignored": true
}
```

`package` and `producer` resolved under the tool's own `/tmp/quadratus-wheel-*/installed/`.

## Notes and failures

- **My usage error, not a defect in b773917:** the first verifier run passed relative interpreter paths (`../bvenv/bin/python`). The tool runs the probe with `cwd=root` (its temp dir), so a relative `--runner-python` fails with `FileNotFoundError`. Absolute paths work. The tool could `resolve()` those arguments; that is an observation only, no change made.
- `pip install --target` writes RECORD entries for `bin/quadratus` and `bin/quadratus-gui` as `../../bin/...`, which do not resolve from the target root. That is pip's `--target` behaviour, not the project's. The `bin/` wrappers carry the builder venv shebang, so they were not used; entry points were exercised through `importlib.metadata`.
- Not done, by scope: providers, vendor CLIs, browsers, live workflows, Run19 source comparison.
