"""Model selection API for client plugins. No portal or UI dependency.

Published as sys.modules["model_controls"] by register(). Each client supplies the
runner, profile home and authorized source lookup; this plugin owns all settings.
"""
import asyncio
import hashlib
import json
from pathlib import Path

from . import settings


def providers():
    from hermes_cli.inventory import build_models_payload, load_picker_context
    payload = build_models_payload(load_picker_context(), non_blocking_catalogs=True)
    return [{"provider": str(row["slug"]), "label": str(row.get("name") or row["slug"]),
             "models": [str(m) for m in row.get("models") or []]}
            for row in payload.get("providers", []) if row.get("slug")]


# Several linked logins are probed one after another; give them longer than core's single hook.
ACCOUNTS_DEADLINE_S = 30.0


def usage():
    """Plan limits of every signed-in provider that reports them, one row per account:
    [{provider, label, accounts: [{label, windows: [{label, used_percent, resets_at}], note}]}].
    A provider plugin that links several logins itself answers `fetch_account_usage_by_account()`;
    Hermes credential pools come from the picker inventory; anything else is one unlabeled account.
    Model-scoped windows (one model family's own cap, e.g. Fable's weekly limit) are left out: they
    never say whether the account itself is out, and the shown limits are the account's."""
    from agent.account_usage_cache import has_account_usage
    from hermes_cli.inventory import build_models_payload, load_picker_context

    payload = build_models_payload(load_picker_context(), non_blocking_catalogs=True)
    shown = []
    for row in payload.get("providers", []):
        slug = str(row.get("slug") or "")
        if not slug or row.get("is_user_defined") or row.get("authenticated") is False:
            continue
        accounts = _linked_accounts(slug)
        if accounts is None:
            pooled = (row.get("usage") or {}).get("accounts")
            if pooled:
                accounts = [_pooled_account(slug, account) for account in pooled]
            elif has_account_usage(slug):
                accounts = [_account("", _provider_snapshot(slug))]
            else:
                accounts = []
        accounts = [account for account in accounts if account["windows"] or account["note"]]
        if accounts:
            shown.append({"provider": slug, "label": str(row.get("name") or slug), "accounts": accounts})
    return shown


def _linked_accounts(slug):
    import logging
    from agent.deadline import run_bounded_sync
    from providers import get_provider_profile

    try:
        hook = getattr(get_provider_profile(slug), "fetch_account_usage_by_account", None)
        if not callable(hook):
            return None
        bounded = run_bounded_sync(hook, ACCOUNTS_DEADLINE_S, label="account-usage-by-account")
    except Exception:
        logging.getLogger(__name__).debug("Per-account usage failed for %s", slug, exc_info=True)
        return None
    if bounded.timed_out or bounded.value is None:
        return None
    return [_account(str(label), snapshot, account_id=str(label)) for label, snapshot in bounded.value]


def _provider_snapshot(slug):
    from agent.account_usage import fetch_account_usage
    from agent.account_usage_cache import cached_account_usage, snapshot_is_stale

    snapshot = cached_account_usage(slug)
    return fetch_account_usage(slug) if snapshot is None or snapshot_is_stale(snapshot) else snapshot


def _reset_options(raw_options):
    if not isinstance(raw_options, list):
        return []
    out = []
    for item in raw_options:
        if not isinstance(item, dict):
            continue
        gid = item.get("id")
        label = item.get("label")
        avail = item.get("available")
        if not isinstance(gid, str) or not gid or not isinstance(label, str) or not label:
            continue
        if isinstance(avail, bool) or not isinstance(avail, (int, float)):
            continue
        option = {
            "id": gid,
            "label": label,
            "available": max(0, int(avail)),
            "usable": bool(item.get("usable")) and int(avail) > 0,
        }
        expires_at = item.get("expires_at")
        if isinstance(expires_at, str) and expires_at:
            option["expires_at"] = expires_at
        out.append(option)
    return out


def _resets(snapshot, *, account_id=""):
    raw = getattr(snapshot, "raw", None)
    if not isinstance(raw, dict):
        return None
    generic = raw.get("usage_resets")
    if isinstance(generic, dict):
        available = generic.get("available")
        if not isinstance(available, bool) and isinstance(available, (int, float)):
            count = max(0, int(available))
            resets = {"available": count, "usable": bool(generic.get("usable")) and count > 0}
            options = _reset_options(generic.get("options"))
            if options:
                resets["options"] = options
            target = str(generic.get("account") or account_id or "")
            if target:
                resets["account"] = target
            return resets
    credits = raw.get("rate_limit_reset_credits")
    if not isinstance(credits, dict):
        return None
    available = credits.get("available_count")
    if isinstance(available, bool) or not isinstance(available, (int, float)):
        return None
    count = max(0, int(available))
    applicable = credits.get("applicable_available_count")
    if isinstance(applicable, bool) or not isinstance(applicable, (int, float)):
        usable = count > 0
    else:
        usable = count > 0 and int(applicable) > 0
    resets = {"available": count, "usable": usable}
    if account_id:
        resets["account"] = account_id
    return resets


def _account(label, snapshot, *, account_id=""):
    windows = [{"label": str(w.label), "used_percent": float(w.used_percent),
                "resets_at": w.reset_at.isoformat() if w.reset_at else None}
               for w in (snapshot.windows if snapshot else ())
               if w.used_percent is not None and getattr(w, "scope", "account") != "model"]
    note = None if windows else (getattr(snapshot, "unavailable_reason", None) or ("Usage unavailable" if snapshot is None else None))
    row = {"label": label, "windows": windows, "note": note}
    resets = _resets(snapshot, account_id=account_id)
    if resets is not None:
        row["resets"] = resets
    return row


def _pooled_account(slug, account):
    from agent.account_usage_cache import cached_account_usage, snapshot_is_stale

    windows = [{"label": str(w.get("label") or ""), "used_percent": float(w["used_percent"]), "resets_at": w.get("resets_at")}
               for w in account.get("windows") or ()
               if w.get("used_percent") is not None and w.get("scope", "account") != "model"]
    note = None if windows else ("Signed out" if account.get("state") == "unavailable" else "Usage unavailable")
    row = {"label": str(account.get("label") or ""), "windows": windows, "note": note}
    account_id = str(account.get("id") or "")
    snapshot = cached_account_usage(slug, identity_id=account_id) if account_id else None
    resets = _resets(snapshot, account_id=account_id) if snapshot is not None and not snapshot_is_stale(snapshot) else None
    if resets is not None:
        row["resets"] = resets
    return row


def _codex_reset_target(account):
    if not account:
        return None, None, None
    from agent.account_usage_cache import _identity_id_for
    from agent.credential_pool import load_pool
    from hermes_cli.auth_codex import _codex_pool_route_base_url

    for entry in load_pool("openai-codex").entries():
        identity_id = _identity_id_for("openai-codex", entry)
        if account in (identity_id, str(entry.id or ""), str(entry.label or "")):
            return _codex_pool_route_base_url(entry.runtime_base_url), entry.runtime_api_key, identity_id
    raise ValueError("Account not found")


def use_reset(provider, account="", force=False, grant=""):
    """Redeem one banked usage reset for Codex or any provider exposing `redeem_account_reset`."""
    from agent.account_usage import fetch_account_usage, redeem_codex_reset_credit
    from providers import get_provider_profile

    slug = str(provider or "").strip().lower()
    if slug == "openai-codex":
        base_url, api_key, identity_id = _codex_reset_target(str(account or "").strip())
        result = redeem_codex_reset_credit(base_url=base_url, api_key=api_key, force=bool(force))
        if result.redeemed or result.status == "already_redeemed":
            try:
                fetch_account_usage(slug, base_url=base_url, api_key=api_key, identity_id=identity_id)
            except Exception:
                pass
        return {
            "status": result.status,
            "redeemed": result.redeemed,
            "message": result.message,
            "available": result.available_count,
        }

    hook = getattr(get_provider_profile(slug), "redeem_account_reset", None) if slug else None
    if not callable(hook):
        raise ValueError("That provider doesn't support usage resets")
    reply = hook(account=str(account or "").strip(), grant=str(grant or "").strip(), force=bool(force))
    if not isinstance(reply, dict):
        raise ValueError("Provider returned an invalid reset response")
    return {
        "status": str(reply.get("status") or ""),
        "redeemed": bool(reply.get("redeemed")),
        "message": str(reply.get("message") or ""),
        "available": reply.get("available"),
    }


class Client:
    def __init__(self, *, home, runner, session_chat, source, active):
        self.home = Path(home)
        self.runner = runner
        self.session_chat = session_chat
        self.source = source
        self.active = active

    def _home(self):
        return self.home

    def _profile_scope(self):
        from gateway.run import _profile_runtime_scope
        return _profile_runtime_scope(self.home)

    def models(self):
        with self._profile_scope():
            return providers()

    def usage(self):
        with self._profile_scope():
            return usage()

    def use_reset(self, provider, account="", force=False, grant=""):
        with self._profile_scope():
            return use_reset(provider, account=account, force=force, grant=grant)

    def model(self):
        from hermes_cli.config import load_config

        with self._profile_scope():
            config = load_config()
            chosen = config.get("model") or {}
            agent = config.get("agent") or {}
            from agent.fast_mode import service_tier_word, parse_service_tier
            from hermes_constants import resolve_reasoning_config
            if not isinstance(chosen, dict):
                chosen = {"default": str(chosen)}
            provider, model = str(chosen.get("provider") or ""), str(chosen.get("default") or chosen.get("model") or "")
            reasoning = resolve_reasoning_config(config, model) or {}
            effort = "none" if reasoning.get("enabled") is False else reasoning.get("effort", "")
            return {"provider": provider, "model": model, "effort": effort,
                    "speed": service_tier_word(parse_service_tier(agent.get("service_tier"))),
                    **settings.options(config, provider, model, chosen.get("base_url") or "")}

    def set_model(self, provider, model, tuning=None):
        """Switch the shown profile's model the way `/model <model> --provider <p> --global` does:
        Hermes validates the pick and rewrites the endpoint keys that go with it."""
        from hermes_cli.config import get_compatible_custom_providers, load_config
        from hermes_cli.model_switch import persist_model_selection, switch_model

        with self._profile_scope():
            config = load_config()
            current = config.get("model") if isinstance(config.get("model"), dict) else {}
            result = switch_model(
                raw_input=model,
                explicit_provider=provider,
                is_global=True,
                current_provider=str(current.get("provider") or ""),
                current_model=str(current.get("default") or ""),
                current_base_url=str(current.get("base_url") or ""),
                user_providers=config.get("providers") if isinstance(config.get("providers"), dict) else {},
                custom_providers=get_compatible_custom_providers(config),
            )
            if not result.success:
                raise ValueError(result.error_message or "That model can't be used")
            speed = (tuning or {}).get("speed")
            if speed and speed not in settings.options(config, result.target_provider, result.new_model, result.base_url)["speeds"]:
                raise ValueError("That speed isn't supported by this model and provider")
            persist_model_selection(result)

    def set_model_options(self, body):
        from hermes_cli.config import set_config_value

        settings.validate(body)
        if body.get("reset") or "confirm" in body:
            raise ValueError("Pick default model settings")
        if "model" in body:
            self.set_model(body["provider"], body["model"], body)
        elif body.get("speed") and body["speed"] not in self.model()["speeds"]:
            raise ValueError("That speed isn't supported by this model and provider")
        with self._profile_scope():
            for field, key in (("effort", "agent.reasoning_effort"), ("speed", "agent.service_tier")):
                if field in body:
                    set_config_value(key, body[field])
        return self.model()

    def _conversation_options(self, chat):
        return settings.Store(self.home), f"{self.source(chat).platform.value}:{chat}"

    def restore_model_options(self, source):
        store = settings.Store(self.home)
        try:
            saved = store.get(f"{source.platform.value}:{source.chat_id}")
        finally:
            store.close()
        if saved:
            settings.apply_tuning(self.runner, self.runner._session_key_for_source(source), saved)

    def session_model(self, session):
        chat = self.session_chat(session)
        if not chat:
            raise ValueError("No chat with that id")
        runner = self.runner
        source = self.source(chat)
        key = runner._session_key_for_source(source)
        with self._profile_scope():
            from hermes_cli.config import load_config
            config = load_config()
            runner._rehydrate_session_model_override(key)
            override = runner._session_model_override(key) or {}
            current = self.model()
            current.update({k: override[k] for k in ("provider", "model") if override.get(k)})
            reasoning = runner._resolve_session_reasoning_config(source=source, session_key=key, model=current["model"]) or {}
            current["effort"] = "none" if reasoning.get("enabled") is False else reasoning.get("effort", "")
            from agent.fast_mode import service_tier_word
            current["speed"] = service_tier_word(runner._resolve_session_service_tier(source=source, session_key=key))
            store, meta = self._conversation_options(chat)
            try:
                saved = store.get(meta)
            finally:
                store.close()
            current.update({k: saved[k] for k in ("effort", "speed") if saved.get(k)})
            block = config.get("model") or {}
            block = block if isinstance(block, dict) else {}
            base_url = override.get("base_url", "") if override else block.get("base_url", "")
            current.update(settings.options(config, current["provider"], current["model"], base_url))
            current["overridden"] = bool(override or saved)
            return current

    async def set_session_model(self, session, body):
        from gateway.slash_commands_model import _ModelSwitchContext
        from hermes_cli.model_switch import switch_model
        from hermes_cli.config import get_compatible_custom_providers, load_config

        settings.validate(body)
        chat = self.session_chat(session)
        if not chat:
            raise ValueError("No chat with that id")
        source = self.source(chat)
        runner = self.runner
        key = runner._session_key_for_source(source)
        with self._profile_scope():
            async with runner._model_switch_lock():
                if key in self.active:
                    raise ValueError("Wait for this turn to finish")
                store, meta = self._conversation_options(chat)
                try:
                    saved = store.get(meta)
                    if body.get("reset"):
                        await runner.async_session_store.set_model_override(key, None)
                        runner._session_state(key).conversation.model_override = None
                        getattr(runner, "_pending_model_notes", {}).pop(key, None)
                        saved = {}
                    else:
                        if "model" in body:
                            config = load_config()
                            current = self.session_model(session)
                            result = await asyncio.to_thread(switch_model, raw_input=body["model"], explicit_provider=body["provider"],
                                is_global=False, current_provider=current["provider"], current_model=current["model"],
                                current_base_url=(runner._session_model_override(key) or {}).get("base_url") or "",
                                user_providers=config.get("providers"), custom_providers=get_compatible_custom_providers(config))
                            if not result.success:
                                raise ValueError(result.error_message or "That model can't be used")
                            from hermes_cli.model_selection_guards import selection_warnings, selection_context_for_agent, combined_message
                            warnings = await asyncio.to_thread(selection_warnings, result.new_model, provider=result.target_provider,
                                base_url=result.base_url, api_key=result.api_key, model_info=getattr(result, "model_info", None),
                                selection_context=selection_context_for_agent(runner._cached_agent_for(key)))
                            confirmation = hashlib.sha256(json.dumps([session, current["provider"], current["model"],
                                result.target_provider, result.new_model, result.base_url, [w.message for w in warnings]]).encode()).hexdigest()
                            if warnings and body.get("confirm") != confirmation:
                                return {"confirmation": confirmation, "message": combined_message(warnings)}
                            offered = settings.options(config, result.target_provider, result.new_model, result.base_url)
                            if body.get("speed") and body["speed"] not in offered["speeds"]:
                                raise ValueError("That speed isn't supported by this model and provider")
                            ctx = _ModelSwitchContext(session_key=key, source=source, config_path=self._home() / "config.yaml", persist_global=False,
                                current_model=current["model"], current_provider=current["provider"])
                            await runner._record_model_switch(result, ctx, source=source, one_turn=False, picker=True)
                            persisted = runner.session_store.get_model_override(key) or {}
                            if persisted.get("model") != result.new_model or persisted.get("provider") != result.target_provider:
                                raise ValueError("The session model could not be saved")
                        elif body.get("speed") and body["speed"] not in self.session_model(session)["speeds"]:
                            raise ValueError("That speed isn't supported by this model and provider")
                        for field in ("effort", "speed"):
                            if field in body:
                                saved[field] = body[field]
                        saved = {k: v for k, v in saved.items() if v}
                    store.put(meta, saved)
                    settings.apply_tuning(runner, key, saved)
                    runner._evict_cached_agent(key)
                finally:
                    store.close()
            return self.session_model(session)

