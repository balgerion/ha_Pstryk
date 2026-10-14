"""Coordinator -> cache -> JSON sensor, with stand-ins for the Home Assistant helpers.

Home Assistant is not installed here, so the few HA names these modules import
are replaced by small stubs. The clock is set to just after each fixture was
fetched (Europe/Warsaw): 2026-10-02 13:00 for the one with both days published,
2026-10-04 02:00 for the one with tomorrow unpublished.
"""
import asyncio
import copy
import importlib
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "pstryk"
FIXTURES = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 10, 2, 13, 0, tzinfo=TZ)
EARLY_NOW = datetime(2026, 10, 4, 2, 0, tzinfo=TZ)
CLOCK = [NOW]

REAL = json.loads((FIXTURES / "api_pricing_20261002T120545.json").read_text())
EARLY = json.loads((FIXTURES / "api_pricing_20261004T013435.json").read_text())


def _parse_datetime(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _as_local(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.astimezone(TZ)


class _Coordinator:
    def __init__(self, hass, logger, name=None, **kwargs):
        self.hass, self.name, self.data, self.last_update_success = hass, name, None, True


class _CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator


def _stub(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def _stubs():
    dt = _stub(
        "homeassistant.util.dt",
        now=lambda: CLOCK[0],
        utcnow=lambda: CLOCK[0].astimezone(timezone.utc),
        as_utc=lambda d: d.astimezone(timezone.utc),
        as_local=_as_local,
        parse_datetime=_parse_datetime,
        DEFAULT_TIME_ZONE=TZ,
    )
    aiohttp = _stub("aiohttp")
    aiohttp.__getattr__ = lambda name: type(name, (Exception,), {})
    package = _stub("pstryk")
    package.__path__ = [str(PKG)]
    return {
        "homeassistant": _stub("homeassistant"),
        "homeassistant.util": _stub("homeassistant.util", dt=dt),
        "homeassistant.util.dt": dt,
        "homeassistant.helpers": _stub("homeassistant.helpers"),
        "homeassistant.helpers.update_coordinator": _stub(
            "homeassistant.helpers.update_coordinator",
            DataUpdateCoordinator=_Coordinator,
            UpdateFailed=type("UpdateFailed", (Exception,), {}),
            CoordinatorEntity=_CoordinatorEntity,
        ),
        "homeassistant.helpers.event": _stub(
            "homeassistant.helpers.event", async_track_point_in_time=lambda *a, **k: None
        ),
        "homeassistant.helpers.storage": _stub("homeassistant.helpers.storage", Store=object),
        "homeassistant.helpers.restore_state": _stub(
            "homeassistant.helpers.restore_state", RestoreEntity=type("RestoreEntity", (), {})
        ),
        "homeassistant.helpers.aiohttp_client": _stub(
            "homeassistant.helpers.aiohttp_client", async_get_clientsession=None
        ),
        "homeassistant.config_entries": _stub(
            "homeassistant.config_entries", ConfigEntry=type("ConfigEntry", (), {})
        ),
        "homeassistant.core": _stub(
            "homeassistant.core", HomeAssistant=type("HomeAssistant", (), {}), callback=lambda f: f
        ),
        "homeassistant.components": _stub("homeassistant.components"),
        "homeassistant.components.sensor": _stub(
            "homeassistant.components.sensor",
            SensorEntity=type("SensorEntity", (), {}),
            SensorStateClass=types.SimpleNamespace(MEASUREMENT="measurement", TOTAL="total"),
            SensorDeviceClass=types.SimpleNamespace(MONETARY="monetary", TIMESTAMP="timestamp"),
        ),
        "homeassistant.loader": _stub("homeassistant.loader", async_get_integration=None),
        "aiohttp": aiohttp,
        "pstryk": package,
    }


@pytest.fixture(scope="module")
def mods():
    stubs = _stubs()
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        yield types.SimpleNamespace(
            uc=importlib.import_module("pstryk.update_coordinator"),
            sensor=importlib.import_module("pstryk.sensor"),
            pc=importlib.import_module("pstryk.price_components"),
        )
    finally:
        for name in [n for n in sys.modules if n == "pstryk" or n.startswith("pstryk.")]:
            del sys.modules[name]
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class _FakeAPI:
    def __init__(self, response):
        self.response = response

    async def fetch(self, url, **kwargs):
        return copy.deepcopy(self.response)


def _fetch(mods, tmp_path, response, price_type):
    coordinator = mods.uc.PstrykDataUpdateCoordinator(None, _FakeAPI(response), price_type)
    coordinator._cache_file = str(tmp_path / f"cache_{price_type}.json")
    coordinator.data = asyncio.run(coordinator._async_update_data())
    sensor = mods.sensor.PstrykJsonPriceSensor(coordinator, price_type, "entry")
    return coordinator, sensor


@pytest.mark.parametrize("price_type", ["buy", "sell"])
def test_entries_carry_components(mods, tmp_path, price_type):
    coordinator, sensor = _fetch(mods, tmp_path, REAL, price_type)
    attrs = sensor.extra_state_attributes
    assert len(attrs["prices_today"]) == 24
    assert len(attrs["prices_tomorrow"]) == 24
    for entry in attrs["prices"]:
        assert list(entry) == ["time", "price", *mods.pc.entry_keys(price_type)]
    cached = json.loads(Path(coordinator._cache_file).read_text())
    assert cached["prices"] == coordinator.data["prices"]


def test_buy_entry_at_13_local(mods, tmp_path):
    _, sensor = _fetch(mods, tmp_path, REAL, "buy")
    entry = sensor.extra_state_attributes["prices"][13]
    frame = REAL["frames"][13]
    # 11:00Z is 13:00 CEST.
    assert frame["start"] == "2026-10-02T11:00:00Z"
    assert entry["time"] == NOW
    pricing = frame["metrics"]["pricing"]
    assert entry["price"] == round(pricing["price_gross"], 2)
    assert entry["dist_price"] == 0.3214
    total = sum(entry[k] for k in mods.pc.BUY_COMPONENTS)
    assert total == pytest.approx(pricing["price_gross"], abs=1e-9)
    assert sensor.native_value == entry["price"]


@pytest.fixture
def early_clock():
    CLOCK[0] = EARLY_NOW
    yield
    CLOCK[0] = NOW


def test_unpublished_tomorrow(mods, tmp_path, early_clock):
    buy, buy_sensor = _fetch(mods, tmp_path, EARLY, "buy")
    assert len(buy.data["prices"]) == 24
    buy_attrs = buy_sensor.extra_state_attributes
    assert len(buy_attrs["prices_today"]) == 24
    assert buy_attrs["prices_tomorrow"] == []
    assert all(e["tge_price"] is not None for e in buy_attrs["prices_today"])

    sell, sell_sensor = _fetch(mods, tmp_path, EARLY, "sell")
    tomorrow = [p for p in sell.data["prices"] if p["start"].startswith("2026-10-05")]
    assert len(tomorrow) == 24
    assert all(p["price"] == 0.0 and p["tge_price"] is None for p in tomorrow)
    assert sell_sensor.extra_state_attributes["prices_tomorrow"] == []


def test_sell_entries_have_no_flags(mods, tmp_path):
    coordinator, sensor = _fetch(mods, tmp_path, REAL, "sell")
    assert any(p["is_expensive"] for p in coordinator.data["prices"])
    for entry in sensor.extra_state_attributes["prices"]:
        assert "is_cheap" not in entry and "is_expensive" not in entry


def test_old_cache_entry_gets_none(mods, tmp_path):
    coordinator, sensor = _fetch(mods, tmp_path, REAL, "buy")
    coordinator.data = {"prices": [
        {"start": "2026-10-02T13:00:00", "price": 1.1, "is_cheap": False, "is_expensive": True}
    ]}
    entry = sensor.extra_state_attributes["prices_today"][0]
    assert all(entry[k] is None for k in mods.pc.BUY_COMPONENTS)
    assert entry["is_expensive"] is True


def test_current_price_lists_unchanged(mods, tmp_path):
    coordinator, _ = _fetch(mods, tmp_path, REAL, "sell")
    price_sensor = mods.sensor.PstrykPriceSensor(coordinator, "sell", 5, 5, "entry")
    price_sensor.hass = types.SimpleNamespace(states=types.SimpleNamespace(get=lambda _: None))
    attrs = price_sensor.extra_state_attributes
    for key in ("All prices", "Best prices", "Worst prices"):
        assert all(set(p) == {"start", "price"} for p in attrs[key])
