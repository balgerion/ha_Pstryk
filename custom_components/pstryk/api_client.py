import logging
import asyncio
import random
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from email.utils import parsedate_to_datetime

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import API_URL, API_TIMEOUT, DOMAIN

_LOGGER = logging.getLogger(__name__)


class PstrykAPIClient:

    def __init__(self, hass: HomeAssistant, api_key: str):
        self.hass = hass
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

        self._rate_limits: Dict[str, Dict[str, Any]] = {}
        self._rate_limit_lock = asyncio.Lock()

        self._request_semaphore = asyncio.Semaphore(3)

        self._in_flight: Dict[str, asyncio.Task] = {}
        self._in_flight_lock = asyncio.Lock()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = async_get_clientsession(self.hass)
        return self._session

    def _get_endpoint_key(self, url: str) -> str:
        if "meter-data/unified-metrics" in url:
            return "unified-metrics"
        return "unknown"

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

        retry_after_header = response.headers.get("Retry-After")
        wait_time = None

        if retry_after_header:
            try:
                wait_time = int(retry_after_header)
            except ValueError:
                try:
                    retry_date = parsedate_to_datetime(retry_after_header)
                    wait_time = (retry_date - datetime.now()).total_seconds()
                except Exception:
                    pass

        if wait_time is None:
            wait_time = 3600

        retry_after_dt = datetime.now() + timedelta(seconds=wait_time)

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
        base_delay: float = 20.0
    ) -> Dict[str, Any]:

        endpoint_key = self._get_endpoint_key(url)

        wait_time = await self._check_rate_limit(endpoint_key)
        if wait_time and wait_time > 0:
            if wait_time <= 60:
                _LOGGER.info(
                    "Waiting %d seconds for rate limit to clear", int(wait_time)
                )
                await asyncio.sleep(wait_time)
            else:
                raise UpdateFailed(
                    f"API rate limited for {endpoint_key}. Please try again in {int(wait_time/60)} minutes."
                )

        headers = {
            "Authorization": self.api_key,
            "Accept": "application/json"
        }

        last_exception = None

        for attempt in range(max_retries):
            try:
                async with self._request_semaphore:
                    async with asyncio.timeout(API_TIMEOUT):
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
        base_delay: float = 20.0
    ) -> Dict[str, Any]:
        async with self._in_flight_lock:
            if url in self._in_flight:
                _LOGGER.debug("Deduplicating request for %s", url)
                try:
                    return await self._in_flight[url]
                except Exception:
                    pass

            task = asyncio.create_task(
                self._make_request(url, max_retries, base_delay)
            )
            self._in_flight[url] = task

        try:
            result = await task
            return result
        finally:
            async with self._in_flight_lock:
                self._in_flight.pop(url, None)
