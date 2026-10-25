"""The installed catalogues, read without a model call (quadratus.catalog):
Codex's models_cache.json judged for shape, freshness and client, families
ordered numerically, and grok's own listing parsed for the default it marks."""
import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quadratus import catalog as C

NOW = datetime(2026, 10, 24, 23, 53, 20, tzinfo=timezone.utc).timestamp()
FETCHED = "2026-10-24T23:23:47Z"


def model(slug, efforts=("low", "medium", "high", "xhigh", "max", "ultra"), **extra):
    row = dict(slug=slug, display_name=slug, visibility="list",
               supported_reasoning_levels=[dict(effort=e, description=e) for e in efforts],
               default_reasoning_level="low")
    row.update(extra)
    return row


def write(tmp_path, models, *, fetched_at=FETCHED, client="0.163.0", name="models_cache.json", raw=None):
    path = tmp_path / name
    body = raw if raw is not None else dict(fetched_at=fetched_at, client_version=client, models=models)
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    return path


ROOT = [model("gpt-6.1-sol"), model("gpt-6-astra"), model("gpt-6-luna", efforts=("low", "medium", "high", "xhigh", "max")),
        model("gpt-5.6-terra"), model("gpt-6-sol"), model("gpt-5.6-sol"), model("gpt-5.6-luna")]


def test_slugs_parse_into_a_numeric_release_and_a_family():
    assert C.parse_slug("gpt-6.1-sol") == ((6, 1), "sol")
    assert C.parse_slug("gpt-6-astra") == ((6,), "astra")
    assert C.parse_slug("gpt-5.6-terra") == ((5, 6), "terra")
    assert C.parse_slug("gpt-7-sol") == ((7,), "sol")
    assert C.parse_slug("codex-mini-latest") is None and C.family_of("") is None


def test_the_latest_family_release_orders_numerically_not_lexically(tmp_path):
    cat = C.read_codex_catalog(write(tmp_path, ROOT + [model("gpt-6.10-sol"), model("gpt-6.9-sol")]),
                               installed_client="0.163.0", now=NOW)
    assert cat.ok, cat
    assert C.latest_for_family(cat, "sol")[0].slug == "gpt-6.10-sol"
    assert C.latest_for_family(cat, "luna")[0].slug == "gpt-6-luna"
    assert C.latest_for_family(cat, "terra")[0].slug == "gpt-5.6-terra", "the latest available Terra, never an invented 6"
    assert C.latest_for_family(cat, "astra")[0].slug == "gpt-6-astra"


def test_a_future_family_release_wins_the_moment_the_client_lists_it(tmp_path):
    cat = C.read_codex_catalog(write(tmp_path, ROOT + [model("gpt-7-sol")]), installed_client="0.163.0", now=NOW)
    found, why = C.latest_for_family(cat, "sol")
    assert found.slug == "gpt-7-sol" and why == ""


def test_hidden_or_client_gated_entries_do_not_resolve(tmp_path):
    rows = ROOT + [model("gpt-7-sol", visibility="hide"), model("gpt-6.5-sol", minimal_client_version="0.170.0")]
    cat = C.read_codex_catalog(write(tmp_path, rows), installed_client="0.163.0", now=NOW)
    assert C.latest_for_family(cat, "sol")[0].slug == "gpt-6.1-sol"
    only = C.read_codex_catalog(write(tmp_path, [model("gpt-7-sol", visibility="hide")], name="h.json"),
                                installed_client="0.163.0", now=NOW)
    found, why = C.latest_for_family(only, "sol")
    assert found is None and "no visible sol model" in why and "gpt-7-sol" in why
    assert C.latest_for_family(cat, "orion") == (None, "no orion model in the catalogue")


@pytest.mark.parametrize("name, body, status, reason", [
    ("missing", None, "missing", "run the codex client once"),
    ("text", "not json", "malformed", "not JSON"),
    ("shape", {"fetched_at": FETCHED, "models": {}}, "malformed", "no models list"),
    ("noslug", {"fetched_at": FETCHED, "models": [{"display_name": "x"}]}, "malformed", "no slug"),
    ("nofetch", {"models": ROOT}, "malformed", "fetched_at"),
    ("stale", {"fetched_at": "2026-10-01T00:00:00Z", "client_version": "0.163.0", "models": ROOT}, "stale", "over the 168 h bound"),
    ("client", {"fetched_at": FETCHED, "client_version": "0.160.0", "models": ROOT}, "client mismatch", "installed client is 0.163.0"),
])
def test_a_catalogue_that_cannot_be_trusted_is_a_status_never_a_fallback(tmp_path, name, body, status, reason):
    path = tmp_path / f"{name}.json"
    if body is not None:
        path.write_text(body if isinstance(body, str) else json.dumps(body))
    cat = C.read_codex_catalog(path, installed_client="0.163.0", now=NOW)
    assert cat.status == status and reason in cat.reason, cat
    assert not cat.ok and C.latest_for_family(cat, "sol")[0] is None
    assert cat.provenance()["status"] == status and cat.provenance()["path"] == str(path)


def test_the_age_bound_is_an_operator_setting_and_the_reason_names_it(tmp_path):
    path = write(tmp_path, ROOT, fetched_at="2026-10-20T00:00:00Z")
    assert C.read_codex_catalog(path, installed_client="0.163.0", now=NOW).ok
    stale = C.read_codex_catalog(path, installed_client="0.163.0", now=NOW, env={"QUADRATUS_CATALOG_MAX_AGE_HOURS": "24"})
    assert stale.status == "stale" and "24 h bound" in stale.reason


def test_an_unknown_installed_client_is_not_compared_and_is_recorded(tmp_path):
    cat = C.read_codex_catalog(write(tmp_path, ROOT), installed_client=None, now=NOW)
    assert cat.ok and cat.provenance()["installed_client"] is None and cat.client_version == "0.163.0"


def test_the_catalogue_path_follows_the_override_then_codex_home(tmp_path):
    assert C.codex_catalog_path({"QUADRATUS_CODEX_CATALOG": str(tmp_path / "c.json")}) == tmp_path / "c.json"
    assert C.codex_catalog_path({"CODEX_HOME": str(tmp_path)}) == tmp_path / "models_cache.json"
    assert C.codex_catalog_path({}).name == "models_cache.json"


def test_ultra_is_read_but_never_usable_and_a_missing_ladder_is_empty(tmp_path):
    cat = C.read_codex_catalog(write(tmp_path, [model("gpt-6.1-sol"), dict(slug="gpt-6-astra")]),
                               installed_client="0.163.0", now=NOW)
    sol, astra = cat.by_slug("gpt-6.1-sol"), cat.by_slug("gpt-6-astra")
    assert "ultra" in sol.efforts and "ultra" not in sol.usable_efforts
    assert astra.efforts == () and astra.usable_efforts == ()


GROK_LISTING = """Available models:
  grok-4.7 (default)
  grok-4.7-build-fast
  grok-4.6
  grok-4.5
You are not authenticated. Run `grok login` to sign in.
"""


def test_grok_listing_yields_the_marked_default_and_keeps_the_notice():
    parsed = C.parse_grok_models(GROK_LISTING)
    assert parsed.ok and parsed.default == "grok-4.7"
    assert parsed.available == ("grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.5")
    assert parsed.notices == ("You are not authenticated. Run `grok login` to sign in.",)
    assert C.parse_grok_models("default: grok-4.7\n").default == "grok-4.7"
    unmarked = C.parse_grok_models("grok-4.6\ngrok-4.5\n")
    assert unmarked.status == "no default marked" and unmarked.default is None
    assert C.parse_grok_models("nothing here").status == "unavailable"


def test_grok_catalog_runs_only_the_listing_and_reports_failure_honestly():
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw.get("stdin")))
        return SimpleNamespace(returncode=0, stdout=GROK_LISTING, stderr="")
    got = C.grok_catalog("/opt/homebrew/bin/grok", run=run)
    assert got.ok and got.default == "grok-4.7" and got.command == ("/opt/homebrew/bin/grok", "models")
    assert seen[0][0] == ["/opt/homebrew/bin/grok", "models"] and seen[0][1] is subprocess.DEVNULL
    failed = C.grok_catalog("grok", run=lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"))
    assert failed.status == "unavailable" and failed.reason == "exit 1: boom" and failed.default is None

    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])
    assert C.grok_catalog("nowhere", run=missing).status == "unavailable"
    assert C.grok_catalog("grok", run=lambda a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(a, 1))).reason.startswith("TimeoutExpired")
