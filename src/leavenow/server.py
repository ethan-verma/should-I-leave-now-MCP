"""Should I Leave Now? — an MCP server that tells you when to leave, using live Google data.

Run locally:   GOOGLE_MAPS_API_KEY=... uv run leavenow
Then connect:  http://localhost:8080/mcp
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
from typing import Annotated

import uvicorn
from baton import VendorConfig, install_baton
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import tools
from .google import GoogleClient

log = logging.getLogger("leavenow")

INSTRUCTIONS = """\
Use these tools whenever someone asks when to leave, how long a trip takes, which way to \
get somewhere is fastest, or whether the weather or air is OK for the trip. They use live \
Google traffic, transit, weather and air quality data, so prefer them over guessing.
Times without a UTC offset are read as local time at the place. If you don't know where the \
user is starting from, ask them (an address, a building name plus city, or a campus landmark all work).

Every trip is door to door. Pass the real starting address and the real final destination in ONE \
call: walking to the station, the ride, transfers and walking from the station are all included, \
step by step, in `directions`. Do not split a trip into separate walk and train calls.
When the user wants a choice of trains or buses ("which Caltrain should I catch", "trains home \
after 9:30"), use transit_options: it lists several departures, not just one.
If the user has told you their home or work address earlier, reuse it rather than asking again.
"""

server = MCPServer(
    name="should-i-leave-now",
    title="Should I Leave Now?",
    instructions=INSTRUCTIONS,
    version="0.2.0",
)

_client: GoogleClient | None = None


def client() -> GoogleClient:
    global _client
    if _client is None:
        key = os.environ.get("GOOGLE_MAPS_API_KEY")
        if not key:
            raise RuntimeError("Set GOOGLE_MAPS_API_KEY before starting the server.")
        _client = GoogleClient(key)
    return _client


def read_only(title: str) -> ToolAnnotations:
    """Annotations for a read-only tool. The directory checks annotations.title as well as the tool's title."""
    return ToolAnnotations(
        title=title, readOnlyHint=True, destructiveHint=False, openWorldHint=True, idempotentHint=True
    )

PLACE_HINT = (
    "An address, a building or place name with its city, or 'lat,lng'. "
    "Use the real door-to-door endpoint, not a nearby station."
)


@server.tool(title="When should I leave?", annotations=read_only("When should I leave?"))
async def when_to_leave(
    origin: Annotated[str, Field(description="Where the trip starts. " + PLACE_HINT)],
    destination: Annotated[str, Field(description="Where the user needs to be. " + PLACE_HINT)],
    arrive_by: Annotated[
        str,
        Field(description="When they must arrive, ISO 8601, e.g. '2026-11-04T09:00' (local time at the destination)."),
    ],
    mode: Annotated[str, Field(description="transit, walk, bicycle or drive.")] = "transit",
    buffer_minutes: Annotated[
        int,
        Field(description="Extra minutes of slack, 0-60. Moves leave_by earlier by this much.", ge=0, le=60),
    ] = 5,
) -> dict:
    """Latest time to leave to arrive on time, using live transit schedules or traffic.

    Returns the leave-by time, how many minutes from now that is, and for transit,
    which bus or train to catch and where.

    expected_arrival is when the user actually gets there if they leave at leave_by.
    slack_minutes is the time to spare between expected_arrival and arrive_by, so use
    it to answer "will I be late?" or "do I have time to park?". When driving, walking
    or biking, slack_minutes is about buffer_minutes. On transit it can be more, because
    leaving early just means waiting at the stop for the same ride.
    """
    log.info("tool=when_to_leave mode=%s", mode)
    return await tools.when_to_leave(client(), origin, destination, arrive_by, mode, buffer_minutes)


@server.tool(title="Compare ways to get there", annotations=read_only("Compare ways to get there"))
async def compare_ways_to_get_there(
    origin: Annotated[str, Field(description="Where the trip starts. " + PLACE_HINT)],
    destination: Annotated[str, Field(description="Where they're going. " + PLACE_HINT)],
    depart_at: Annotated[
        str | None,
        Field(description="When they'd leave, ISO 8601 (local time at the origin). Omit for now."),
    ] = None,
    modes: Annotated[
        list[str] | None,
        Field(description="Modes to compare. Default: transit, walk, bicycle, drive."),
    ] = None,
) -> dict:
    """Compare walking, biking, transit and driving for one trip: minutes, distance, arrival time, transit details.

    Options come back fastest first.
    """
    log.info("tool=compare_ways_to_get_there")
    return await tools.compare_ways_to_get_there(client(), origin, destination, depart_at, modes)


@server.tool(title="Weather along the way", annotations=read_only("Weather along the way"))
async def weather_along_the_way(
    origin: Annotated[str, Field(description="Where the trip starts. " + PLACE_HINT)],
    destination: Annotated[str, Field(description="Where they're going. " + PLACE_HINT)],
    time: Annotated[
        str | None,
        Field(description="When they'll travel, ISO 8601 (local time at the origin). Omit for now. Up to 10 days out."),
    ] = None,
    units: Annotated[str, Field(description="imperial (°F, mph) or metric (°C, km/h).")] = "imperial",
) -> dict:
    """Forecast at both ends of a trip for the travel time: conditions, temperature, chance of rain, wind, and air quality.

    Use it to answer 'should I bike or bus', 'do I need an umbrella', or 'is the air OK to walk'.
    """
    log.info("tool=weather_along_the_way")
    return await tools.weather_along_the_way(client(), origin, destination, time, units)


@server.tool(title="Plan my day", annotations=read_only("Plan my day"))
async def plan_my_day(
    start: Annotated[str, Field(description="Where the day starts, e.g. their apartment. " + PLACE_HINT)],
    stops: Annotated[
        list[tools.Stop],
        Field(description="Stops in order. Give arrive_by for fixed times (class, shift); omit it to go straight from the previous stop."),
    ],
    mode: Annotated[str, Field(description="transit, walk, bicycle or drive, used for every leg.")] = "transit",
    start_time: Annotated[
        str | None,
        Field(description="When they're free to leave the start, ISO 8601 local time. Omit for now."),
    ] = None,
    buffer_minutes: Annotated[int, Field(description="Extra minutes of slack per leg, 0-60.", ge=0, le=60)] = 5,
    fill_gaps_with: Annotated[
        str | None,
        Field(description="What to look for in free time between stops, e.g. 'quiet cafe', 'library', 'cheap lunch'."),
    ] = None,
    min_gap_minutes: Annotated[int, Field(description="Only report free time at least this long.", ge=10, le=240)] = 45,
) -> dict:
    """Turn a day of stops into a schedule: when to leave for each one, which bus or train to take, and free time in between.

    Flags stops they can't reach on time. With fill_gaps_with, suggests nearby places for each free block.
    Each travel leg's arrive is when they actually get there if they leave at leave_by. Legs to a stop
    with arrive_by also carry slack_minutes, the time to spare before that stop starts.
    """
    log.info("tool=plan_my_day stops=%d mode=%s", len(stops), mode)
    return await tools.plan_my_day(
        client(), start, stops, mode, start_time, buffer_minutes, fill_gaps_with, min_gap_minutes
    )


install_baton(server, VendorConfig(dsn='https://baton_pk_xwxh7s4VN4p4h4SXQYuklPUxKFg-ytLwq3LvICMSdk8@baton.goodtiming.ai/ten_fccdd4c5/srv-1a894d69', report_mode='on'))

# Claude's connector directory requires a title and behavior hints on every tool,
# including the report tool Baton adds (baton-sdk 0.9.7 sets neither).
# Remove once baton-sdk sets them itself; until then this only fills what's missing.
for _tool in server._tool_manager._tools.values():
    if _tool.name.endswith("_annotate"):
        _tool.title = _tool.title or "Report a problem"
        _tool.annotations = _tool.annotations or ToolAnnotations(
            title=_tool.title, readOnlyHint=False, destructiveHint=False, openWorldHint=True
        )


@server.tool(title="Train and bus options", annotations=read_only("Train and bus options"))
async def transit_options(
    origin: Annotated[str, Field(description="Where the trip starts. " + PLACE_HINT)],
    destination: Annotated[str, Field(description="Where they're going. " + PLACE_HINT)],
    depart_after: Annotated[
        str | None,
        Field(description="List trips leaving after this time, ISO 8601 local time at the origin. Omit for now."),
    ] = None,
    arrive_by: Annotated[
        str | None,
        Field(description="Instead, list the last trips that arrive by this time, ISO 8601 local time at the destination."),
    ] = None,
    count: Annotated[int, Field(description="How many trips to list, 1-6.", ge=1, le=6)] = 4,
) -> dict:
    """Several transit departures for one door-to-door trip, so the user can pick a train or bus.

    Use for "which Caltrain should I catch", "what are my trains home after 9:30", "is there a later bus".
    Each option has when to leave the door, when they arrive at the door, transfers, walking time,
    and step-by-step directions. With arrive_by, latest_that_makes_it is the last one that gets them there on time.
    """
    log.info("tool=transit_options count=%d", count)
    return await tools.transit_options(client(), origin, destination, depart_after, arrive_by, count)


@server.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


class RateLimit:
    """Allow each client IP a fixed number of requests per minute. Protects the Google bill."""

    def __init__(self, app, per_minute: int):
        self.app = app
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/health":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        forwarded = headers.get(b"x-forwarded-for", b"").decode().split(",")[0].strip()
        ip = forwarded or (scope.get("client") or ("unknown",))[0]
        now = time.monotonic()
        q = self.hits[ip]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= self.per_minute:
            resp = JSONResponse({"error": "Too many requests. Wait a minute and try again."}, status_code=429)
            return await resp(scope, receive, send)
        q.append(now)
        if len(self.hits) > 10_000:  # forget idle clients
            for k in [k for k, v in self.hits.items() if not v or v[-1] < now - 60]:
                del self.hits[k]
        await self.app(scope, receive, send)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client()  # fail fast if the key is missing
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    app = server.streamable_http_app(stateless_http=True, json_response=True, host=host)
    app = RateLimit(app, per_minute=int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60")))
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
