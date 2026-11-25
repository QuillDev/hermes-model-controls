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


def test_a_model_scoped_cap_is_left_out_of_the_account_limits(api, hermes):
    hermes.rows = [{"slug": "claude", "name": "Claude"},
                   {"slug": "codex", "name": "Codex", "usage": {"accounts": [{"id": "a", "label": "work", "state": "ready", "windows": [
                       {"label": "Weekly", "used_percent": 40.0, "resets_at": None, "scope": "account"},
                       {"label": "Spark week", "used_percent": 90.0, "resets_at": None, "scope": "model"}]}]}}]
    hermes.linked["claude"] = [("primary", _snapshot(AccountUsageWindow("Current week", 20), AccountUsageWindow("Fable week", 80, scope="model")))]
    shown = {row["provider"]: [w["label"] for a in row["accounts"] for w in a["windows"]] for row in api.usage()}
    assert shown == {"claude": ["Current week"], "codex": ["Weekly"]}


def test_banked_resets_are_reported_on_single_and_pooled_accounts(api, hermes, monkeypatch):
    import agent.account_usage_cache as cache

    single_raw = {"rate_limit_reset_credits": {"available_count": 3, "applicable_available_count": 0}}
    pooled_raw = {"rate_limit_reset_credits": {"available_count": 2, "applicable_available_count": 1}}
    hermes.rows = [
        {"slug": "openai-codex", "name": "ChatGPT / Codex"},
        {"slug": "pooled-codex", "name": "Pooled", "usage": {"accounts": [
            {"id": "codex:acct:1", "label": "work", "state": "ready", "windows": [{"label": "Weekly", "used_percent": 50.0, "resets_at": None, "scope": "account"}]},
        ]}},
    ]
    hermes.single["openai-codex"] = AccountUsageSnapshot(
        provider="openai-codex", source="usage_api", fetched_at=datetime.now(timezone.utc),
        windows=(AccountUsageWindow("Weekly", 8),), raw=single_raw,
    )
    pooled_snap = AccountUsageSnapshot(
        provider="pooled-codex", source="usage_api", fetched_at=datetime.now(timezone.utc),
        windows=(AccountUsageWindow("Weekly", 50),), raw=pooled_raw,
    )
    monkeypatch.setattr(cache, "cached_account_usage", lambda slug, identity_id=None, **kw: pooled_snap if identity_id == "codex:acct:1" else None)
    monkeypatch.setattr(cache, "snapshot_is_stale", lambda snap: False)

    rows = {row["provider"]: row["accounts"] for row in api.usage()}
    assert rows["openai-codex"][0]["resets"] == {"available": 3, "usable": False}
    assert rows["pooled-codex"][0]["resets"] == {"available": 2, "usable": True, "account": "codex:acct:1"}


def test_use_reset_redeems_codex_credit_and_refreshes_usage(api, hermes, monkeypatch):
    import agent.account_usage as account_usage
    import agent.account_usage_cache as cache
    import agent.credential_pool as credential_pool
    import hermes_cli.auth_codex as auth_codex

    calls = []
    refreshed = []

    def fake_redeem(*, base_url=None, api_key=None, force=False):
        calls.append((base_url, api_key, force))
        return account_usage.CodexResetRedeemResult(status="reset", message="Reset redeemed.", available_count=2, windows_reset=1)

    def fake_fetch(slug, **kw):
        refreshed.append((slug, kw))
        return _snapshot(AccountUsageWindow("Weekly", 0))

    monkeypatch.setattr(account_usage, "redeem_codex_reset_credit", fake_redeem)
    monkeypatch.setattr(account_usage, "fetch_account_usage", fake_fetch)

    outcome = api.use_reset("openai-codex", force=True)
    assert outcome == {"status": "reset", "redeemed": True, "message": "Reset redeemed.", "available": 2}
    assert calls == [(None, None, True)]
    assert refreshed == [("openai-codex", {"base_url": None, "api_key": None, "identity_id": None})]

    entry = SimpleNamespace(id="e1", label="work", runtime_base_url="https://chatgpt.com/backend-api", runtime_api_key="tok-1")
    monkeypatch.setattr(credential_pool, "load_pool", lambda slug: SimpleNamespace(entries=lambda: [entry]))
    monkeypatch.setattr(cache, "_identity_id_for", lambda slug, e: "codex:acct:1")
    monkeypatch.setattr(auth_codex, "_codex_pool_route_base_url", lambda url: url)

    pooled = api.use_reset("openai-codex", account="codex:acct:1", force=True)
    assert pooled["redeemed"] is True
    assert calls[-1] == ("https://chatgpt.com/backend-api", "tok-1", True)
    assert refreshed[-1] == ("openai-codex", {"base_url": "https://chatgpt.com/backend-api", "api_key": "tok-1", "identity_id": "codex:acct:1"})

    with pytest.raises(ValueError, match="doesn't support"):
        api.use_reset("antigravity-oauth")
    with pytest.raises(ValueError, match="Account not found"):
        api.use_reset("openai-codex", account="missing")


def test_provider_plugin_resets_surface_options_and_dispatch_redeem(api, hermes, monkeypatch):
    import providers

    claude_resets = {
        "available": 3,
        "usable": True,
        "options": [
            {"id": "full-reset-2611", "label": "weekly reset", "available": 1, "usable": True, "expires_at": "2026-12-10T22:00:00+00:00"},
            {"id": "launch-2610-fable", "label": "Fable reset", "available": 2, "usable": True},
        ],
    }
    hermes.rows = [{"slug": "claude-subscription-directsdk-experimental", "name": "Claude"}]
    hermes.linked["claude-subscription-directsdk-experimental"] = [
        ("work@example.test", AccountUsageSnapshot(
            provider="claude-subscription-directsdk-experimental", source="claude_code_get_usage",
            fetched_at=datetime.now(timezone.utc),
            windows=(AccountUsageWindow("Current week", 18),),
            raw={"usage_resets": claude_resets},
        )),
    ]

    shown = api.usage()
    assert shown[0]["accounts"][0]["resets"] == {**claude_resets, "account": "work@example.test"}

    redeem_calls = []

    def redeem_hook(*, account="", grant="", force=False):
        redeem_calls.append((account, grant, force))
        return {"status": "reset", "redeemed": True, "message": "Limits reset.", "available": 2}

    monkeypatch.setattr(providers, "get_provider_profile", lambda slug: SimpleNamespace(
        name=slug, redeem_account_reset=redeem_hook if slug == "claude-subscription-directsdk-experimental" else None,
    ))
    outcome = api.use_reset("claude-subscription-directsdk-experimental", account="work@example.test", grant="launch-2610-fable")
    assert outcome == {"status": "reset", "redeemed": True, "message": "Limits reset.", "available": 2}
    assert redeem_calls == [("work@example.test", "launch-2610-fable", False)]
