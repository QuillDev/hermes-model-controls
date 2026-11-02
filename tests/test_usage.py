"""Usage by account, as a client shows it. Needs Hermes importable; only provider data sources are faked."""
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytest.importorskip("agent.account_usage")
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow  # noqa: E402

RESET = datetime(2026, 11, 5, 16, tzinfo=timezone.utc)


def _snapshot(*windows, reason=None):
    return AccountUsageSnapshot(provider="p", source="test", fetched_at=datetime.now(timezone.utc),
                                windows=tuple(windows), unavailable_reason=reason)


@pytest.fixture
def api():
    spec = importlib.util.spec_from_file_location("usage_model_controls", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    return plugin.api


@pytest.fixture
def hermes(monkeypatch):
    """The providers Hermes lists, what each one's usage source answers, and how often it's asked."""
    import agent.account_usage as account_usage
    import agent.account_usage_cache as cache
    import hermes_cli.inventory as inventory
    import providers

    state = SimpleNamespace(rows=[], linked={}, single={}, fetched=[])
    monkeypatch.setattr(inventory, "load_picker_context", lambda: None)
    monkeypatch.setattr(inventory, "build_models_payload", lambda *a, **kw: {"providers": state.rows})

    def profile(slug):
        if slug not in state.linked:
            return SimpleNamespace(name=slug)
        answer = state.linked[slug]
        return SimpleNamespace(name=slug, fetch_account_usage_by_account=answer if callable(answer) else lambda: answer)

    def fetch(slug, **_):
        state.fetched.append(slug)
        return state.single.get(slug)

    monkeypatch.setattr(providers, "get_provider_profile", profile)
    monkeypatch.setattr(account_usage, "fetch_account_usage", fetch)
    monkeypatch.setattr(cache, "has_account_usage", lambda slug: slug in state.single or slug in state.linked)
    monkeypatch.setattr(cache, "cached_account_usage", lambda *a, **kw: None)
    return state


def test_each_linked_login_is_its_own_row(api, hermes):
    hermes.rows = [{"slug": "claude", "name": "Claude"}]
    hermes.linked["claude"] = [
        ("primary", _snapshot(AccountUsageWindow("Current week", 100, RESET), AccountUsageWindow("Current session", None))),
        ("secondary", _snapshot(AccountUsageWindow("Current week", 12.5))),
        ("third", _snapshot(reason="Claude Code is not signed in")),
    ]
    assert api.usage() == [{"provider": "claude", "label": "Claude", "accounts": [
        {"label": "primary", "windows": [{"label": "Current week", "used_percent": 100.0, "resets_at": RESET.isoformat()}], "note": None},
        {"label": "secondary", "windows": [{"label": "Current week", "used_percent": 12.5, "resets_at": None}], "note": None},
        {"label": "third", "windows": [], "note": "Claude Code is not signed in"},
    ]}]


def test_a_hermes_credential_pool_lists_its_accounts(api, hermes):
    hermes.rows = [{"slug": "codex", "name": "Codex", "usage": {"accounts": [
        {"id": "a", "label": "work", "state": "ready", "windows": [{"label": "Weekly", "used_percent": 40.0, "resets_at": None, "scope": "account"}]},
        {"id": "b", "label": "old", "state": "unavailable", "windows": []},
    ]}}]
    assert api.usage()[0]["accounts"] == [
        {"label": "work", "windows": [{"label": "Weekly", "used_percent": 40.0, "resets_at": None}], "note": None},
        {"label": "old", "windows": [], "note": "Signed out"},
    ]


def test_a_single_login_is_one_unlabeled_account_and_quiet_providers_are_left_out(api, hermes):
    hermes.rows = [{"slug": "codex", "name": "Codex"}, {"slug": "plain", "name": "Plain"},
                   {"slug": "away", "name": "Away", "authenticated": False}, {"slug": "empty", "name": "Empty"}]
    hermes.single = {"codex": _snapshot(AccountUsageWindow("5h", 3)), "away": _snapshot(AccountUsageWindow("5h", 1)), "empty": _snapshot()}
    assert api.usage() == [{"provider": "codex", "label": "Codex", "accounts": [
        {"label": "", "windows": [{"label": "5h", "used_percent": 3.0, "resets_at": None}], "note": None}]}]
    assert "away" not in hermes.fetched and "plain" not in hermes.fetched


def test_a_failing_per_account_hook_falls_back_to_the_provider_total(api, hermes):
    def broken():
        raise RuntimeError("boom")
    hermes.rows = [{"slug": "claude", "name": "Claude"}]
    hermes.linked["claude"] = broken
    hermes.single["claude"] = _snapshot(AccountUsageWindow("Current week", 7))
    assert api.usage()[0]["accounts"] == [{"label": "", "windows": [{"label": "Current week", "used_percent": 7.0, "resets_at": None}], "note": None}]
