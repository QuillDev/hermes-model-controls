# model-controls

A standalone Hermes plugin for model selection. Client plugins supply the interface; this plugin reads Hermes's model inventory, resolves routes, saves profile defaults and holds per-conversation model, reasoning and speed overrides.

Install with `hermes plugins install QuillDev/hermes-model-controls --enable`, then restart the gateway. Install alongside the client plugin. It does not depend on Nako Portal, start a server, register agent tools or alter Hermes source files.

After registration, client plugins access `sys.modules.get("model_controls")`. Check at call time because plugins may load in either order. If absent, hide the controls or report that model-controls needs to be enabled.

Create `api.Client(home=profile_home, runner=gateway_runner, session_chat=authorized_session_to_chat, source=chat_to_session_source, active=adapter_active_sessions)`. The client owns authorization and supplies only sessions it can access. The API owns profile scope, capabilities, route resolution and persistence.

`models()` returns Hermes's provider/model inventory. `model()` and `set_model_options(body)` read and update the profile default. `session_model(id)` and async `set_session_model(id, body)` read and update that conversation only. `restore_model_options(source)` restores tuning before a turn after process restart. Settings bodies accept provider/model, effort, speed, or reset. Empty effort/speed restore inheritance. A reset clears all conversation overrides. A guarded switch returns `confirmation` and `message` without applying it; resubmit the same selection with that confirmation after the person accepts.

Model routes use Hermes's own persisted session overrides. Tuning lives in `<profile-home>/model-controls/settings.db`; no credentials are stored there. Defaults stay in Hermes's config. A session selection never writes the profile default.

The plugin uses private Hermes gateway model-switch and session-state methods. Verify compatibility when upgrading Hermes. Tests run with `python -m pytest -c tests/pytest.ini`; integration with a real gateway is exercised by the portal's backend suite with `MODEL_CONTROLS_SOURCE` pointing at this checkout.
