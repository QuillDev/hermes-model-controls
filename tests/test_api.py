"""The plugin works without any client application installed."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_registration_and_profile_isolated_persistence(tmp_path):
    spec = importlib.util.spec_from_file_location("standalone_model_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    class Context:
        def on_unload(self, callback):
            self.unload = callback
    ctx = Context()
    prior = sys.modules.get("model_controls")
    try:
        plugin.register(ctx)
        api = sys.modules["model_controls"]
        first = api.settings.Store(tmp_path / "first")
        second = api.settings.Store(tmp_path / "second")
        first.put("chat:one", {"effort": "high", "speed": "normal"})
        first.close()
        reopened = api.settings.Store(tmp_path / "first")
        assert reopened.get("chat:one") == {"effort": "high", "speed": "normal"}
        assert reopened.get("chat:two") == {}
        assert second.get("chat:one") == {}
        reopened.put("chat:one", {})
        assert reopened.get("chat:one") == {}
        reopened.close()
        second.close()
        ctx.unload()
        assert "model_controls" not in sys.modules
    finally:
        if prior is not None:
            sys.modules["model_controls"] = prior
