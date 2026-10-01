"""Async GeoIP wrapper around the synchronous geoip2 library.

geoip2 reads from local .mmdb files and is CPU-bound/IO-bound sync.
Calls are wrapped in asyncio.to_thread() to avoid blocking the event loop.

Preserves the exact fallback behaviour of utils/geoip.py:
- Returns "Unknown" when the database file is missing or lookup fails.
- Lazy-loads readers on first use (double-checked locking with asyncio.Lock).
"""

import asyncio

import geoip2.database
import geoip2.errors
import maxminddb

from app.infrastructure.logging import get_logger

log = get_logger(__name__)


class GeoIPService:
    def __init__(self, country_db_path: str, city_db_path: str) -> None:
        self._country_db_path = country_db_path
        self._city_db_path = city_db_path
        self._country_reader: geoip2.database.Reader | None = None
        self._city_reader: geoip2.database.Reader | None = None
        self._country_loaded = False
        self._city_loaded = False
        self._lock = asyncio.Lock()

    async def _get_country_reader(self) -> geoip2.database.Reader | None:
        if not self._country_loaded:
            async with self._lock:
                if not self._country_loaded:
                    try:
                        self._country_reader = await asyncio.to_thread(
                            geoip2.database.Reader, self._country_db_path
                        )
                    except (OSError, maxminddb.InvalidDatabaseError) as e:
                        log.warning(
                            "geoip_country_db_unavailable",
                            error=str(e),
                            error_type=type(e).__name__,
                        )
                        self._country_reader = None
                    self._country_loaded = True
        return self._country_reader

    async def _get_city_reader(self) -> geoip2.database.Reader | None:
        if not self._city_loaded:
            async with self._lock:
                if not self._city_loaded:
                    try:
                        self._city_reader = await asyncio.to_thread(
                            geoip2.database.Reader, self._city_db_path
                        )
                    except (OSError, maxminddb.InvalidDatabaseError) as e:
                        log.warning(
                            "geoip_city_db_unavailable",
                            error=str(e),
                            error_type=type(e).__name__,
                        )
                        self._city_reader = None
                    self._city_loaded = True
        return self._city_reader

    async def get_country(self, ip_address: str) -> str:
        reader = await self._get_country_reader()
        if reader is None:
            return "Unknown"
        try:
            result = await asyncio.to_thread(reader.country, ip_address)
            return result.country.name or "Unknown"
        except (
            geoip2.errors.AddressNotFoundError,
            ValueError,
            maxminddb.InvalidDatabaseError,
        ):
            return "Unknown"

    async def get_country_code(self, ip_address: str) -> str | None:
        """ISO 3166-1 alpha-2 code for an IP, or None when unresolvable.

        Nullable like get_city (no "Unknown" sentinel) — callers treat None
        as "no geo decision possible".
        """
        reader = await self._get_country_reader()
        if reader is None:
            return None
        try:
            result = await asyncio.to_thread(reader.country, ip_address)
            return result.country.iso_code
        except (
            geoip2.errors.AddressNotFoundError,
            ValueError,
            maxminddb.InvalidDatabaseError,
        ):
            return None

    async def get_city(self, ip_address: str) -> str | None:
        reader = await self._get_city_reader()
        if reader is None:
            return None
        try:
            result = await asyncio.to_thread(reader.city, ip_address)
            return result.city.name
        except (
            geoip2.errors.AddressNotFoundError,
            ValueError,
            maxminddb.InvalidDatabaseError,
        ):
            return None
