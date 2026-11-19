"""Execute the real coordinator with fake HA states and deterministic timers."""
import importlib.util
import sys
import types
from pathlib import Path

import bms_const as C
import pytest


@pytest.fixture
def runtime(monkeypatch):
    modules = {}
    for name in ["homeassistant", "homeassistant.config_entries", "homeassistant.core", "homeassistant.helpers", "homeassistant.helpers.event", "homeassistant.helpers.storage", "homeassistant.helpers.update_coordinator"]:
        modules[name] = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, modules[name])
    modules["homeassistant.config_entries"].ConfigEntry = object
    core = modules["homeassistant.core"]
    core.CALLBACK_TYPE = object
    core.Event = core.HomeAssistant = object
    core.callback = lambda fn: fn
    clock = types.SimpleNamespace(now=0.0, timers=[])

    def later(hass, delay, callback):
        timer = types.SimpleNamespace(at=clock.now + delay, callback=callback, cancelled=False)
        clock.timers.append(timer)
        return lambda: setattr(timer, "cancelled", True)

    modules["homeassistant.helpers.event"].async_call_later = later
    modules["homeassistant.helpers.event"].async_track_state_change_event = lambda *args: lambda: None

    class Store:
        def __init__(self, *args):
            pass

        def async_delay_save(self, *args):
            pass

    class Coordinator:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, hass, *args, **kwargs):
            self.hass = hass
            self.data = {}

        def async_set_updated_data(self, data):
            self.data = data

    modules["homeassistant.helpers.storage"].Store = Store
    modules["homeassistant.helpers.update_coordinator"].DataUpdateCoordinator = Coordinator
    name = "bms_pure_pkg.coordinator"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "custom_components/benni_media_state/coordinator.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.now)
    states = {}
    hass = types.SimpleNamespace(states=types.SimpleNamespace(get=states.get))
    entry = types.SimpleNamespace(data={}, options={}, entry_id="test")
    coord = module.MediaStateCoordinator(hass, entry)

    def put(key, value, **attrs):
        states[coord._entity_id(key)] = types.SimpleNamespace(state=value, attributes=attrs)

    def advance(now):
        while True:
            due = [t for t in clock.timers if not t.cancelled and t.at <= now]
            if not due:
                break
            timer = min(due, key=lambda t: t.at)
            clock.now = timer.at
            timer.cancelled = True
            timer.callback(None)
        clock.now = now

    return coord, clock, put, advance


def test_grace_starts_at_event_and_expires_without_another_state_event(runtime):
    coord, clock, put, advance = runtime
    put(C.CONF_TV_PLAYER, "on", source="Apple OTT")
    put(C.CONF_APPLETV_PLAYER, "playing")
    coord.async_set_updated_data(coord._compute())
    assert coord.data["context"] == C.CTX_STREAMING
    clock.now = 10
    put(C.CONF_APPLETV_PLAYER, "paused")
    put(C.CONF_TV_PLAYER, "on", source="unavailable")
    coord._on_state_change(None)
    advance(12)
    assert coord.data["context"] == C.CTX_STREAMING
    assert coord.data["source_arbitration"]["grace_remaining_seconds"] == 3
    advance(15)
    assert coord.data["context"] != C.CTX_STREAMING
    assert coord.data["source_arbitration"]["grace_expired"]


def test_other_source_wins_without_native_ps5_or_tv_master(runtime):
    coord, clock, put, advance = runtime
    put(C.CONF_TV_PLAYER, "on", source="Apple OTT")
    put(C.CONF_APPLETV_PLAYER, "playing")
    coord.async_set_updated_data(coord._compute())
    clock.now = 1
    put(C.CONF_APPLETV_PLAYER, "paused")
    put(C.CONF_PS5_PLAYER, "off")
    put(C.CONF_TV_PLAYER, "on", source="PlayStation 5")
    coord._on_state_change(None)
    advance(3)
    assert coord.data["device"] == C.DEV_PS5
    assert coord.data["source_arbitration"]["reason"] == "source_changed"


def test_tv_off_bypasses_running_grace(runtime):
    coord, clock, put, advance = runtime
    put(C.CONF_TV_PLAYER, "on", source="Apple OTT")
    put(C.CONF_APPLETV_PLAYER, "playing")
    coord.async_set_updated_data(coord._compute())
    put(C.CONF_APPLETV_PLAYER, "paused")
    put(C.CONF_TV_PLAYER, "on", source="unknown")
    coord._on_state_change(None)
    advance(1)
    put(C.CONF_TV_PLAYER, "off", source="Apple OTT")
    coord._on_state_change(None)
    advance(2)
    assert coord.data["context"] == C.CTX_IDLE
    assert not coord.data["source_arbitration"]["grace_active"]


def test_authoritative_playing_cannot_be_starved_by_repeated_events(runtime):
    coord, clock, put, advance = runtime
    put(C.CONF_APPLETV_PLAYER, "playing")
    coord._on_state_change(None)
    for time in [0.5, 1, 1.5]:
        advance(time)
        coord._on_state_change(None)
    advance(2)
    assert coord.data["context"] == C.CTX_STREAMING


def test_tv_only_startup_finishes_without_new_events(runtime):
    coord, clock, put, advance = runtime
    put(C.CONF_TV_PLAYER, "unavailable")
    put(C.CONF_TV_POWER, "50")
    coord.async_set_updated_data(coord._compute())
    assert coord.data["context"] == C.CTX_IDLE
    advance(20)
    assert coord.data["context"] == C.CTX_TV


def test_existing_binding_override_wins_over_lan_prefill(runtime):
    coord, clock, put, advance = runtime
    coord.entry.options[C.CONF_APPLETV_PLAYER] = "media_player.current_native"
    put(C.CONF_APPLETV_PLAYER, "paused")
    assert coord.bindings()[C.CONF_APPLETV_PLAYER] == "media_player.current_native"
    assert coord._atv()[0] == "paused"


@pytest.mark.parametrize("pc_on, device", [(True, C.DEV_PC), (False, C.DEV_NONE)])
def test_tv_master_off_rejects_electrical_ps5_activity(runtime, pc_on, device):
    coord, _, put, _ = runtime
    put(C.CONF_TV_MASTER, "off", is_active=False)
    put(C.CONF_TV_PLAYER, "unavailable")
    put(C.CONF_PS5_ACTIVE, "active", is_active=True, source_watt=45.0)
    put(C.CONF_PS5_PLAYER, "off")
    put(C.CONF_PS5_RAW, "previous game")
    put(C.CONF_PC_ACTIVE, "active" if pc_on else "off", is_active=pc_on)

    data = coord._compute()

    assert (data["context"], data["device"], data["subcontext"]) == (
        C.CTX_IDLE, device, C.SUB_NONE,
    )
    assert data["gaming_platform"] == C.GP_NONE
    assert data["activity_context"] == C.ACTX_IDLE
    assert data["activity_attrs"]["entertainment_active"] is False
    assert data["source_arbitration"]["reason"] == "tv_off"
    assert data["devices"]["ps5"]["active"] is True


@pytest.mark.parametrize("source, atv_state, context, device", [
    ("PlayStation 5", "playing", C.CTX_GAMING, C.DEV_PS5),
    ("Apple OTT", "playing", C.CTX_STREAMING, C.DEV_APPLETV),
    ("ARD", "off", C.CTX_TV, C.DEV_TV),
    ("Other HDMI", "off", C.CTX_TV, C.DEV_TV),
])
def test_tv_on_ps5_master_cannot_override_other_input(runtime, source, atv_state, context, device):
    coord, _, put, advance = runtime
    put(C.CONF_TV_MASTER, "active", is_active=True)
    put(C.CONF_TV_PLAYER, "on", source=source)
    put(C.CONF_PS5_ACTIVE, "active", is_active=True, source_watt=45.0)
    put(C.CONF_PS5_PLAYER, "off")
    put(C.CONF_PS5_RAW, "fixture game")
    put(C.CONF_APPLETV_PLAYER, atv_state)

    coord.async_set_updated_data(coord._compute())
    if device in (C.DEV_PS5, C.DEV_APPLETV):
        assert (coord.data["context"], coord.data["device"]) == (context, device)
    advance(20)

    assert (coord.data["context"], coord.data["device"]) == (context, device)


@pytest.mark.parametrize("state", ["off", "standby"])
def test_explicit_tv_player_off_blocks_even_playing_apple_tv(runtime, state):
    coord, _, put, advance = runtime
    put(C.CONF_TV_PLAYER, "on", source="Apple OTT")
    put(C.CONF_APPLETV_PLAYER, "playing", app_id="com.netflix.Netflix")
    coord.async_set_updated_data(coord._compute())
    assert coord.data["context"] == C.CTX_STREAMING

    put(C.CONF_TV_PLAYER, state, source="Apple OTT")
    put(C.CONF_APPLETV_PLAYER, "playing", app_id="com.apple.TVSettings")
    coord._on_state_change(None)
    advance(2)

    assert coord.data["context"] == C.CTX_IDLE
    assert coord.data["device"] == C.DEV_NONE
    assert coord.data["entertainment_active"] is False


@pytest.mark.parametrize("unknown_state", ["unknown", "unavailable"])
def test_unknown_tv_keeps_native_and_master_fallback(runtime, unknown_state):
    coord, _, put, _ = runtime
    put(C.CONF_TV_MASTER, unknown_state)
    put(C.CONF_TV_PLAYER, unknown_state)
    put(C.CONF_PS5_ACTIVE, "active", is_active=True, source_watt=45.0)
    put(C.CONF_PS5_PLAYER, "off")
    assert coord._compute()["device"] == C.DEV_PS5

    put(C.CONF_PS5_ACTIVE, "off", is_active=False)
    put(C.CONF_APPLETV_PLAYER, "playing")
    data = coord._compute()
    assert data["context"] == C.CTX_STREAMING
    assert data["source_arbitration"]["reason"] == "tv_not_confirmed"


def test_tv_off_ends_ps5_session_before_psn_updates_and_clears_holds(runtime):
    coord, _, put, advance = runtime
    put(C.CONF_TV_MASTER, "active", is_active=True)
    put(C.CONF_TV_PLAYER, "on", source="PlayStation 5")
    put(C.CONF_PS5_ACTIVE, "active", is_active=True)
    put(C.CONF_PS5_PLAYER, "playing")
    put(C.CONF_PS5_RAW, "fixture game")
    put(C.CONF_PC_ACTIVE, "active", is_active=True)
    put(C.CONF_HOMEPODS_PLAYER, "playing")
    coord.async_set_updated_data(coord._compute())
    assert coord.data["subcontext"] == C.SUB_GAME_DEFAULT
    assert coord.data["activity_context"] == C.ACTX_GAMING

    put(C.CONF_TV_MASTER, "off", is_active=False)
    coord._on_state_change(None)
    advance(2)
    assert (coord.data["context"], coord.data["device"]) == (C.CTX_IDLE, C.DEV_PC)
    assert coord.data["activity_context"] == C.ACTX_MUSIC
    assert coord.data["activity_attrs"]["gaming_platform"] == C.GP_NONE
    assert coord.data["activity_attrs"]["entertainment_active"] is False
    assert coord.data["activity_attrs"]["homepods_playing"] is True
    assert coord.data["source_arbitration"]["last_valid_foreground_source"] == C.DEV_PS5
    assert coord.data["source_arbitration"]["grace_active"] is False
    assert coord._ps5_on_since is None
    assert coord._sticky_gaming_sub is None

    put(C.CONF_TV_MASTER, "unavailable")
    put(C.CONF_TV_PLAYER, "unavailable")
    put(C.CONF_PS5_ACTIVE, "unavailable")
    put(C.CONF_PS5_PLAYER, "unavailable")
    coord._on_state_change(None)
    advance(4)
    assert (coord.data["context"], coord.data["device"]) == (C.CTX_IDLE, C.DEV_PC)

    put(C.CONF_PC_RAW, "PC fixture game")
    coord._on_state_change(None)
    advance(6)
    assert coord.data["gaming_platform"] == C.GP_PC
    assert coord.data["activity_attrs"]["game_title"] == "PC fixture game"

    put(C.CONF_TV_PLAYER, "on", source="PlayStation 5")
    put(C.CONF_PS5_RAW, "idle")
    coord._on_state_change(None)
    advance(8)
    assert coord.data["gaming_platform"] == C.GP_PS5
    assert coord.data["subcontext"] == C.SUB_GAME_GRIND


def test_short_webos_off_does_not_override_confirmed_tv_master(runtime):
    coord, _, put, advance = runtime
    put(C.CONF_TV_MASTER, "active", is_active=True)
    put(C.CONF_TV_PLAYER, "on", source="PlayStation 5")
    coord.async_set_updated_data(coord._compute())
    assert coord.data["gaming_platform"] == C.GP_PS5

    put(C.CONF_TV_PLAYER, "off")
    coord._on_state_change(None)
    advance(2)
    assert coord.data["gaming_platform"] == C.GP_PS5
    assert coord.data["source_arbitration"]["reason"] == "source_grace"
    advance(5)
    assert coord.data["context"] == C.CTX_IDLE
    assert coord.data["source_arbitration"]["grace_expired"] is True
