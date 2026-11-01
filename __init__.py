"""Shared model controls for Hermes client plugins."""
import sys
from . import api


def register(ctx):
    sys.modules["model_controls"] = api
    if hasattr(ctx, "on_unload"):
        def unload():
            if sys.modules.get("model_controls") is api:
                sys.modules.pop("model_controls", None)
        ctx.on_unload(unload)
