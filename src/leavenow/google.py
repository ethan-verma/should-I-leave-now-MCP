"""Thin async client for the Google Maps Platform APIs this server uses.

Every call goes through `_cached`, so repeated questions (same route, same
place) don't hit Google twice within a few minutes. That keeps the bill down.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
TIMEZONE_URL = "https://maps.googleapis.com/maps/api/timezone/json"
WEATHER_HOURS_URL = "https://weather.googleapis.com/v1/forecast/hours:lookup"
AIR_QUALITY_URL = "https://airquality.googleapis.com/v1/currentConditions:lookup"
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

PLACE_FIELDS = ",".join(
    [
        "places.displayName",
        "places.formattedAddress",
        "places.rating",
        "places.userRatingCount",
        "places.regularOpeningHours.weekdayDescriptions",
        "places.googleMapsUri",
        "places.location",
    ]
)

ROUTE_FIELDS = ",".join(
    [
        "routes.duration",
        "routes.distanceMeters",
        "routes.legs.startLocation",
        "routes.legs.endLocation",
        "routes.legs.steps.travelMode",
        "routes.legs.steps.staticDuration",
        "routes.legs.steps.transitDetails",
        "routes.travelAdvisory.transitFare",
    ]
)

# Seconds to keep each kind of answer.
TTL = {
    "geocode": 24 * 3600,
    "timezone": 24 * 3600,
    "route": 120,
    "weather": 600,
    "air": 600,
    "places": 600,
}
MAX_CACHE_ENTRIES = 5000


class GoogleAPIError(Exception):
    """Google returned an error. The message is safe to show to the model."""


@dataclass
class Place:
    address: str
    lat: float
    lng: float


def to_rfc3339(dt: datetime) -> str:
    """UTC timestamp in the form Google wants, e.g. 2026-11-04T17:00:00Z."""
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class GoogleClient:
    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key
        self.http = httpx.AsyncClient(timeout=15.0, transport=transport)
        self._cache: dict[str, tuple[float, Any]] = {}

    async def aclose(self) -> None:
        await self.http.aclose()

    # ---- caching -------------------------------------------------------

    async def _cached(self, kind: str, key_parts: Any, fetch):
        key = kind + ":" + json.dumps(key_parts, sort_keys=True, default=str)
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit and hit[0] > now:
            return hit[1]
        value = await fetch()
        if len(self._cache) >= MAX_CACHE_ENTRIES:
            # Drop expired entries first, then the oldest half if still full.
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            if len(self._cache) >= MAX_CACHE_ENTRIES:
                for k in list(self._cache)[: MAX_CACHE_ENTRIES // 2]:
                    del self._cache[k]
        self._cache[key] = (now + TTL[kind], value)
        return value

    async def _send(self, method: str, url: str, **kwargs) -> dict:
        try:
            resp = await self.http.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise GoogleAPIError(f"Couldn't reach Google ({type(e).__name__}). Try again.") from e
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            msg = (data.get("error") or {}).get("message") or resp.text[:200]
            raise GoogleAPIError(f"Google API error {resp.status_code}: {msg}")
        return data

    # ---- APIs ----------------------------------------------------------

    async def geocode(self, address: str) -> Place:
        async def fetch():
            data = await self._send("GET", GEOCODE_URL, params={"address": address, "key": self.api_key})
            status = data.get("status")
            if status == "ZERO_RESULTS" or not data.get("results"):
                raise GoogleAPIError(
                    f"Couldn't find '{address}'. Try a fuller address or add the city."
                )
            if status != "OK":
                raise GoogleAPIError(f"Geocoding failed: {status} {data.get('error_message', '')}".strip())
            top = data["results"][0]
            loc = top["geometry"]["location"]
            return Place(top.get("formatted_address", address), loc["lat"], loc["lng"])

        return await self._cached("geocode", address.strip().lower(), fetch)

    async def timezone_id(self, lat: float, lng: float) -> str:
        async def fetch():
            data = await self._send(
                "GET",
                TIMEZONE_URL,
                params={"location": f"{lat},{lng}", "timestamp": int(time.time()), "key": self.api_key},
            )
            if data.get("status") != "OK":
                raise GoogleAPIError(f"Time zone lookup failed: {data.get('status')}")
            return data["timeZoneId"]

        return await self._cached("timezone", [round(lat, 2), round(lng, 2)], fetch)

    async def compute_route(
        self,
        origin: str,
        destination: str,
        mode: str,
        departure_time: datetime | None = None,
        arrival_time: datetime | None = None,
    ) -> dict | None:
        """Best route for one travel mode, or None if Google has no route."""
        body: dict[str, Any] = {
            "origin": {"address": origin},
            "destination": {"address": destination},
            "travelMode": mode,
            "units": "IMPERIAL",
        }
        if mode == "DRIVE":
            body["routingPreference"] = "TRAFFIC_AWARE"
        if departure_time is not None:
            body["departureTime"] = to_rfc3339(departure_time)
        if arrival_time is not None:
            body["arrivalTime"] = to_rfc3339(arrival_time)

        async def fetch():
            data = await self._send(
                "POST",
                ROUTES_URL,
                json=body,
                headers={"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": ROUTE_FIELDS},
            )
            routes = data.get("routes") or []
            return routes[0] if routes else None

        return await self._cached("route", body, fetch)

    async def hourly_forecast(self, lat: float, lng: float, hours: int, units: str) -> list[dict]:
        hours = max(1, min(hours, 240))

        async def fetch():
            data = await self._send(
                "GET",
                WEATHER_HOURS_URL,
                params={
                    "key": self.api_key,
                    "location.latitude": lat,
                    "location.longitude": lng,
                    "hours": hours,
                    "pageSize": hours,
                    "unitsSystem": units,
                },
            )
            return data.get("forecastHours") or []

        return await self._cached("weather", [round(lat, 3), round(lng, 3), hours, units], fetch)

    async def air_quality(self, lat: float, lng: float) -> dict:
        async def fetch():
            return await self._send(
                "POST",
                AIR_QUALITY_URL,
                params={"key": self.api_key},
                json={
                    "location": {"latitude": lat, "longitude": lng},
                    "extraComputations": ["LOCAL_AQI", "HEALTH_RECOMMENDATIONS"],
                },
            )

        return await self._cached("air", [round(lat, 3), round(lng, 3)], fetch)

    async def search_places(
        self, query: str, lat: float, lng: float, radius_m: float = 800, open_now: bool = False, limit: int = 8
    ) -> list[dict]:
        """Places matching `query` (e.g. 'quiet cafe') inside a box around a point, best matches first.

        Text search only accepts a rectangle as a hard limit (a circle is just a preference),
        so callers should still check each result's distance.
        """
        dlat = radius_m / 111_320
        dlng = radius_m / (111_320 * max(math.cos(math.radians(lat)), 0.01))
        body: dict[str, Any] = {
            "textQuery": query,
            "maxResultCount": limit,
            "locationRestriction": {
                "rectangle": {
                    "low": {"latitude": lat - dlat, "longitude": lng - dlng},
                    "high": {"latitude": lat + dlat, "longitude": lng + dlng},
                }
            },
        }
        if open_now:
            body["openNow"] = True

        async def fetch():
            data = await self._send(
                "POST",
                PLACES_SEARCH_URL,
                json=body,
                headers={"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": PLACE_FIELDS},
            )
            return data.get("places") or []

        return await self._cached("places", body, fetch)
