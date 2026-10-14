import logging
import asyncio
import random
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from email.utils import parsedate_to_datetime

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    API_TIMEOUT,
    API_URL,
    API_HOURLY_LIMIT,
    API_SLOT_WAIT_SECONDS,
    API_WINDOW_MARGIN_SECONDS,
    DOMAIN,
    PRICING_ENDPOINT,
    REQUEST_LOG_STORE_VERSION,
)
from .request_budget import WINDOW, RequestBudget

_LOGGER = logging.getLogger(__name__)

DEFAULT_RETRY_AFTER_SECONDS = 3600
BUDGET_WINDOW = WINDOW + timedelta(seconds=API_WINDOW_MARGIN_SECONDS)


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def retry_after_seconds(headers, now: datetime) -> float:
    """Seconds to wait after a 429: Retry-After as seconds or an HTTP date, else an hour."""
    value = headers.get("Retry-After")
    if value:
        try:
            return int(value)
        except ValueError:
            try:
                return (parsedate_to_datetime(value) - now).total_seconds()
            except Exception:
                pass
    return DEFAULT_RETRY_AFTER_SECONDS


async def async_claim_slot(budget: RequestBudget, keep_free: int = 0, still_wanted=None) -> datetime | None:
    """Record a request if the budget has room, or will have within API_SLOT_WAIT_SECONDS.

    Returns the recorded time, to be moved to the send time with `restamp`, or
    None if there is no room. `still_wanted` (optional) is called after a wait
    and raises if the caller was retired meanwhile.
    """
    now = dt_util.utcnow()
    if not budget.claim(now, keep_free):
        free_at = budget.free_at(now, keep_free)
        wait = (free_at - now).total_seconds() if free_at else None
        if wait is None or wait > API_SLOT_WAIT_SECONDS:
            return None
        await _sleep(wait + 0.05)
        if still_wanted is not None:
            still_wanted()
        now = dt_util.utcnow()
        if not budget.claim(now, keep_free):
            return None
    return now


class BudgetExhausted(UpdateFailed):
    """No request was sent: the hourly API budget has no room for it."""

    def __init__(self, endpoint_key: str, free_at):
        self.endpoint_key = endpoint_key
        self.free_at = free_at
        super().__init__(
            f"API budget for {endpoint_key} is full ({API_HOURLY_LIMIT}/h); "
            f"next slot at {dt_util.as_local(free_at).strftime('%H:%M:%S') if free_at else 'unknown'}"
        )


class ClientClosed(UpdateFailed):
    """No request was sent: the entry this client belonged to was unloaded or reloaded."""


class SharedRequestLog:
    """The request history for the whole Home Assistant instance, kept in .storage.

    Every client generation (a reload replaces the client while old requests may
    still be running) and the config flow's API-key check count against this one
    object, so they can never hold separate copies of the history.
    """

    def __init__(self, hass: HomeAssistant):
        self._store = Store(hass, REQUEST_LOG_STORE_VERSION, f"{DOMAIN}_request_log")
        self._budgets: Dict[str, RequestBudget] = {}
        self._loaded = False
        self._load_lock = asyncio.Lock()

    async def async_load(self) -> None:
        async with self._load_lock:
            if self._loaded:
                return
            data = await self._store.async_load() or {}
            for endpoint_key, value in data.items():
                self._budgets[endpoint_key] = RequestBudget.from_dict(value, API_HOURLY_LIMIT, BUDGET_WINDOW)
            self._loaded = True
            _LOGGER.debug("Loaded API request log: %s", {
                key: budget.used(dt_util.utcnow()) for key, budget in self._budgets.items()
            })

    def budget(self, endpoint_key: str) -> RequestBudget:
        if endpoint_key not in self._budgets:
            self._budgets[endpoint_key] = RequestBudget(API_HOURLY_LIMIT, BUDGET_WINDOW)
        return self._budgets[endpoint_key]

    def _data(self) -> dict:
        now = dt_util.utcnow()
        return {key: b.to_dict(now) for key, b in self._budgets.items()}

    async def async_save(self) -> None:
        try:
            await self._store.async_save(self._data())
        except Exception as err:
            _LOGGER.warning("Failed to save the API request log: %s", err)

    def save_soon(self) -> None:
        """Save without waiting for it: within a second, or when Home Assistant stops.

        Used at the moment of sending, where an awaited save would put the
        request later than its recorded time again.
        """
        self._store.async_delay_save(self._data, 1)


REQUEST_LOG_KEY = "request_log"


async def async_get_request_log(hass: HomeAssistant) -> SharedRequestLog:
    data = hass.data.setdefault(DOMAIN, {})
    request_log = data.get(REQUEST_LOG_KEY)
    if request_log is None:
        request_log = data[REQUEST_LOG_KEY] = SharedRequestLog(hass)
    await request_log.async_load()
    return request_log


async def async_validate_api_key(hass: HomeAssistant, api_key: str) -> bool | None:
    """One pricing request to check the key, counted in the shared log. None if there is no room."""
    now = dt_util.utcnow()
    url = API_URL + PRICING_ENDPOINT.format(
        start=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    request_log = await async_get_request_log(hass)
    budget = request_log.budget("unified-metrics")
    claimed = await async_claim_slot(budget)
    if claimed is None:
        return None
    await request_log.async_save()
    try:
        session = async_get_clientsession(hass)
        async with asyncio.timeout(API_TIMEOUT):
            budget.restamp(claimed, dt_util.utcnow())
            request_log.save_soon()
            resp = await session.get(url, headers={"Authorization": api_key, "Accept": "application/json"})
    except (Exception, asyncio.TimeoutError):
        return False
    if resp.status == 429:
        # Pstryk asks us to wait; that says nothing about the key.
        now = dt_util.utcnow()
        budget.block_until(now + timedelta(seconds=retry_after_seconds(resp.headers, now)))
        await request_log.async_save()
        return None
    return resp.status == 200


class PstrykAPIClient:

    def __init__(self, hass: HomeAssistant, api_key: str):
        self.hass = hass
        self.api_key = api_key

        # Every HTTP attempt is counted in the instance-wide request log.
        self._request_log: Optional[SharedRequestLog] = None
        self._session: Optional[aiohttp.ClientSession] = None

        self._rate_limits: Dict[str, Dict[str, Any]] = {}
        self._rate_limit_lock = asyncio.Lock()

        self._request_semaphore = asyncio.Semaphore(3)

        self._in_flight: Dict[str, asyncio.Task] = {}
        self._in_flight_lock = asyncio.Lock()

        self.closed = False

    def close(self) -> None:
        """The entry is unloading: work still waiting (a slot, a retry) sends nothing more."""
        self.closed = True

    def _check_open(self) -> None:
        if self.closed:
            raise ClientClosed("Pstryk entry was unloaded; request not sent")

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = async_get_clientsession(self.hass)
        return self._session

    def _get_endpoint_key(self, url: str) -> str:
        if "meter-data/unified-metrics" in url:
            return "unified-metrics"
        return "unknown"

    def _budget(self, endpoint_key: str) -> RequestBudget:
        return self._request_log.budget(endpoint_key)

    async def async_load_budget(self) -> None:
        self._request_log = await async_get_request_log(self.hass)

    async def _save_budget(self) -> None:
        await self._request_log.async_save()

    def budget_free_at(self, endpoint_key: str = "unified-metrics", keep_free: int = 0):
        return self._budget(endpoint_key).free_at(dt_util.utcnow(), keep_free)

    def budget_room_soon(self, endpoint_key: str = "unified-metrics", keep_free: int = 0) -> bool:
        """Room now, or within the time a request waits for a slot."""
        free_at = self.budget_free_at(endpoint_key, keep_free)
        return free_at is None or (free_at - dt_util.utcnow()).total_seconds() <= API_SLOT_WAIT_SECONDS

    async def _check_rate_limit(self, endpoint_key: str) -> Optional[float]:
        async with self._rate_limit_lock:
            if endpoint_key in self._rate_limits:
                limit_info = self._rate_limits[endpoint_key]
                retry_after = limit_info.get("retry_after")

                if retry_after and datetime.now() < retry_after:
                    wait_time = (retry_after - datetime.now()).total_seconds()
                    return wait_time
                elif retry_after and datetime.now() >= retry_after:
                    del self._rate_limits[endpoint_key]

        return None

    def _calculate_backoff(self, attempt: int, base_delay: float = 20.0) -> float:
        backoff = base_delay * (2 ** attempt)
        jitter = backoff * 0.2 * (2 * random.random() - 1)
        return max(1.0, backoff + jitter)

    async def _handle_rate_limit(self, response: aiohttp.ClientResponse, endpoint_key: str):

        wait_time = retry_after_seconds(response.headers, dt_util.utcnow())

        retry_after_dt = datetime.now() + timedelta(seconds=wait_time)
        self._budget(endpoint_key).block_until(dt_util.utcnow() + timedelta(seconds=wait_time))
        await self._save_budget()

        async with self._rate_limit_lock:
            self._rate_limits[endpoint_key] = {
                "retry_after": retry_after_dt,
                "backoff": wait_time
            }

        _LOGGER.warning(
            "Endpoint %s is rate limited. Will retry after %d seconds", endpoint_key, int(wait_time)
        )


    async def _make_request(
        self,
        url: str,
        max_retries: int = 3,
        base_delay: float = 20.0,
        keep_free: int = 0
    ) -> Dict[str, Any]:

        endpoint_key = self._get_endpoint_key(url)
        if self._request_log is None:
            await self.async_load_budget()

        wait_time = await self._check_rate_limit(endpoint_key)
        if wait_time and wait_time > 0:
            if wait_time <= 60:
                _LOGGER.info(
                    "Waiting %d seconds for rate limit to clear", int(wait_time)
                )
                await asyncio.sleep(wait_time)
            else:
                # A known 429 block: nothing is sent, and callers learn when it ends.
                raise BudgetExhausted(endpoint_key, dt_util.utcnow() + timedelta(seconds=wait_time))

        headers = {
            "Authorization": self.api_key,
            "Accept": "application/json"
        }

        last_exception = None

        for attempt in range(max_retries):
            self._check_open()
            budget = self._budget(endpoint_key)
            claimed = await async_claim_slot(budget, keep_free, self._check_open)
            if claimed is None:
                raise BudgetExhausted(endpoint_key, budget.free_at(dt_util.utcnow(), keep_free))
            # Saved before sending, so a crash cannot lose it. That save holds the
            # claim time; the send time below follows it to the file unawaited.
            await self._save_budget()

            try:
                async with self._request_semaphore:
                    async with asyncio.timeout(API_TIMEOUT):
                        if self.closed:
                            # Unloaded during the awaits above: not sent, slot given back.
                            budget.release(claimed)
                            self._request_log.save_soon()
                            self._check_open()
                        budget.restamp(claimed, dt_util.utcnow())
                        self._request_log.save_soon()
                        async with self.session.get(url, headers=headers) as response:
                            if response.status == 200:
                                data = await response.json()
                                return data

                            elif response.status == 429:
                                await self._handle_rate_limit(response, endpoint_key)

                                if attempt < max_retries - 1:
                                    backoff = self._calculate_backoff(attempt, base_delay)
                                    _LOGGER.debug(
                                        "Rate limited, retrying in %.1f seconds (attempt %d/%d)",
                                        backoff, attempt + 1, max_retries
                                    )
                                    await asyncio.sleep(backoff)
                                    continue
                                else:
                                    raise UpdateFailed(
                                        f"API rate limit exceeded after {max_retries} attempts"
                                    )

                            elif response.status == 500:
                                error_text = await response.text()
                                if error_text.strip().startswith('<!doctype html>') or error_text.strip().startswith('<html'):
                                    _LOGGER.error(
                                        "API error 500 for %s (HTML error page received)", endpoint_key
                                    )
                                else:
                                    _LOGGER.error(
                                        "API returned 500 for %s: %s",
                                        endpoint_key, error_text[:100]
                                    )

                                if attempt < max_retries - 1:
                                    backoff = self._calculate_backoff(attempt, base_delay)
                                    _LOGGER.debug(
                                        "Retrying after 500 error in %.1f seconds (attempt %d/%d)",
                                        backoff, attempt + 1, max_retries
                                    )
                                    await asyncio.sleep(backoff)
                                    continue
                                else:
                                    raise UpdateFailed(
                                        f"API server error (500) for {endpoint_key} after {max_retries} attempts"
                                    )

                            elif response.status in (401, 403):
                                raise UpdateFailed(
                                    f"Authentication failed (status {response.status}). Please check your API key."
                                )

                            elif response.status == 404:
                                raise UpdateFailed(
                                    f"API endpoint not found (404): {endpoint_key}"
                                )

                            else:
                                error_text = await response.text()
                                if error_text.strip().startswith('<!doctype html>') or error_text.strip().startswith('<html'):
                                    _LOGGER.error(
                                        "API error %d for %s (HTML error page received)", response.status, endpoint_key
                                    )
                                else:
                                    _LOGGER.error(
                                        "API error %d for %s: %s",
                                        response.status, endpoint_key, error_text[:100]
                                    )

                                if attempt < max_retries - 1:
                                    backoff = self._calculate_backoff(attempt, base_delay)
                                    await asyncio.sleep(backoff)
                                    continue
                                else:
                                    raise UpdateFailed(
                                        f"API error {response.status} for {endpoint_key}"
                                    )

            except ClientClosed:
                raise

            except asyncio.TimeoutError as err:
                last_exception = err
                _LOGGER.warning(
                    "Timeout fetching from %s (attempt %d/%d)",
                    endpoint_key, attempt + 1, max_retries
                )

                if attempt < max_retries - 1:
                    backoff = self._calculate_backoff(attempt, base_delay)
                    await asyncio.sleep(backoff)
                    continue

            except aiohttp.ClientError as err:
                last_exception = err
                _LOGGER.warning(
                    "Network error fetching from %s: %s (attempt %d/%d)",
                    endpoint_key, err, attempt + 1, max_retries
                )

                if attempt < max_retries - 1:
                    backoff = self._calculate_backoff(attempt, base_delay)
                    await asyncio.sleep(backoff)
                    continue

            except Exception as err:
                last_exception = err
                _LOGGER.exception(
                    "Unexpected error fetching from %s: %s",
                    endpoint_key, err
                )
                break

        if last_exception:
            raise UpdateFailed(
                f"Failed to fetch data from {endpoint_key} after {max_retries} attempts"
            ) from last_exception

        raise UpdateFailed(f"Failed to fetch data from {endpoint_key}")

    async def fetch(
        self,
        url: str,
        max_retries: int = 3,
        base_delay: float = 20.0,
        keep_free: int = 0
    ) -> Dict[str, Any]:
        """GET `url`. Each attempt needs room in the hourly budget, leaving `keep_free` slots."""
        async with self._in_flight_lock:
            task = self._in_flight.get(url)
            created = task is None
            if created:
                task = asyncio.create_task(
                    self._make_request(url, max_retries, base_delay, keep_free)
                )
                self._in_flight[url] = task
            else:
                _LOGGER.debug("Deduplicating request for %s", url)

        try:
            return await task
        finally:
            if created:
                async with self._in_flight_lock:
                    self._in_flight.pop(url, None)
