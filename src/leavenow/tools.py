"""The logic behind each MCP tool. Kept separate from server.py so it's easy to test."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from .google import GoogleAPIError, GoogleClient

MODES = ("TRANSIT", "WALK", "BICYCLE", "DRIVE")
MODE_ALIASES = {
    "transit": "TRANSIT", "bus": "TRANSIT", "train": "TRANSIT", "subway": "TRANSIT", "public transit": "TRANSIT",
    "walk": "WALK", "walking": "WALK", "foot": "WALK",
    "bike": "BICYCLE", "bicycle": "BICYCLE", "biking": "BICYCLE", "cycling": "BICYCLE",
    "drive": "DRIVE", "driving": "DRIVE", "car": "DRIVE",
}


def now_utc() -> datetime:
    """Current time. Tests replace this to freeze the clock."""
    return datetime.now(timezone.utc)


# ---- small helpers -----------------------------------------------------------


def normalize_mode(mode: str) -> str:
    m = MODE_ALIASES.get(mode.strip().lower(), mode.strip().upper())
    if m not in MODES:
        raise ToolError(f"Unknown mode '{mode}'. Use one of: transit, walk, bicycle, drive.")
    return m


def parse_time(value: str, tz: ZoneInfo) -> datetime:
    """Parse an ISO 8601 time. A time without an offset is read in `tz` (the place's local time)."""
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ToolError(
            f"Couldn't read the time '{value}'. Use ISO 8601, like 2026-11-04T09:00 "
            "(local time at the place) or 2026-11-04T09:00:00-08:00."
        )
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


def seconds(duration: str | None) -> int:
    """Google durations look like '754s'."""
    if not duration:
        return 0
    return int(float(duration.rstrip("s")))


def clock(dt: datetime, tz: ZoneInfo) -> str:
    local = dt.astimezone(tz)
    return local.strftime("%-I:%M %p").lstrip("0")


def stamp(dt: datetime, tz: ZoneInfo) -> dict:
    local = dt.astimezone(tz)
    return {"local_time": clock(dt, tz), "iso": local.replace(microsecond=0).isoformat()}


async def place_tz(client: GoogleClient, address: str) -> tuple[ZoneInfo, str]:
    place = await client.geocode(address)
    tz_id = await client.timezone_id(place.lat, place.lng)
    return ZoneInfo(tz_id), place.address


def _ride(td: dict, tz: ZoneInfo) -> dict:
    line = td.get("transitLine", {})
    stops = td.get("stopDetails", {})
    ride = {
        "do": "ride",
        "line": line.get("nameShort") or line.get("name"),
        "vehicle": (line.get("vehicle") or {}).get("type", "TRANSIT").lower(),
        "toward": td.get("headsign"),
        "board_at": (stops.get("departureStop") or {}).get("name"),
        "get_off_at": (stops.get("arrivalStop") or {}).get("name"),
        "stops": td.get("stopCount"),
    }
    if stops.get("departureTime"):
        ride["departs"] = clock(datetime.fromisoformat(stops["departureTime"]), tz)
    if stops.get("arrivalTime"):
        ride["arrives"] = clock(datetime.fromisoformat(stops["arrivalTime"]), tz)
    return {k: v for k, v in ride.items() if v is not None}


def directions(route: dict, tz: ZoneInfo) -> list[dict]:
    """Door-to-door steps in order: walk to the stop, ride, (transfer), walk to the door."""
    steps = [s for leg in route.get("legs", []) for s in leg.get("steps", [])]
    out: list[dict] = []
    walk_s = 0

    def flush(to: str | None):
        nonlocal walk_s
        if walk_s >= 30:
            out.append({"do": "walk", "minutes": max(1, round(walk_s / 60)), "to": to or "your destination"})
        walk_s = 0

    for s in steps:
        td = s.get("transitDetails")
        if td:
            flush(((td.get("stopDetails") or {}).get("departureStop") or {}).get("name"))
            out.append(_ride(td, tz))
        else:
            walk_s += seconds(s.get("staticDuration"))
    flush(None)
    return out


def summarize(route: dict, tz: ZoneInfo) -> dict:
    """Turn a raw Google route into something short Claude can reason about."""
    steps = [s for leg in route.get("legs", []) for s in leg.get("steps", [])]
    meters = route.get("distanceMeters", 0)
    out = {
        "door_to_door_minutes": round(seconds(route.get("duration")) / 60),
        "miles": round(meters / 1609.34, 1),
        "km": round(meters / 1000, 1),
    }
    rides = sum(1 for s in steps if s.get("transitDetails"))
    if rides:
        out["directions"] = directions(route, tz)
        out["transfers"] = rides - 1
        walk_min = sum(seconds(s.get("staticDuration")) for s in steps if s.get("travelMode") == "WALK") / 60
        out["walking_minutes"] = round(walk_min)
    fare = (route.get("travelAdvisory") or {}).get("transitFare")
    if fare:
        out["fare"] = f"{fare.get('units', '0')}.{int(fare.get('nanos', 0)) // 10_000_000:02d} {fare.get('currencyCode', '')}".strip()
    return out


def transit_window(route: dict) -> tuple[datetime, datetime] | None:
    """For a transit route: when you must walk out the door, and when you actually arrive."""
    steps = [s for leg in route.get("legs", []) for s in leg.get("steps", [])]
    idx = [i for i, s in enumerate(steps) if s.get("transitDetails")]
    if not idx:
        return None
    first, last = steps[idx[0]], steps[idx[-1]]
    board = datetime.fromisoformat(first["transitDetails"]["stopDetails"]["departureTime"])
    alight = datetime.fromisoformat(last["transitDetails"]["stopDetails"]["arrivalTime"])
    walk_before = sum(seconds(s.get("staticDuration")) for s in steps[: idx[0]])
    walk_after = sum(seconds(s.get("staticDuration")) for s in steps[idx[-1] + 1 :])
    return board - timedelta(seconds=walk_before), alight + timedelta(seconds=walk_after)


def span(minutes: int) -> str:
    """75 -> '1 h 15 min', 40 -> '40 min'."""
    h, m = divmod(abs(minutes), 60)
    if not h:
        return f"{m} min"
    return f"{h} h {m} min" if m else f"{h} h"


def _wrap(e: GoogleAPIError) -> ToolError:
    return ToolError(str(e))


def _no_route(mode: str, origin: str, destination: str) -> ToolError:
    hint = " Try mode='walk', 'bicycle' or 'drive'." if mode == "TRANSIT" else ""
    return ToolError(f"No {mode.lower()} route found from '{origin}' to '{destination}'.{hint}")


async def arrive_by_leg(
    client: GoogleClient, origin: str, destination: str, mode: str, target: datetime, now: datetime
) -> tuple[dict, datetime, datetime]:
    """Plan a trip that must arrive by `target`. Returns (route, when to walk out the door, actual arrival)."""
    if mode == "TRANSIT":
        route = await client.compute_route(origin, destination, mode, arrival_time=target)
        if route is None:
            raise _no_route(mode, origin, destination)
        window = transit_window(route)
        if window:
            return route, window[0], window[1]
        # Google says just walk.
        return route, target - timedelta(seconds=seconds(route.get("duration"))), target

    if mode == "DRIVE":
        # Traffic depends on when you leave, so guess, check, and adjust a few times.
        soonest = now + timedelta(minutes=1)
        depart = max(target - timedelta(minutes=30), soonest)
        route = None
        for _ in range(3):
            route = await client.compute_route(origin, destination, mode, departure_time=depart)
            if route is None:
                raise _no_route(mode, origin, destination)
            guess = max(target - timedelta(seconds=seconds(route["duration"])), soonest)
            if abs((guess - depart).total_seconds()) < 60:
                break
            depart = guess
        return route, depart, depart + timedelta(seconds=seconds(route["duration"]))

    route = await client.compute_route(origin, destination, mode)
    if route is None:
        raise _no_route(mode, origin, destination)
    return route, target - timedelta(seconds=seconds(route.get("duration"))), target


async def depart_at_leg(
    client: GoogleClient, origin: str, destination: str, mode: str, depart: datetime
) -> tuple[dict, datetime, datetime]:
    """Plan a trip leaving at `depart`. Returns (route, departure, arrival)."""
    route = await client.compute_route(origin, destination, mode, departure_time=depart)
    if route is None:
        raise _no_route(mode, origin, destination)
    window = transit_window(route) if mode == "TRANSIT" else None
    if window:
        return route, window[0], window[1]
    return route, depart, depart + timedelta(seconds=seconds(route.get("duration")))


def arrival_with_buffer(route: dict, mode: str, arrive: datetime, buffer: timedelta) -> datetime:
    """When you actually get there if you leave `buffer` earlier than needed.

    On a transit ride the extra time is spent waiting at the stop, so you arrive with
    the same bus. Driving, walking or biking, leaving earlier means arriving earlier.
    """
    if mode == "TRANSIT" and transit_window(route):
        return arrive
    return arrive - buffer


def slack(target: datetime, arrive: datetime) -> int:
    """Minutes to spare between actually arriving and when you need to be there."""
    return round((target - arrive).total_seconds() / 60)


# ---- tool 1: when_to_leave ---------------------------------------------------


async def when_to_leave(
    client: GoogleClient,
    origin: str,
    destination: str,
    arrive_by: str,
    mode: str = "transit",
    buffer_minutes: int = 5,
) -> dict:
    mode = normalize_mode(mode)
    if not 0 <= buffer_minutes <= 60:
        raise ToolError("buffer_minutes must be between 0 and 60.")
    try:
        tz, dest_name = await place_tz(client, destination)
        target = parse_time(arrive_by, tz)
        now = now_utc()
        if target <= now:
            raise ToolError(f"{clock(target, tz)} has already passed at the destination. Pick a later time.")

        route, depart, arrive_at = await arrive_by_leg(client, origin, destination, mode, target, now)
    except GoogleAPIError as e:
        raise _wrap(e)

    leave_by = depart - timedelta(minutes=buffer_minutes)
    arrive_at = arrival_with_buffer(route, mode, arrive_at, timedelta(minutes=buffer_minutes))
    minutes_left = round((leave_by - now).total_seconds() / 60)
    if minutes_left < 0:
        status = f"late: you needed to leave {span(minutes_left)} ago"
    elif minutes_left <= 2:
        status = "leave now"
    else:
        status = f"leave in {span(minutes_left)}"

    trip = summarize(route, tz)
    return {
        "status": status,
        "leave_by": stamp(leave_by, tz),
        "minutes_until_leave": minutes_left,
        "expected_arrival": stamp(arrive_at, tz),
        "arrive_by": stamp(target, tz),
        "slack_minutes": slack(target, arrive_at),
        "buffer_minutes": buffer_minutes,
        "mode": mode.lower(),
        "destination": destination,
        "destination_address": dest_name,
        "trip": trip,
    }


# ---- tool 2: compare_ways_to_get_there ---------------------------------------


async def compare_ways_to_get_there(
    client: GoogleClient,
    origin: str,
    destination: str,
    depart_at: str | None = None,
    modes: list[str] | None = None,
) -> dict:
    wanted = [normalize_mode(m) for m in (modes or ["transit", "walk", "bicycle", "drive"])]
    try:
        tz, origin_name = await place_tz(client, origin)
    except GoogleAPIError as e:
        raise _wrap(e)
    now = now_utc()
    depart = parse_time(depart_at, tz) if depart_at else now
    # Google rejects departure times in the past.
    depart = max(depart, now + timedelta(seconds=30))

    async def one(mode: str) -> dict:
        try:
            route = await client.compute_route(origin, destination, mode, departure_time=depart)
        except GoogleAPIError as e:
            return {"mode": mode.lower(), "error": str(e)}
        if route is None:
            return {"mode": mode.lower(), "error": "no route"}
        trip = summarize(route, tz)
        arrive = depart + timedelta(minutes=trip["door_to_door_minutes"])
        if mode == "TRANSIT":
            window = transit_window(route)
            if window:
                arrive = window[1]
        return {"mode": mode.lower(), "arrives": clock(arrive, tz), **trip}

    results = await asyncio.gather(*(one(m) for m in wanted))
    ok = sorted((r for r in results if "error" not in r), key=lambda r: r["door_to_door_minutes"])
    failed = [r for r in results if "error" in r]
    return {
        "from": origin_name,
        "leaving": stamp(depart, tz),
        "fastest": ok[0]["mode"] if ok else None,
        "options": ok + failed,
    }


# ---- tool 3: weather_along_the_way -------------------------------------------


def _hour_at(hours: list[dict], when: datetime) -> dict | None:
    for h in hours:
        iv = h.get("interval", {})
        try:
            start = datetime.fromisoformat(iv["startTime"])
            end = datetime.fromisoformat(iv["endTime"])
        except (KeyError, ValueError):
            continue
        if start <= when < end:
            return h
    return hours[0] if hours else None


def _weather_summary(h: dict | None) -> dict | None:
    if not h:
        return None
    temp = h.get("temperature") or {}
    feels = h.get("feelsLikeTemperature") or {}
    precip = ((h.get("precipitation") or {}).get("probability")) or {}
    wind = ((h.get("wind") or {}).get("speed")) or {}
    unit = "°F" if temp.get("unit") == "FAHRENHEIT" else "°C"
    chance = precip.get("percent", 0)
    cond = h.get("weatherCondition") or {}
    wet = any(w in (cond.get("type") or "").upper() for w in ("RAIN", "SHOWER", "DRIZZLE", "THUNDER", "SNOW"))
    return {
        "conditions": (cond.get("description") or {}).get("text"),
        "temperature": f"{temp.get('degrees')}{unit}" if "degrees" in temp else None,
        "feels_like": f"{feels.get('degrees')}{unit}" if "degrees" in feels else None,
        "chance_of_rain_percent": chance,
        "rain_likely": chance >= 40 or wet,
        "wind": f"{wind.get('value')} {'mph' if wind.get('unit') == 'MILES_PER_HOUR' else 'km/h'}" if wind else None,
    }


def _air_summary(aq: dict) -> dict:
    indexes = aq.get("indexes") or []
    local = next((i for i in indexes if i.get("code") != "uaqi"), None)
    best = local or (indexes[0] if indexes else {})
    out = {
        "aqi": best.get("aqi"),
        "scale": best.get("displayName"),
        "category": best.get("category"),
        "main_pollutant": best.get("dominantPollutant"),
    }
    advice = (aq.get("healthRecommendations") or {}).get("generalPopulation")
    if advice:
        out["advice"] = advice
    return out


async def weather_along_the_way(
    client: GoogleClient,
    origin: str,
    destination: str,
    time: str | None = None,
    units: str = "imperial",
) -> dict:
    units = units.strip().upper()
    if units not in ("IMPERIAL", "METRIC"):
        raise ToolError("units must be 'imperial' or 'metric'.")
    try:
        start, end = await asyncio.gather(client.geocode(origin), client.geocode(destination))
        tz = ZoneInfo(await client.timezone_id(start.lat, start.lng))
        now = now_utc()
        when = parse_time(time, tz) if time else now
        if when < now - timedelta(hours=1):
            raise ToolError("That time has passed. Weather forecasts only cover the next 10 days.")
        hours_ahead = int((when - now).total_seconds() // 3600) + 2
        if hours_ahead > 240:
            raise ToolError("Forecasts only go 10 days out. Pick a sooner time.")

        f_start, f_end, a_start, a_end = await asyncio.gather(
            client.hourly_forecast(start.lat, start.lng, hours_ahead, units),
            client.hourly_forecast(end.lat, end.lng, hours_ahead, units),
            client.air_quality(start.lat, start.lng),
            client.air_quality(end.lat, end.lng),
        )
    except GoogleAPIError as e:
        raise _wrap(e)

    w_start, w_end = _weather_summary(_hour_at(f_start, when)), _weather_summary(_hour_at(f_end, when))
    result = {
        "time": stamp(when, tz),
        "origin": {"place": origin, "address": start.address, "weather": w_start, "air": _air_summary(a_start)},
        "destination": {"place": destination, "address": end.address, "weather": w_end, "air": _air_summary(a_end)},
        "rain_likely_on_trip": any(w and w["rain_likely"] for w in (w_start, w_end)),
    }
    if when - now > timedelta(hours=3):
        result["note"] = "Air quality is current conditions, not a forecast for that time."
    return result


# ---- tool 4: plan_my_day -----------------------------------------------------

MAX_STOPS = 8


class Stop(BaseModel):
    place: str = Field(description="Where this stop is: address, or building/place name with city.")
    arrive_by: str | None = Field(
        None, description="When they must be there, ISO 8601 local time, e.g. '2026-11-04T14:00'. Omit to go right after the previous stop."
    )
    stay_minutes: int = Field(60, ge=0, le=720, description="How long they'll be there.")
    label: str | None = Field(None, description="Short name like 'CS 61B lecture' or 'Lab'.")


def _hours_on(place: dict, day: datetime) -> str | None:
    days = (place.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []
    # Google lists Monday first, same as Python's weekday().
    return days[day.weekday()] if len(days) == 7 else None


SUGGEST_RADIUS_M = 800  # about a 10 minute walk
WALK_M_PER_MIN = 80


def meters_between(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


async def _suggest(client: GoogleClient, near: str, query: str, when: datetime, now: datetime) -> list[dict]:
    spot = await client.geocode(near)
    found = await client.search_places(
        query, spot.lat, spot.lng, radius_m=SUGGEST_RADIUS_M, open_now=when - now < timedelta(minutes=30)
    )
    out = []
    for p in found:
        loc = p.get("location") or {}
        if "latitude" not in loc:
            continue
        dist = meters_between(spot.lat, spot.lng, loc["latitude"], loc["longitude"])
        if dist > SUGGEST_RADIUS_M:
            continue
        item = {
            "name": (p.get("displayName") or {}).get("text"),
            "address": p.get("formattedAddress"),
            "rating": p.get("rating"),
            "walk_minutes": max(1, round(dist / WALK_M_PER_MIN)),
            "maps_url": p.get("googleMapsUri"),
        }
        hours = _hours_on(p, when)
        if hours:
            item["hours_that_day"] = hours
        out.append({k: v for k, v in item.items() if v is not None})
        if len(out) == 3:
            break
    return out


async def plan_my_day(
    client: GoogleClient,
    start: str,
    stops: list[Stop],
    mode: str = "transit",
    start_time: str | None = None,
    buffer_minutes: int = 5,
    fill_gaps_with: str | None = None,
    min_gap_minutes: int = 45,
) -> dict:
    mode = normalize_mode(mode)
    if not stops:
        raise ToolError("Give at least one stop.")
    if len(stops) > MAX_STOPS:
        raise ToolError(f"Plan up to {MAX_STOPS} stops at a time.")
    if not 0 <= buffer_minutes <= 60:
        raise ToolError("buffer_minutes must be between 0 and 60.")

    try:
        tz, start_name = await place_tz(client, start)
    except GoogleAPIError as e:
        raise _wrap(e)
    now = now_utc()
    soonest = now + timedelta(seconds=30)
    free_at = max(parse_time(start_time, tz), now) if start_time else now
    here, here_label = start, "start"
    schedule: list[dict] = []
    warnings: list[str] = []
    travel_minutes = 0
    buffer = timedelta(minutes=buffer_minutes)

    for i, stop in enumerate(stops, 1):
        label = stop.label or stop.place
        target = parse_time(stop.arrive_by, tz) if stop.arrive_by else None
        if target and target <= now:
            raise ToolError(f"Stop {i} ({label}) is at {clock(target, tz)}, which has passed. Leave it out or change the time.")
        try:
            if target:
                route, depart, arrive = await arrive_by_leg(client, here, stop.place, mode, target, now)
                leave_by = depart - buffer
                if leave_by < free_at:
                    # Can't make it on time. Plan the soonest trip instead and say how late.
                    route, leave_by, arrive = await depart_at_leg(client, here, stop.place, mode, max(free_at, soonest))
                    late = round((arrive - target).total_seconds() / 60)
                    if late > 0:
                        busy = "it's already past" if here_label == "start" and not start_time else f"you're at {here_label} until {clock(free_at, tz)}"
                        warnings.append(
                            f"Tight: to reach {label} by {clock(target, tz)} you'd need to leave by "
                            f"{clock(depart - buffer, tz)}, but {busy}. Expect to arrive about {late} min late."
                        )
                else:
                    arrive = arrival_with_buffer(route, mode, arrive, buffer)
                    if here_label != "start" and leave_by - free_at >= timedelta(minutes=min_gap_minutes):
                        gap = {
                            "type": "free_time",
                            "at": here_label,
                            "from": clock(free_at, tz),
                            "until": clock(leave_by, tz),
                            "minutes": round((leave_by - free_at).total_seconds() / 60),
                        }
                        if fill_gaps_with:
                            gap["suggestions"] = await _suggest(client, here, fill_gaps_with, free_at, now)
                            gap["suggestions_note"] = f"Within about a 10 minute walk of {here_label}."
                        schedule.append(gap)
            else:
                route, leave_by, arrive = await depart_at_leg(client, here, stop.place, mode, max(free_at, soonest))
        except GoogleAPIError as e:
            raise _wrap(e)

        trip = summarize(route, tz)
        travel_minutes += trip["door_to_door_minutes"]
        leg = {
            "type": "travel",
            "from": here_label,
            "to": label,
            "leave_by": stamp(leave_by, tz),
            "arrive": stamp(arrive, tz),
            "trip": trip,
        }
        if target:
            leg["slack_minutes"] = slack(target, arrive)
        schedule.append(leg)
        begins = max(arrive, target) if target else arrive
        ends = begins + timedelta(minutes=stop.stay_minutes)
        schedule.append(
            {"type": "stop", "label": label, "place": stop.place, "from": clock(begins, tz), "until": clock(ends, tz)}
        )
        free_at, here, here_label = ends, stop.place, label

    first = next(s for s in schedule if s["type"] == "travel")
    return {
        "start": start_name,
        "date": first["leave_by"]["iso"][:10],
        "mode": mode.lower(),
        "first_leave_by": first["leave_by"],
        "total_travel_minutes": travel_minutes,
        "warnings": warnings,
        "schedule": schedule,
    }


# ---- tool 5: transit_options -------------------------------------------------

MAX_OPTIONS = 6
SEARCH_WINDOW = timedelta(hours=4)


def _first_ride(route: dict) -> tuple[datetime, str | None] | None:
    for leg in route.get("legs", []):
        for s in leg.get("steps", []):
            td = s.get("transitDetails")
            if td:
                line = td.get("transitLine", {})
                when = datetime.fromisoformat(td["stopDetails"]["departureTime"])
                return when, line.get("nameShort") or line.get("name")
    return None


def _option(route: dict, tz: ZoneInfo) -> dict:
    leave, arrive = transit_window(route)
    trip = summarize(route, tz)
    out = {
        "leave_door": clock(leave, tz),
        "arrive_door": clock(arrive, tz),
        "door_to_door_minutes": round((arrive - leave).total_seconds() / 60),
        "transfers": trip.get("transfers", 0),
        "walking_minutes": trip.get("walking_minutes"),
        "directions": trip.get("directions", []),
    }
    if "fare" in trip:
        out["fare"] = trip["fare"]
    return out


async def transit_options(
    client: GoogleClient,
    origin: str,
    destination: str,
    depart_after: str | None = None,
    arrive_by: str | None = None,
    count: int = 4,
) -> dict:
    if depart_after and arrive_by:
        raise ToolError("Give depart_after or arrive_by, not both.")
    if not 1 <= count <= MAX_OPTIONS:
        raise ToolError(f"count must be between 1 and {MAX_OPTIONS}.")
    now = now_utc()
    try:
        tz, _ = await place_tz(client, destination if arrive_by else origin)
        options: list[tuple[datetime, dict]] = []
        seen: set[tuple] = set()

        if arrive_by:
            target = parse_time(arrive_by, tz)
            if target <= now:
                raise ToolError(f"{clock(target, tz)} has already passed. Pick a later time.")
            t = target
            for _ in range(count * 2):
                route = await client.compute_route(origin, destination, "TRANSIT", arrival_time=t)
                window = transit_window(route) if route else None
                if not window or window[0] < now or window[1] < target - SEARCH_WINDOW:
                    break
                key = _first_ride(route)
                if key not in seen:
                    seen.add(key)
                    options.append((window[0], _option(route, tz)))
                if len(options) == count:
                    break
                t = window[1] - timedelta(minutes=1)
            options.sort(key=lambda o: o[0])
        else:
            start = max(parse_time(depart_after, tz) if depart_after else now, now + timedelta(seconds=30))
            t = start
            for _ in range(count * 2):
                route = await client.compute_route(origin, destination, "TRANSIT", departure_time=t)
                ride = _first_ride(route) if route else None
                if not ride or ride[0] > start + SEARCH_WINDOW:
                    break
                if ride not in seen:
                    seen.add(ride)
                    options.append((transit_window(route)[0], _option(route, tz)))
                if len(options) == count:
                    break
                t = ride[0] + timedelta(minutes=1)
    except GoogleAPIError as e:
        raise _wrap(e)

    result: dict = {"from": origin, "to": destination, "options": [o for _, o in options]}
    if arrive_by:
        result["arrive_by"] = stamp(target, tz)
        if options:
            result["latest_that_makes_it"] = options[-1][1]["leave_door"]
    else:
        result["searching_from"] = stamp(start, tz)
    if not options:
        result["note"] = (
            "No transit trips found in that window. It may be walkable, or service may have ended. "
            "Try compare_ways_to_get_there."
        )
    elif len(options) < count:
        result["note"] = f"Only {len(options)} trip(s) found within {int(SEARCH_WINDOW.total_seconds() // 3600)} hours."
    return result
