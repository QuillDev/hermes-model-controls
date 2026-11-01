"""Hermes capabilities and durable per-conversation tuning."""
import json
import os
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, home):
        folder = Path(home) / "model-controls"
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        path = folder / "settings.db"
        self.db = sqlite3.connect(path, timeout=10)
        os.chmod(path, 0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (session TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def get(self, session):
        row = self.db.execute("SELECT value FROM settings WHERE session=?", (session,)).fetchone()
        return json.loads(row[0]) if row else {}

    def put(self, session, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (session, json.dumps(value)))
        self.db.commit()

    def close(self):
        self.db.close()


def options(config, provider, model, base_url=""):
    from agent.reasoning_effort import route_supported_efforts
    from hermes_cli.models import resolve_fast_mode_overrides

    efforts = list(route_supported_efforts(provider, model))
    speeds = ["normal"]
    if resolve_fast_mode_overrides(model, provider=provider, base_url=base_url):
        speeds += ["fast", "auto", "cold"]
        if resolve_fast_mode_overrides(model, provider=provider, base_url=base_url, tier="ultrafast"):
            speeds.append("ultrafast")
    return {"efforts": list(dict.fromkeys(["none", *efforts])), "speeds": speeds}


def validate(body):
    """Validate every field before any part of a selection is written."""
    from hermes_constants import parse_reasoning_effort
    from agent.fast_mode import parse_exact_service_tier
    if not isinstance(body, dict) or not body or set(body) - {"provider", "model", "effort", "speed", "reset", "confirm"}:
        raise ValueError("Invalid model settings")
    for key in ("provider", "model", "effort", "speed"):
        if key in body and not isinstance(body[key], str):
            raise ValueError(f"Invalid {key}")
    if ("model" in body) != ("provider" in body) or ("model" in body and (not body["model"] or not body["provider"])):
        raise ValueError("Pick a model and provider")
    if body.get("effort") and parse_reasoning_effort(body["effort"]) is None:
        raise ValueError("Unknown reasoning effort")
    if body.get("speed"):
        parse_exact_service_tier(body["speed"])
    if "reset" in body and body["reset"] is not True:
        raise ValueError("Invalid reset")
    if body.get("reset") and len(body) != 1:
        raise ValueError("Reset cannot include other settings")


def apply_tuning(runner, key, saved):
    from agent.fast_mode import parse_service_tier
    from hermes_constants import parse_reasoning_effort

    runner._set_session_reasoning_override(key, parse_reasoning_effort(saved["effort"]) if saved.get("effort") else None)
    runner._set_session_service_tier_override(key, parse_service_tier(saved.get("speed")), clear=not bool(saved.get("speed")))
