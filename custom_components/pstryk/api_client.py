"""Shared API client for Pstryk Energy integration with caching and rate limiting."""
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
from homeassistant.helpers.translation import async_get_translations

from .const import API_URL, API_TIMEOUT, DOMAIN

_LOGGER = logging.getLogger(__name__)


class PstrykAPIClient:
    """Shared API client with caching, rate limiting, and proper error handling."""

    def __init__(self, hass: HomeAssistant, api_key: str):
        """Initialize the API client."""
        self.hass = hass
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._translations: Dict[str, str] = {}
        self._translations_loaded = False

        # Rate limiting: {endpoint_key: {"retry_after": datetime, "backoff": float}}
        self._rate_limits: Dict[str, Dict[str, Any]] = {}
        self._rate_limit_lock = asyncio.Lock()

        # Request throttling - limit concurrent requests
        self._request_semaphore = asyncio.Semaphore(3)  # Max 3 concurrent requests

        # Deduplication - track in-flight requests
        self._in_flight: Dict[str, asyncio.Task] = {}
        self._in_flight_lock = asyncio.Lock()

    @property
    def session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None:
            self._session = async_get_clientsession(self.hass)
        return self._session

    async def _load_translations(self):
        """Load translations for error messages."""
        if not self._translations_loaded:
            try:
                self._translations = await async_get_translations(
                    self.hass, self.hass.config.language, DOMAIN, ["debug"]
                )
                self._translations_loaded = True
                # Debug: log sample keys to understand the format
                if self._translations:
                    sample_keys = list(self._translations.keys())[:3]
                    _LOGGER.debug("Loaded %d translation keys, samples: %s",
                                len(self._translations), sample_keys)
            except Exception as ex:
                _LOGGER.warning("Failed to load translations for API client: %s", ex)
                self._translations = {}
                self._translations_loaded = True

    def _t(self, key: str, **kwargs) -> str:
        """Get translated string with fallback."""
        # Try different key formats as async_get_translations may return different formats
        possible_keys = [
            f"component.{DOMAIN}.debug.{key}",  # Full format: component.pstryk.debug.key
            f"{DOMAIN}.debug.{key}",            # Domain format: pstryk.debug.key
            f"debug.{key}",                      # Short format: debug.key
            key                                  # Just the key
        ]

        template = None
        for possible_key in possible_keys:
            template = self._translations.get(possible_key)
            if template:
                break

        # If translation not found, create a fallback message
        if not template:
            # Fallback patterns for common error types
            if key == "api_error_html":
                template = "API error {status} for {endpoint} (HTML error page received)"
            elif key == "rate_limited":
                template = "Endpoint '{endpoint}' is rate limited. Will retry after {seconds} seconds"
            elif key == "waiting_rate_limit":
                template = "Waiting {seconds} seconds for rate limit to clear"
            else:
                _LOGGER.debug("Translation key not found: %s (tried formats: %s)", key, possible_keys)
                template = key

        try:
            return template.format(**kwargs)
        except (KeyError, ValueError) as e:
            _LOGGER.warning("Failed to format translation template '%s': %s", template, e)
            return template

    def _get_endpoint_key(self, url: str) -> str:
        """Extract endpoint key from URL for rate limiting."""
        # The integration now uses unified-metrics for both pricing and meter data.
        if "meter-data/unified-metrics" in url:
            return "unified-metrics"
        return "unknown"

    async def _check_rate_limit(self, endpoint_key: str) -> Optional[float]:
        """Check if we're rate limited and return wait time if needed."""
        async with self._rate_limit_lock:
            if endpoint_key in self._rate_limits:
                limit_info = self._rate_limits[endpoint_key]
                retry_after = limit_info.get("retry_after")

                if retry_after and datetime.now() < retry_after:
                    wait_time = (retry_after - datetime.now()).total_seconds()
                    return wait_time
                elif retry_after and datetime.now() >= retry_after:
                    # Rate limit expired, clear it
                    del self._rate_limits[endpoint_key]

        return None

    def _calculate_backoff(self, attempt: int, base_delay: float = 20.0) -> float:
        """Calculate exponential backoff with jitter."""
        # Exponential backoff: base_delay * (2 ^ attempt)
        backoff = base_delay * (2 ** attempt)
        # Add jitter: ±20% randomization
        jitter = backoff * 0.2 * (2 * random.random() - 1)
        return max(1.0, backoff + jitter)

    async def _handle_rate_limit(self, response: aiohttp.ClientResponse, endpoint_key: str):
        """Handle 429 rate limit response."""
        # Ensure translations are loaded
        await self._load_translations()

        retry_after_header = response.headers.get("Retry-After")
        wait_time = None

        if retry_after_header:
            try:
                # Try parsing as seconds
                wait_time = int(retry_after_header)
            except ValueError:
                # Try parsing as HTTP date
                try:
                    retry_date = parsedate_to_datetime(retry_after_header)
                    wait_time = (retry_date - datetime.now()).total_seconds()
                except Exception:
                    pass

        # Fallback to 3600 seconds (1 hour) if not specified
        if wait_time is None:
            wait_time = 3600

        retry_after_dt = datetime.now() + timedelta(seconds=wait_time)

        async with self._rate_limit_lock:
            self._rate_limits[endpoint_key] = {
                "retry_after": retry_after_dt,
                "backoff": wait_time
            }

        _LOGGER.warning(
            self._t("rate_limited", endpoint=endpoint_key, seconds=int(wait_time))
        )


    async def _make_request(
        self,
        url: str,
        max_retries: int = 3,
        base_delay: float = 20.0
    ) -> Dict[str, Any]:
        """Make API request with retries, rate limiting, and deduplication."""
        # Load translations if not already loaded
        await self._load_translations()

        endpoint_key = self._get_endpoint_key(url)

        # Check if we're rate limited
        wait_time = await self._check_rate_limit(endpoint_key)
        if wait_time and wait_time > 0:
            # If wait time is reasonable, wait
            if wait_time <= 60:
                _LOGGER.info(
                    self._t("waiting_rate_limit", seconds=int(wait_time))
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
                # Use semaphore to limit concurrent requests
                async with self._request_semaphore:
                    async with asyncio.timeout(API_TIMEOUT):
                        async with self.session.get(url, headers=headers) as response:
                            # Handle different status codes
                            if response.status == 200:
                                data = await response.json()
                                return data

                            elif response.status == 429:
                                # Handle rate limiting
                                await self._handle_rate_limit(response, endpoint_key)

                                # Retry with exponential backoff
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
                                # Extract plain text from HTML if present
                                if error_text.strip().startswith('<!doctype html>') or error_text.strip().startswith('<html'):
                                    # Just log that it's HTML, not the whole HTML
                                    _LOGGER.error(
                                        self._t("api_error_html", status=500, endpoint=endpoint_key)
                                    )
                                else:
                                    # Log actual error text (truncated)
                                    _LOGGER.error(
                                        "API returned 500 for %s: %s",
                                        endpoint_key, error_text[:100]
                                    )

                                # Retry with backoff
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
                                # Clean HTML from error messages
                                if error_text.strip().startswith('<!doctype html>') or error_text.strip().startswith('<html'):
                                    _LOGGER.error(
                                        self._t("api_error_html", status=response.status, endpoint=endpoint_key)
                                    )
                                else:
                                    _LOGGER.error(
                                        "API error %d for %s: %s",
                                        response.status, endpoint_key, error_text[:100]
                                    )

                                # For other errors, retry with backoff
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

        # All retries exhausted, raise the error
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
        """Fetch data with deduplication of concurrent requests."""
        # Check if there's already an in-flight request for this URL
        async with self._in_flight_lock:
            if url in self._in_flight:
                _LOGGER.debug("Deduplicating request for %s", url)
                # Wait for the existing request to complete
                try:
                    return await self._in_flight[url]
                except Exception:
                    # If the in-flight request failed, create a new one
                    pass

            # Create new request task
            task = asyncio.create_task(
                self._make_request(url, max_retries, base_delay)
            )
            self._in_flight[url] = task

        try:
            result = await task
            return result
        finally:
            # Remove from in-flight requests
            async with self._in_flight_lock:
                self._in_flight.pop(url, None)
