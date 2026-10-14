"""API budget, price refresh, button, tomorrow checks and cost, with stand-ins for Home Assistant.

Home Assistant is not installed here, so the HA names these modules import are
replaced by small stubs (as in tests/test_json_entries.py on the price-components
branch). The real API client, coordinators, refresh, button, poller and sensor run
against a fake HTTP session that records when each request is sent.

The clock is local Europe/Warsaw. The fixture was fetched on 2026-10-02 at 12:05,
so 2026-10-02 afternoon has both days published.
"""
import asyncio
import copy
import importlib
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "pstryk"
FIXTURES = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("Europe/Warsaw")
CLOCK = [datetime(2026, 10, 2, 12, 5, tzinfo=TZ)]
PRICING = json.loads((FIXTURES / "api_pricing_20261002T120545.json").read_text())
# Fetched 2026-10-04 01:34: Oct 4 published, Oct 5 not (buy price null, sell 0.0).
EARLY = json.loads((FIXTURES / "api_pricing_20261004T013435.json").read_text())
CACHE = {}  # the integration's cache files, by price type

STORE_DISK = {}  # survives "restarts": one dict per store key
SCHEDULED = []  # (when_utc, callback) from async_track_point_in_time
NOTIFICATIONS = {}
SAVE_DELAYS_MS = []  # how long each request-log save takes on the fake clock; then 0
DURING_SAVE = []  # callables, one run inside each request-log save


def _set_clock(*args):
    CLOCK[0] = datetime(*args, tzinfo=TZ)


def _as_local(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.astimezone(TZ)


def _parse_datetime(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


class _Coordinator:
    def __init__(self, hass, logger, name=None, **kwargs):
        self.hass, self.name, self.data, self.last_update_success = hass, name, None, True

    def async_update_listeners(self):
        pass

    def async_add_listener(self, listener):
        return lambda: None

    def async_set_updated_data(self, data):
        self.data, self.last_update_success = data, True

    async def async_request_refresh(self):
        try:
            self.data = await self._async_update_data()
            self.last_update_success = True
        except Exception:
            self.last_update_success = False


class _Store:
    def __init__(self, hass, version, key):
        self.key = key

    async def async_load(self):
        return copy.deepcopy(STORE_DISK.get(self.key))

    async def async_save(self, data):
        STORE_DISK[self.key] = copy.deepcopy(data)
        if SAVE_DELAYS_MS:
            CLOCK[0] += timedelta(milliseconds=SAVE_DELAYS_MS.pop(0))
        if DURING_SAVE:
            DURING_SAVE.pop(0)()

    def async_delay_save(self, data_func, delay=0):
        STORE_DISK[self.key] = copy.deepcopy(data_func())


class _RestoreEntity:
    """What PstrykAveragePriceSensor uses of an entity; `last_state` is set by the test."""

    last_state = None
    writes = 0

    async def async_added_to_hass(self):
        pass

    async def async_get_last_state(self):
        return self.last_state

    def async_on_remove(self, func):
        pass

    def async_write_ha_state(self):
        self.writes += 1


def _track(hass, callback, when):
    item = (when, callback)
    SCHEDULED.append(item)
    return lambda: SCHEDULED.remove(item) if item in SCHEDULED else None


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
    notify = _stub(
        "homeassistant.components.persistent_notification",
        async_create=lambda hass, message, title=None, notification_id=None: NOTIFICATIONS.__setitem__(
            notification_id, message
        ),
        async_dismiss=lambda hass, notification_id: NOTIFICATIONS.pop(notification_id, None),
    )
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
            CoordinatorEntity=type("CoordinatorEntity", (), {"__init__": lambda self, c: None}),
        ),
        "homeassistant.helpers.event": _stub("homeassistant.helpers.event", async_track_point_in_time=_track),
        "homeassistant.helpers.storage": _stub("homeassistant.helpers.storage", Store=_Store),
        "homeassistant.helpers.restore_state": _stub(
            "homeassistant.helpers.restore_state", RestoreEntity=_RestoreEntity
        ),
        "homeassistant.helpers.aiohttp_client": _stub(
            "homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: hass.session
        ),
        "homeassistant.config_entries": _stub(
            "homeassistant.config_entries", ConfigEntry=type("ConfigEntry", (), {})
        ),
        "homeassistant.core": _stub(
            "homeassistant.core", HomeAssistant=type("HomeAssistant", (), {}), callback=lambda f: f
        ),
        "homeassistant.components": _stub("homeassistant.components", persistent_notification=notify),
        "homeassistant.components.persistent_notification": notify,
        "homeassistant.components.button": _stub(
            "homeassistant.components.button", ButtonEntity=type("ButtonEntity", (), {})
        ),
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
            api=importlib.import_module("pstryk.api_client"),
            uc=importlib.import_module("pstryk.update_coordinator"),
            cost=importlib.import_module("pstryk.energy_cost_coordinator"),
            refresh=importlib.import_module("pstryk.price_refresh"),
            poll=importlib.import_module("pstryk.tomorrow_poll"),
            sensor=importlib.import_module("pstryk.sensor"),
            button=importlib.import_module("pstryk.button"),
        )
    finally:
        for name in [n for n in sys.modules if n == "pstryk" or n.startswith("pstryk.")]:
            del sys.modules[name]
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class _Response:
    def __init__(self, status, body, headers=None, gate=None):
        self.status, self._body, self.headers, self._gate = status, body, headers or {}, gate

    def __await__(self):  # the config flow awaits session.get() directly
        return self.__aenter__().__await__()

    async def __aenter__(self):
        if self._gate is not None:
            await self._gate.wait()
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return copy.deepcopy(self._body)

    async def text(self):
        return "error"


class _Session:
    """Records the time of every HTTP request; serves queued statuses, then 200."""

    def __init__(self):
        self.sent = []
        self.statuses = []
        self.headers = {}
        self.pricing = PRICING
        self.gate = None  # an asyncio.Event holds responses until set
        self.cost_frames = False  # True: cost answers carry one frame valued by request number

    def _cost(self, url):
        if not self.cost_frames:
            return {"frames": []}
        n = len(self.sent)
        day = CLOCK[0].replace(hour=0, minute=0, second=0, microsecond=0)
        start = day if "resolution=day" in url else day.replace(day=1)
        return {"frames": [{
            "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "is_live": True,
            "energy_balance_value": n,
            "energy_sold_value": n,
            "fae_cost": 2 * n,
            "fae_usage": n,
            "rae": n,
        }]}

    def get(self, url, headers=None):
        self.sent.append(CLOCK[0])
        status = self.statuses.pop(0) if self.statuses else 200
        body = self.pricing if "metrics=pricing" in url else self._cost(url)
        return _Response(status, body, self.headers if status == 429 else None, self.gate)

    def max_in_any_hour(self):
        return max(
            (sum(1 for t in self.sent if s <= t < s + timedelta(hours=1)) for s in self.sent),
            default=0,
        )


class _Hass:
    def __init__(self):
        self.session = _Session()
        self.data = {"pstryk": {}}


@pytest.fixture
def env(mods, monkeypatch):
    STORE_DISK.clear()
    SCHEDULED.clear()
    NOTIFICATIONS.clear()
    CACHE.clear()
    SAVE_DELAYS_MS.clear()
    DURING_SAVE.clear()
    _set_clock(2026, 10, 2, 12, 5)
    hass = _Hass()

    async def _sleep(seconds):  # waiting for a slot moves the fake clock
        CLOCK[0] += timedelta(seconds=seconds)

    monkeypatch.setattr(mods.api, "_sleep", _sleep)

    async def build(entry_id="e"):
        client = mods.api.PstrykAPIClient(hass, "key")
        await client.async_load_budget()
        client._calculate_backoff = lambda attempt, base_delay=20.0: 0
        coords = {}
        for price_type in ("buy", "sell"):
            coord = mods.uc.PstrykDataUpdateCoordinator(hass, client, price_type, False, 5, 30, entry_id)

            async def _cache_write(data, price_type=price_type):
                CACHE[price_type] = copy.deepcopy(data)

            async def _cache_read(price_type=price_type):
                return copy.deepcopy(CACHE.get(price_type))

            coord._save_cache = _cache_write
            coord._load_cache = _cache_read
            hass.data["pstryk"][f"{entry_id}_{price_type}"] = coord
            coords[price_type] = coord
        return client, coords

    return types.SimpleNamespace(hass=hass, build=build)


def _run(coro):
    return asyncio.run(coro)


async def _two_more_calls(client):
    # Stand-in for any other traffic on the shared endpoint (e.g. cost).
    url = "https://api.pstryk.pl/integrations/meter-data/unified-metrics/?metrics=meter_values,cost"
    await client.fetch(url, max_retries=1)
    await client.fetch(url, max_retries=1)


def test_review_p1_refresh_waits_for_the_shared_budget(mods, env):
    async def scenario():
        client, coords = await env.build()
        await asyncio.gather(*(c.async_request_refresh() for c in coords.values()))  # 12:05:00, one request
        _set_clock(2026, 10, 2, 12, 5, 15)
        await _two_more_calls(client)
        _set_clock(2026, 10, 2, 12, 25, 5)
        blocked = await mods.refresh.async_refresh_prices(env.hass, "e", "test")
        sent_when_blocked = len(env.hass.session.sent)
        _set_clock(2026, 10, 2, 13, 5, 1)
        allowed = await mods.refresh.async_refresh_prices(env.hass, "e", "test")
        return blocked, sent_when_blocked, allowed

    blocked, sent_when_blocked, allowed = _run(scenario())
    assert blocked == "budget"
    assert sent_when_blocked == 3
    assert allowed == "found"
    assert len(env.hass.session.sent) == 4
    assert env.hass.session.max_in_any_hour() == 3


def test_budget_survives_a_restart(mods, env):
    async def scenario():
        client, coords = await env.build()
        await asyncio.gather(*(c.async_request_refresh() for c in coords.values()))
        await _two_more_calls(client)
        _set_clock(2026, 10, 2, 12, 30)
        env.hass.data["pstryk"].clear()
        await env.build()  # restart: a new client loads the saved request log
        return await mods.refresh.async_refresh_prices(env.hass, "e", "after restart")

    assert _run(scenario()) == "budget"
    assert len(env.hass.session.sent) == 3


def test_review_p2_concurrent_refreshes_keep_retries(mods, env):
    async def scenario():
        client, coords = await env.build()
        seen = []
        original = client._make_request

        async def spy(url, max_retries=3, base_delay=20.0, keep_free=0):
            seen.append(max_retries)
            return await original(url, max_retries, base_delay, keep_free)

        client._make_request = spy
        results = await asyncio.gather(
            mods.refresh.async_refresh_prices(env.hass, "e", "press 1"),
            mods.refresh.async_refresh_prices(env.hass, "e", "press 2"),
        )
        _set_clock(2026, 10, 2, 14, 0)
        await coords["buy"]._async_update_data()  # an ordinary scheduled fetch
        return results, seen, [c.retry_attempts for c in coords.values()]

    results, seen, retry_attempts = _run(scenario())
    assert results == ["found", "found"]
    assert retry_attempts == [5, 5]
    assert seen[-1] == 5
    assert all(r == 1 for r in seen[:-1])
    # Two presses ran one after the other; each one request (buy and sell deduplicated).
    assert len(env.hass.session.sent) == 3


def test_retries_count_and_stop_at_the_budget(mods, env):
    async def scenario():
        client, coords = await env.build()
        await coords["buy"].async_request_refresh()  # 12:05: request 1, real prices
        kept = coords["buy"].data["prices"]
        env.hass.session.statuses = [500, 500, 500, 500, 500]
        await coords["buy"].async_request_refresh()
        return coords["buy"], kept

    buy, kept = _run(scenario())
    assert len(env.hass.session.sent) == 3  # 5 retries configured, budget allows 2 more
    assert buy.data["prices"] == kept  # current data kept
    assert buy.last_update_success is True
    assert buy._unsub_budget_retry is not None  # a fetch is booked for when a slot frees
    # 12:05:00 + the hour + 2 s margin, then 5 s more
    assert SCHEDULED[-1][0] == datetime(2026, 10, 2, 13, 5, 7, tzinfo=TZ).astimezone(timezone.utc)


def test_cost_runs_only_on_leftover_budget(mods, env):
    async def scenario():
        client, coords = await env.build()
        cost = mods.cost.PstrykCostDataUpdateCoordinator(env.hass, client, 5, 30)
        cost.price_coordinators = list(coords.values())
        cost.schedule_hourly_update = lambda: None
        _set_clock(2026, 10, 2, 12, 50)
        await cost._handle_hourly_update(None)  # tomorrow missing: prices first
        at_1250 = len(env.hass.session.sent)
        _set_clock(2026, 10, 2, 13, 0)
        price = await mods.refresh.async_refresh_prices(env.hass, "e", "tomorrow check")
        _set_clock(2026, 10, 2, 15, 50)
        await cost._handle_hourly_update(None)  # tomorrow cached: daily + yearly
        at_1550 = len(env.hass.session.sent)
        _set_clock(2026, 10, 2, 16, 20)
        await cost._handle_hourly_update(None)  # 2 used: the last slot stays for prices
        at_1620 = len(env.hass.session.sent)
        return at_1250, price, at_1550, at_1620

    at_1250, price, at_1550, at_1620 = _run(scenario())
    assert (at_1250, price, at_1550, at_1620) == (0, "found", 3, 3)
    assert env.hass.session.max_in_any_hour() <= 3


def test_button_reports_a_full_budget(mods, env):
    async def scenario():
        client, coords = await env.build()
        await _two_more_calls(client)
        await client.fetch(
            "https://api.pstryk.pl/integrations/meter-data/unified-metrics/?metrics=cost", max_retries=1
        )
        button = mods.button.PstrykRefreshPricesButton("e")
        button.hass = env.hass
        await button.async_press()

    _run(scenario())
    assert len(env.hass.session.sent) == 3
    assert "13:05" in NOTIFICATIONS["pstryk_refresh_budget"]


def test_tomorrow_checks_until_found_within_budget(mods, env):
    async def scenario():
        client, coords = await env.build()
        poller = mods.poll.PstrykTomorrowPoller(env.hass, "e")
        results = []
        # Before publication the API answers 500 (as a stand-in for "nothing yet").
        env.hass.session.statuses = [500, 500, 500]
        for minute in (10, 30, 50):
            _set_clock(2026, 10, 2, 12, minute)
            results.append(await poller.async_check())
        for minute in (10, 30):
            _set_clock(2026, 10, 2, 13, minute)
            results.append(await poller.async_check())
        return results

    results = _run(scenario())
    assert results == ["failed", "failed", "failed", "found", "found"]
    assert len(env.hass.session.sent) == 4  # one per check, no retries; none after found
    assert env.hass.session.max_in_any_hour() == 3


def test_tomorrow_check_waits_on_a_full_budget(mods, env):
    async def scenario():
        client, coords = await env.build()
        _set_clock(2026, 10, 2, 11, 55)
        await _two_more_calls(client)
        await client.fetch(
            "https://api.pstryk.pl/integrations/meter-data/unified-metrics/?metrics=cost", max_retries=1
        )
        poller = mods.poll.PstrykTomorrowPoller(env.hass, "e")
        _set_clock(2026, 10, 2, 12, 10)
        first = await poller.async_check()
        _set_clock(2026, 10, 2, 12, 55, 1)
        second = await poller.async_check()
        return first, second

    assert _run(scenario()) == ("budget", "found")
    assert len(env.hass.session.sent) == 4
    assert env.hass.session.max_in_any_hour() == 3


def test_prices_fetched_sensor_shows_the_fetch_time(mods, env):
    async def scenario():
        client, coords = await env.build()
        sensor = mods.sensor.PstrykPricesFetchedSensor(coords["buy"], "e")
        sensor.coordinator = coords["buy"]
        before = sensor.native_value
        _set_clock(2026, 10, 2, 12, 7, 30)
        await coords["buy"].async_request_refresh()
        fetched = sensor.native_value
        _set_clock(2026, 10, 2, 13, 0)
        coords["buy"].data = {**coords["buy"].data, "is_cached": True}  # hourly tick from cache
        return before, fetched, sensor.native_value, sensor.extra_state_attributes

    before, fetched, later, attrs = _run(scenario())
    assert before is None
    assert fetched == datetime(2026, 10, 2, 12, 7, 30, tzinfo=TZ)
    assert later == fetched
    assert attrs == {"tomorrow_available": True}


def _used(env):
    """Requests in the saved log within the last hour (what the integration believes)."""
    log = STORE_DISK.get("pstryk_request_log", {}).get("unified-metrics", {"times": []})
    now = CLOCK[0].astimezone(timezone.utc)
    return sum(1 for t in log["times"] if now - datetime.fromisoformat(t) < timedelta(hours=1))


def test_review2_p1_api_key_check_counts(mods, env):
    async def scenario():
        env.hass.session.pricing = EARLY
        _set_clock(2026, 10, 4, 12, 5)
        valid = await mods.api.async_validate_api_key(env.hass, "key")  # config flow
        client, coords = await env.build()  # fresh setup: no cache, fetch
        await asyncio.gather(*(c.async_load_startup_cache() for c in coords.values()))
        await asyncio.gather(*(c.async_request_refresh() for c in coords.values()))
        poller = mods.poll.PstrykTomorrowPoller(env.hass, "e")
        results = []
        for minute in (10, 30):
            _set_clock(2026, 10, 4, 12, minute)
            results.append(await poller.async_check())
        return valid, results

    valid, results = _run(scenario())
    assert valid is True
    assert results == ["no_tomorrow", "budget"]
    assert len(env.hass.session.sent) == 3
    assert _used(env) == len(env.hass.session.sent)


def test_api_key_check_waits_for_a_full_budget(mods, env):
    async def scenario():
        client, coords = await env.build()
        await _two_more_calls(client)
        await client.fetch(
            "https://api.pstryk.pl/integrations/meter-data/unified-metrics/?metrics=cost", max_retries=1
        )
        return await mods.api.async_validate_api_key(env.hass, "key")

    assert _run(scenario()) is None
    assert len(env.hass.session.sent) == 3


def test_review2_p2_reload_during_a_request_shares_one_budget(mods, env):
    async def scenario():
        client, coords = await env.build()
        await asyncio.gather(*(c.async_request_refresh() for c in coords.values()))  # request 1
        for c in coords.values():
            c._has_tomorrow = False  # pretend tomorrow is still missing
        env.hass.session.gate = asyncio.Event()
        old_poller = mods.poll.PstrykTomorrowPoller(env.hass, "e")
        _set_clock(2026, 10, 2, 12, 10)
        press = asyncio.create_task(mods.refresh.async_refresh_prices(env.hass, "e", "button"))  # request 2
        await asyncio.sleep(0)
        queued = asyncio.create_task(old_poller.async_check())  # waits for the lock
        await asyncio.sleep(0)
        # Reload: the old generation is stopped and dropped; the request log stays.
        old_poller.stop()
        for key in [k for k in env.hass.data["pstryk"] if k.startswith("e_")]:
            del env.hass.data["pstryk"][key]
        env.hass.session.gate.set()
        _, new_coords = await env.build()
        CACHE.clear()  # force the replacement's start-up fetch
        await asyncio.gather(*(c.async_load_startup_cache() for c in new_coords.values()))
        await asyncio.gather(*(c.async_request_refresh() for c in new_coords.values()))  # request 3
        return await press, await queued

    press, queued = _run(scenario())
    assert press == "found"
    assert queued == "stopped"
    assert len(env.hass.session.sent) == 3
    assert _used(env) == 3


def test_review2_p3_unpublished_rows_are_not_todays_prices(mods, env):
    async def scenario():
        env.hass.session.pricing = EARLY
        _set_clock(2026, 10, 4, 1, 34, 35)
        client, coords = await env.build()
        await asyncio.gather(*(c.async_request_refresh() for c in coords.values()))  # cache Oct 4/5
        env.hass.data["pstryk"].clear()
        _set_clock(2026, 10, 5, 9, 0)
        client, coords = await env.build()  # restart on Oct 5
        loaded = {t: await c.async_load_startup_cache() for t, c in coords.items()}
        # The budget is full, so the start-up fetch cannot replace the rows.
        await _two_more_calls(client)
        await client.fetch(
            "https://api.pstryk.pl/integrations/meter-data/unified-metrics/?metrics=cost", max_retries=1
        )
        await coords["sell"].async_request_refresh()
        return loaded, coords["sell"]

    loaded, sell = _run(scenario())
    assert loaded == {"buy": False, "sell": False}
    assert sell.data is None
    assert sell.last_update_success is False  # unavailable, not a 0.0 price


def test_review2_p4_restart_after_midnight_rebuilds_today(mods, env):
    async def scenario():
        _set_clock(2026, 10, 2, 13, 0)
        client, coords = await env.build()
        await asyncio.gather(*(c.async_request_refresh() for c in coords.values()))  # cache Oct 2/3
        sent = len(env.hass.session.sent)
        env.hass.data["pstryk"].clear()
        STORE_DISK.clear()
        _set_clock(2026, 10, 3, 9, 0)
        client, coords = await env.build()
        loaded = await coords["sell"].async_load_startup_cache()
        return sent, loaded, coords["sell"].data

    sent, loaded, data = _run(scenario())
    assert loaded is True
    assert len(env.hass.session.sent) == sent  # no request at start-up
    assert len(data["prices_today"]) == 24
    assert all(p["start"].startswith("2026-10-03") for p in data["prices_today"])


def test_review2_p5_button_explains_a_429_block(mods, env):
    async def scenario():
        client, coords = await env.build()
        env.hass.session.statuses = [429]
        env.hass.session.headers = {"Retry-After": "3600"}
        poller = mods.poll.PstrykTomorrowPoller(env.hass, "e")
        for c in coords.values():
            c._has_tomorrow = False
        _set_clock(2026, 10, 2, 12, 10)
        poll = await poller.async_check()
        button = mods.button.PstrykRefreshPricesButton("e")
        button.hass = env.hass
        _set_clock(2026, 10, 2, 12, 15)
        await button.async_press()
        return poll

    assert _run(scenario()) == "failed"
    assert len(env.hass.session.sent) == 1  # the press sent nothing
    assert "13:10" in NOTIFICATIONS["pstryk_refresh_budget"]



# Third review: schedules that repeat on the hour meet their own previous request.


def _at(day, hour, minute, second=0, ms=0):
    CLOCK[0] = datetime(2026, 10, day, hour, minute, second, ms * 1000, tzinfo=TZ)


def _slow(session, ms=400):
    """Every request takes `ms` on the fake clock."""
    get = session.get

    def slow_get(url, headers=None):
        response = get(url, headers)
        CLOCK[0] += timedelta(milliseconds=ms)
        return response

    session.get = slow_get


def test_review3_checks_an_hour_apart_wait_for_the_slot(mods, env):
    # Timers fire a few ms off; a check may come just before last hour's request leaves the window.
    # The whole late day, 12:10 to 23:50, with the timer 5-50 ms late.
    times = [(h, m, 50 - (7 * (3 * h + m // 20)) % 46) for h in range(12, 24) for m in (10, 30, 50)]

    async def scenario():
        env.hass.session.pricing = EARLY  # Oct 5 stays unpublished
        _at(4, 12, 9)
        client, coords = await env.build()
        poller = mods.poll.PstrykTomorrowPoller(env.hass, "e")
        results = []
        for hour, minute, ms in times:
            _at(4, hour, minute, 0, ms)
            results.append(await poller.async_check())
        return results

    assert _run(scenario()) == ["no_tomorrow"] * len(times)
    assert len(env.hass.session.sent) == len(times)
    assert env.hass.session.max_in_any_hour() == 3


def test_review3_cost_runs_every_hour(mods, env):
    async def scenario():
        client, coords = await env.build()
        await asyncio.gather(*(c.async_request_refresh() for c in coords.values()))  # 12:05, tomorrow cached
        cost = mods.cost.PstrykCostDataUpdateCoordinator(env.hass, client, 1, 30)
        cost.price_coordinators = list(coords.values())
        cost.schedule_hourly_update = lambda: None
        _slow(env.hass.session)
        runs = []
        # 13:50 to 22:50. Each run starts when the last one's slots free, about 2 s later every hour.
        for hour, ms in [(h, 30 - (11 * h) % 27) for h in range(13, 23)]:
            _at(2, hour, 50, 0, ms)
            before = len(env.hass.session.sent)
            cost.data = None
            await cost._handle_hourly_update(None)
            runs.append((len(env.hass.session.sent) - before, sorted(cost.data or {})))
        return runs

    assert _run(scenario()) == [(2, ["daily", "monthly", "yearly"])] * 10
    assert env.hass.session.max_in_any_hour() <= 3


def _new_cost(mods, env, client, coords, entry_id="e"):
    cost = mods.cost.PstrykCostDataUpdateCoordinator(env.hass, client, 1, 30, entry_id)
    cost.price_coordinators = list(coords.values())
    cost.schedule_hourly_update = lambda: None
    cost.last_update_success = False  # as sensor.py sets it before restoring
    return cost


async def _cost_runs_then_restart(mods, env, restart_at, entry_id="e"):
    """Full run at 13:50 on Oct 2, daily only at 14:50, then a restart at `restart_at`.

    Cost frames carry their request number: 12:05 prices is 1, 13:50 daily 2 and
    yearly 3, the other call at 14:30 is 4, 14:50 daily 5.
    """
    env.hass.session.cost_frames = True
    client, coords = await env.build()
    await asyncio.gather(*(c.async_request_refresh() for c in coords.values()))  # 12:05, tomorrow cached
    cost = _new_cost(mods, env, client, coords)
    _set_clock(2026, 10, 2, 13, 50)
    await cost._handle_hourly_update(None)  # daily + yearly
    full = copy.deepcopy(cost.data)
    _set_clock(2026, 10, 2, 14, 30)
    await client.fetch(  # another call leaves room for daily only at 14:50
        "https://api.pstryk.pl/integrations/meter-data/unified-metrics/?metrics=cost", max_retries=1
    )
    _set_clock(2026, 10, 2, 14, 50)
    await cost._handle_hourly_update(None)
    sent = len(env.hass.session.sent)
    env.hass.data["pstryk"].clear()
    _set_clock(*restart_at)
    client, coords = await env.build()  # restart
    restarted = _new_cost(mods, env, client, coords, entry_id)
    await restarted.async_restore()
    return full, sent, restarted, coords


def test_cost_data_survives_a_restart_without_a_request(mods, env):
    full, sent, restarted, _ = _run(_cost_runs_then_restart(mods, env, (2026, 10, 2, 15, 0)))
    assert sorted(full) == ["daily", "monthly", "yearly"]
    assert full["monthly"]["total_balance"] == 3
    assert sent == 5  # the yearly call at 14:50 was refused
    assert len(env.hass.session.sent) == sent  # restoring sent nothing
    assert restarted.last_update_success is True
    assert restarted.data["daily"]["total_balance"] == 5  # from 14:50
    assert restarted.data["monthly"] == full["monthly"]  # from 13:50, kept through the partial run
    assert restarted.data["yearly"] == full["yearly"]


def test_cost_data_of_another_entry_is_not_restored(mods, env):
    _, _, restarted, _ = _run(_cost_runs_then_restart(mods, env, (2026, 10, 2, 15, 0), entry_id="other"))
    assert restarted.data is None
    assert restarted.last_update_success is False


def _average(mods, cost, coords, period, last_state=None):
    sensor = mods.sensor.PstrykAveragePriceSensor(cost, coords["buy"], period, "e")
    sensor.last_state = last_state
    _run(sensor.async_added_to_hass())
    return sensor


def test_averages_after_midnight_use_only_their_own_period(mods, env):
    _, _, restarted, coords = _run(_cost_runs_then_restart(mods, env, (2026, 10, 3, 0, 5)))
    yesterday = types.SimpleNamespace(state="1.5", attributes={})
    daily = _average(mods, restarted, coords, "daily", yesterday)
    monthly = _average(mods, restarted, coords, "monthly", yesterday)
    assert sorted(restarted.data) == ["monthly", "yearly"]
    assert daily.available is False  # yesterday's 1.5 is not shown as today's
    assert monthly.available is True
    assert monthly.native_value == 2.0  # from the restored month, not the entity's 1.5


def test_averages_without_entity_state_come_from_restored_cost(mods, env):
    _, _, restarted, coords = _run(_cost_runs_then_restart(mods, env, (2026, 10, 2, 15, 0)))
    daily = _average(mods, restarted, coords, "daily")
    assert daily.available is True
    assert daily.native_value == 2.0


@pytest.mark.parametrize("now, periods", [
    ((2026, 10, 2, 23, 59), ["daily", "monthly", "yearly"]),
    ((2026, 10, 3, 0, 5), ["monthly", "yearly"]),
    ((2026, 11, 1, 0, 5), ["yearly"]),
    ((2027, 1, 1, 0, 5), []),
])
def test_restored_cost_data_stays_within_its_period(mods, env, now, periods):
    # Fetched 2026-10-02 13:50 local (11:50Z); periods end at local midnight.
    entry = {"fetched": "2026-10-02T11:50:00+00:00", "data": {"total_balance": 1.0}}
    saved = {period: entry for period in ("daily", "monthly", "yearly")}
    assert sorted(mods.cost.restorable_periods(saved, datetime(*now, tzinfo=TZ))) == periods


def test_nothing_saved_leaves_cost_unavailable(mods, env):
    async def scenario():
        client, coords = await env.build()
        cost = _new_cost(mods, env, client, coords)
        await cost.async_restore()
        return cost

    cost = _run(scenario())
    assert cost.data is None
    assert cost.last_update_success is False


def test_a_slot_further_away_is_not_waited_for(mods, env):
    async def scenario():
        client, coords = await env.build()
        await _two_more_calls(client)
        await coords["buy"].async_request_refresh()  # 12:05: the third
        _at(2, 13, 4, 0)  # the slot frees in a minute
        return await mods.refresh.async_refresh_prices(env.hass, "e", "test")

    assert _run(scenario()) == "budget"
    assert len(env.hass.session.sent) == 3


async def _restart_with_today_cached(env, attempts=5):
    """Cache fetched Oct 4 09:00 (Oct 5 unpublished), restart at 12:06: only tomorrow is missing."""
    env.hass.session.pricing = EARLY
    _at(4, 9, 0)
    await asyncio.gather(*(c.async_request_refresh() for c in (await env.build())[1].values()))
    env.hass.data["pstryk"].clear()
    STORE_DISK.clear()
    _at(4, 12, 6)
    client, coords = await env.build()
    for coord in coords.values():
        coord.retry_attempts = attempts
    return coords


def test_review3_startup_fetch_for_tomorrow_is_one_attempt(mods, env):
    async def scenario():
        coords = await _restart_with_today_cached(env)
        env.hass.session.statuses = [500] * 5
        before = len(env.hass.session.sent)
        await asyncio.gather(*(c.async_startup() for c in coords.values()))
        at_startup = len(env.hass.session.sent) - before
        poller = mods.poll.PstrykTomorrowPoller(env.hass, "e")
        env.hass.session.statuses = []
        _at(4, 12, 10)
        return at_startup, await poller.async_check(), coords["buy"]

    at_startup, check, buy = _run(scenario())
    assert at_startup == 1  # not the configured 5 attempts
    assert check == "no_tomorrow"  # the 12:10 check still has a slot
    assert buy.last_update_success is True
    assert len(buy.data["prices_today"]) == 24


@pytest.mark.parametrize("attempts, statuses", [(2, [500, 500]), (5, [401])])
def test_review3_failed_startup_fetch_keeps_todays_cached_prices(mods, env, attempts, statuses):
    async def scenario():
        coords = await _restart_with_today_cached(env, attempts)
        env.hass.session.statuses = list(statuses)
        await asyncio.gather(*(c.async_startup() for c in coords.values()))
        return coords["buy"]

    buy = _run(scenario())
    assert buy.last_update_success is True
    assert buy.today_usable(buy.data)


def test_startup_without_a_cache_fetches_with_retries(mods, env):
    async def scenario():
        client, coords = await env.build()
        env.hass.session.statuses = [500]
        await asyncio.gather(*(c.async_startup() for c in coords.values()))
        return coords["buy"]

    buy = _run(scenario())
    assert len(env.hass.session.sent) == 2
    assert buy.last_update_success is True and buy._has_tomorrow


def test_review3_hour_tick_keeps_yesterdays_fetch_with_todays_prices(mods, env):
    async def scenario():
        _at(2, 13, 0)
        client, coords = await env.build()
        await asyncio.gather(*(c.async_request_refresh() for c in coords.values()))  # cache Oct 2/3
        env.hass.data["pstryk"].clear()
        STORE_DISK.clear()
        _at(3, 9, 0)
        client, coords = await env.build()
        sell = coords["sell"]
        sell.schedule_hourly_update = lambda: None
        await sell.async_startup()
        sent = len(env.hass.session.sent)
        _at(3, 10, 0, 5)
        await sell._handle_hourly_update(None)
        return len(env.hass.session.sent) - sent, sell

    sent, sell = _run(scenario())
    assert sent == 0
    assert not CACHE["sell"].get("invalid")
    assert sell.last_update_success is True
    assert sell._has_tomorrow is False  # the cached flag was about Oct 3


def test_hour_tick_still_rejects_a_cache_without_todays_prices(mods, env):
    async def scenario():
        _at(2, 13, 0)
        client, coords = await env.build()
        sell = coords["sell"]
        sell.schedule_hourly_update = lambda: None
        await sell.async_request_refresh()
        sent = len(env.hass.session.sent)
        _at(4, 10, 0, 5)  # two days on: nothing for Oct 4 in it
        await sell._handle_hourly_update(None)
        return len(env.hass.session.sent) - sent

    assert _run(scenario()) == 1


def test_review3_api_key_check_429_blocks_the_budget(mods, env):
    async def scenario():
        client, coords = await env.build()
        env.hass.session.statuses = [429]
        env.hass.session.headers = {"Retry-After": "1800"}
        valid = await mods.api.async_validate_api_key(env.hass, "key")
        _at(2, 12, 6)
        return valid, await mods.refresh.async_refresh_prices(env.hass, "e", "test"), client.budget_free_at()

    valid, refresh, free_at = _run(scenario())
    assert valid is None  # "wait", not "invalid key"
    assert refresh == "budget"
    assert len(env.hass.session.sent) == 1
    assert free_at == datetime(2026, 10, 2, 12, 35, tzinfo=TZ)


def test_retry_after_header(mods):
    now = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    assert mods.api.retry_after_seconds({"Retry-After": "120"}, now) == 120
    assert mods.api.retry_after_seconds({"Retry-After": "Fri, 02 Oct 2026 10:30:00 GMT"}, now) == 1800
    assert mods.api.retry_after_seconds({"Retry-After": "soon"}, now) == 3600
    assert mods.api.retry_after_seconds({}, now) == 3600


def test_review3_button_reports_a_failed_refresh(mods, env):
    async def scenario():
        client, coords = await env.build()
        env.hass.session.statuses = [500]
        button = mods.button.PstrykRefreshPricesButton("e")
        button.hass = env.hass
        await button.async_press()
        failed = dict(NOTIFICATIONS)
        await button.async_press()
        return failed

    failed = _run(scenario())
    assert "pstryk_refresh_failed" in failed
    assert NOTIFICATIONS == {}  # a good press clears it


# Fourth review (Codex on 3a9acb9).


def test_review4_a_slot_frees_an_hour_after_the_request_was_sent(mods, env):
    # The log save sits between claiming a slot and sending; it took 200 ms for the first request.
    async def scenario():
        env.hass.session.pricing = EARLY
        _at(4, 12, 9)
        client, coords = await env.build()
        poller = mods.poll.PstrykTomorrowPoller(env.hass, "e")
        SAVE_DELAYS_MS.extend([200, 10, 10, 10])
        for hour, minute in [(12, 10), (12, 30), (12, 50), (13, 10)]:
            _at(4, hour, minute, 0, 100)
            await poller.async_check()

    _run(scenario())
    sent = env.hass.session.sent
    assert len(sent) == 4
    assert sent[0] == datetime(2026, 10, 4, 12, 10, 0, 300000, tzinfo=TZ)
    assert sent[3] - sent[0] >= timedelta(hours=1, seconds=2)
    assert env.hass.session.max_in_any_hour() == 3


def test_review4_unload_during_a_slot_wait_sends_nothing(mods, env, monkeypatch):
    async def scenario():
        env.hass.session.pricing = EARLY
        _at(4, 12, 9)
        client, coords = await env.build()
        poller = mods.poll.PstrykTomorrowPoller(env.hass, "e")
        for hour, minute in [(12, 10), (12, 30), (12, 50)]:
            _at(4, hour, minute, 0, 100)
            await poller.async_check()

        async def unload_while_waiting(seconds):
            CLOCK[0] += timedelta(seconds=seconds)
            client.close()  # what _cleanup_coordinators does

        monkeypatch.setattr(mods.api, "_sleep", unload_while_waiting)
        _at(4, 13, 10, 0, 50)
        return await poller.async_check(), _used(env)

    result, used = _run(scenario())
    assert result == "stopped"
    assert len(env.hass.session.sent) == 3
    assert used == 2  # no slot taken for it either (12:10 has left the window)


def test_review4_retired_coordinator_neither_fetches_nor_books_a_retry(mods, env):
    async def scenario():
        client, coords = await env.build()
        await _two_more_calls(client)
        await coords["buy"].async_request_refresh()  # budget full
        SCHEDULED.clear()
        client.close()
        await coords["buy"].async_request_refresh()
        coords["buy"]._arm_budget_retry(None)
        _set_clock(2026, 10, 2, 14, 0)
        return await coords["buy"].async_fetch_once()

    assert _run(scenario()) == "stopped"
    assert SCHEDULED == []
    assert len(env.hass.session.sent) == 3
    assert CACHE["buy"]["owner"] == "e"


def test_review4_another_entrys_cache_is_not_trusted(mods, env):
    async def scenario():
        _at(2, 13, 0)
        client, coords = await env.build("old")
        await asyncio.gather(*(c.async_request_refresh() for c in coords.values()))
        env.hass.data["pstryk"].clear()  # entry deleted; its cache files stay
        _at(2, 15, 0)
        client, coords = await env.build("new")
        loaded = await coords["buy"].async_load_startup_cache()
        exposed = coords["buy"].data
        await asyncio.gather(*(c.async_startup() for c in coords.values()))
        return loaded, exposed, coords["buy"]

    loaded, exposed, buy = _run(scenario())
    assert loaded is False and exposed is None
    assert len(env.hass.session.sent) == 2  # the new entry fetched its own prices
    assert CACHE["buy"]["owner"] == "new"
    assert buy.last_update_success is True


def test_review4_a_cache_without_an_owner_is_not_trusted(mods, env):
    async def scenario():
        _at(2, 13, 0)
        client, coords = await env.build()
        await asyncio.gather(*(c.async_request_refresh() for c in coords.values()))
        for cached in CACHE.values():
            del cached["owner"]  # written by an older version
        env.hass.data["pstryk"].clear()
        _at(2, 15, 0)
        client, coords = await env.build()
        sell = coords["sell"]
        sell.schedule_hourly_update = lambda: None
        loaded = await sell.async_load_startup_cache()
        await sell._handle_hourly_update(None)  # no data: the tick must not take the file either
        return loaded

    assert _run(scenario()) is False
    assert len(env.hass.session.sent) == 2


# Fifth review (Codex on 4f2a2ef).


def test_review5_unload_during_the_log_save_sends_nothing(mods, env):
    async def scenario():
        env.hass.session.pricing = EARLY
        _at(4, 12, 9)
        client, coords = await env.build()
        poller = mods.poll.PstrykTomorrowPoller(env.hass, "e")
        _at(4, 12, 10, 0, 100)
        await poller.async_check()
        DURING_SAVE.append(client.close)  # unloaded while the claim is being saved
        _at(4, 12, 30, 0, 100)
        return await poller.async_check()

    assert _run(scenario()) == "stopped"
    assert len(env.hass.session.sent) == 1
    assert _used(env) == 1  # the slot it had claimed is given back, on disk too


def test_review5_a_restart_restores_the_send_time(mods, env):
    # The save before the third request takes 3 s, longer than the margin.
    async def scenario(restart):
        env.hass.session.pricing = EARLY
        _at(4, 12, 9)
        client, coords = await env.build()
        poller = mods.poll.PstrykTomorrowPoller(env.hass, "e")
        SAVE_DELAYS_MS.extend([10, 10, 3000])
        for minute in (10, 30, 50):
            _at(4, 12, minute, 0, 100)
            await poller.async_check()
        if restart:
            env.hass.data["pstryk"].clear()
            client, coords = await env.build()
            poller = mods.poll.PstrykTomorrowPoller(env.hass, "e")
        for minute in (10, 30, 50):
            _at(4, 13, minute, 0, 100)
            await poller.async_check()
        return list(env.hass.session.sent)

    for restart in (True, False):
        env.hass.session.sent.clear()
        STORE_DISK.clear()
        env.hass.data["pstryk"].clear()
        sent = _run(scenario(restart))
        assert len(sent) == 6
        assert sent[2] == datetime(2026, 10, 4, 12, 50, 3, 100000, tzinfo=TZ)
        assert sent[5] - sent[2] >= timedelta(hours=1, seconds=2)
        assert env.hass.session.max_in_any_hour() == 3
