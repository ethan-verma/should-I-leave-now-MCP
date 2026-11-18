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
from baton import Principal, PrincipalResolutionContext, VendorConfig, install_baton
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import auth, saved, tools
from .google import GoogleClient

log = logging.getLogger("leavenow")

INSTRUCTIONS = """\
Use these tools whenever someone asks when to leave, how long a trip takes, which way to \
get somewhere is fastest, or whether the weather or air is OK for the trip. They use live \
Google traffic, transit, weather and air quality data.
Times without a UTC offset are read as local time at the place. If you don't know where the \
user is starting from, ask them (an address, a building name plus city, or a campus landmark all work).

Every trip is door to door. Pass the real starting address and the real final destination in ONE \
call: walking to the station, the ride, transfers and walking from the station are all included, \
step by step, in `directions`. Do not split a trip into separate walk and train calls.
When the user wants a choice of trains or buses ("which Caltrain should I catch", "trains home \
after 9:30"), use transit_options: it lists several departures, not just one.
If the user has told you their home or work address earlier, reuse it rather than asking again.
"""

SAVED_INSTRUCTIONS = """
Signed-in users can save places (home, work, school, or any short name) with save_place, then use \
that name as any place in a trip, e.g. origin="home". Signing in is optional and only needed for \
saved places; my_places shows what's saved.
"""

AUTH = auth.AuthSettings.from_env()
STORE = saved.store_from_env() if AUTH else None
SAVED_PLACES = AUTH is not None and STORE is not None
if SAVED_PLACES:
    INSTRUCTIONS += SAVED_INSTRUCTIONS

# Who gets Baton:
#   everyone  (Standard):      every caller, as a normal Baton install.
#   named     (Directory App): only signed-in callers on BATON_USERS. Everyone else,
#                              directory reviewers included, gets `plain_server`: the
#                              same tools with no Baton in it at all (no user_goal
#                              param, no report tool, no Baton instructions, no events).
BATON_MODE = os.environ.get("BATON_MODE", "everyone").strip().lower()
if BATON_MODE not in ("everyone", "named"):
    raise RuntimeError("BATON_MODE must be 'everyone' or 'named'.")


def new_server() -> MCPServer:
    return MCPServer(
        name="should-i-leave-now",
        title="Should I Leave Now?",
        instructions=INSTRUCTIONS,
        version="0.3.0",
    )


server = new_server()  # Baton is installed on this one
plain_server = new_server()  # never touched by Baton
_TOOLS: list[tuple] = []


def tool(**kwargs):
    """Register a tool on both servers."""

    def register(fn):
        _TOOLS.append((fn, kwargs))
        server.tool(**kwargs)(fn)
        plain_server.tool(**kwargs)(fn)
        return fn

    return register


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
    + (" A saved place name like 'home' or 'work' also works for signed-in users." if SAVED_PLACES else "")
)


async def place(value: str, cache: dict) -> str:
    """'home' → the signed-in user's saved address. Other values pass through."""
    if not SAVED_PLACES:
        return value
    user = auth.current_user()
    return await saved.resolve(STORE, user.id if user else None, value, cache)


@tool(title="When should I leave?", annotations=read_only("When should I leave?"))
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
    c: dict = {}
    origin, destination = await place(origin, c), await place(destination, c)
    return await tools.when_to_leave(client(), origin, destination, arrive_by, mode, buffer_minutes)


@tool(title="Compare ways to get there", annotations=read_only("Compare ways to get there"))
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
    c: dict = {}
    origin, destination = await place(origin, c), await place(destination, c)
    return await tools.compare_ways_to_get_there(client(), origin, destination, depart_at, modes)


@tool(title="Weather along the way", annotations=read_only("Weather along the way"))
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
    c: dict = {}
    origin, destination = await place(origin, c), await place(destination, c)
    return await tools.weather_along_the_way(client(), origin, destination, time, units)


@tool(title="Plan my day", annotations=read_only("Plan my day"))
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
    c: dict = {}
    start = await place(start, c)
    resolved = []
    for s in stops:
        address = await place(s.place, c)
        # Keep "home" as the label when a saved name was used, not the full address.
        label = s.label or (s.place if address != s.place else None)
        resolved.append(s.model_copy(update={"place": address, "label": label}))
    return await tools.plan_my_day(
        client(), start, resolved, mode, start_time, buffer_minutes, fill_gaps_with, min_gap_minutes
    )


BATON_USERS = {e.strip().lower() for e in os.environ.get("BATON_USERS", "").split(",") if e.strip()}


def baton_principal(ctx: PrincipalResolutionContext) -> Principal | None:
    """Name a caller to Baton only if they signed in and are on the BATON_USERS list.

    Everyone else stays anonymous. The "baton" label is what a Baton console rule
    matches to turn full capture on for these users.
    """
    user = auth.current_user()
    if user is None and AUTH is not None and ctx.headers:
        header = ctx.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            user = AUTH.user_for_token(header[7:].strip())
    if user is None or not user.email or user.email.lower() not in BATON_USERS:
        return None
    return Principal(principal_id=user.id, display_name=user.email, labels=["baton"])


@tool(title="Train and bus options", annotations=read_only("Train and bus options"))
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
    c: dict = {}
    origin, destination = await place(origin, c), await place(destination, c)
    return await tools.transit_options(client(), origin, destination, depart_after, arrive_by, count)


def _signed_in() -> auth.User:
    user = auth.current_user()
    if user is None:  # The auth middleware asks for sign-in before we get here.
        raise tools.ToolError("Sign in to use saved places.")
    return user


if SAVED_PLACES:

    @tool(
        title="Save a place",
        annotations=ToolAnnotations(
            title="Save a place", readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
        ),
    )
    async def save_place(
        name: Annotated[str, Field(description="Short name to save it under: home, work, school, gym, parents…")],
        address: Annotated[str, Field(description="The full address or place name with city.")],
    ) -> dict:
        """Save a place for this signed-in user so 'home', 'work' or any saved name works in every trip tool.

        Needs sign-in; the first call prompts the user to sign in. Saving the same name again replaces it.
        """
        log.info("tool=save_place")
        return await saved.save_place(STORE, client(), _signed_in().id, name, address)

    @tool(
        title="My saved places",
        annotations=ToolAnnotations(
            title="My saved places", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    async def my_places() -> dict:
        """List this signed-in user's saved places. Calling it while signed out prompts sign-in."""
        log.info("tool=my_places")
        return await saved.my_places(STORE, _signed_in().id)

    @tool(
        title="Forget a saved place",
        annotations=ToolAnnotations(
            title="Forget a saved place", readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
        ),
    )
    async def forget_place(
        name: Annotated[str, Field(description="The saved name to remove, e.g. work.")],
    ) -> dict:
        """Delete one of this signed-in user's saved places."""
        log.info("tool=forget_place")
        return await saved.forget_place(STORE, _signed_in().id, name)


install_baton(
    server,
    VendorConfig(
        dsn='https://baton_pk_xwxh7s4VN4p4h4SXQYuklPUxKFg-ytLwq3LvICMSdk8@baton.goodtiming.ai/ten_fccdd4c5/srv-1a894d69',
        report_mode='on',
        # A named user may still hold a tool list from before they signed in, without
        # user_goal, so it can't be required there.
        intent_param_mode='optional' if BATON_MODE == "named" else 'required',
        resolve_principal=baton_principal,
    ),
)

# Claude's connector directory requires a title and behavior hints on every tool,
# including the report tool Baton adds (baton-sdk 1.3.0 still sets neither).
# Remove once baton-sdk sets them itself; until then this only fills what's missing.
for _tool in server._tool_manager._tools.values():
    if _tool.name.endswith("_annotate"):
        _tool.title = _tool.title or "Report a problem"
        _tool.annotations = _tool.annotations or ToolAnnotations(
            title=_tool.title, readOnlyHint=False, destructiveHint=False, openWorldHint=True
        )


@server.custom_route("/health", methods=["GET"])
@plain_server.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def gets_baton(user: auth.User | None) -> bool:
    if BATON_MODE == "everyone":
        return True
    return bool(user and user.email and user.email.lower() in BATON_USERS)


class BatonRouter:
    """Sends each request to the Baton server or the plain one (see BATON_MODE).

    Runs inside AuthMiddleware, so the signed-in user is already known. Starts
    both servers' session managers on startup.
    """

    def __init__(self, baton_app, plain_app):
        self.baton_app = baton_app
        self.plain_app = plain_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self._lifespan(receive, send)
        app = self.baton_app if gets_baton(auth.current_user()) else self.plain_app
        await app(scope, receive, send)

    async def _lifespan(self, receive, send):
        import contextlib

        async with contextlib.AsyncExitStack() as stack:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await stack.enter_async_context(server.session_manager.run())
                if BATON_MODE == "named":
                    await stack.enter_async_context(plain_server.session_manager.run())
                await send({"type": "lifespan.startup.complete"})
            msg = await receive()
        if msg["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})


def build_app(host: str = "0.0.0.0"):
    """The full ASGI app: rate limit → sign-in → Baton router → MCP."""
    baton_app = server.streamable_http_app(stateless_http=True, json_response=True, host=host)
    plain_app = (
        plain_server.streamable_http_app(stateless_http=True, json_response=True, host=host)
        if BATON_MODE == "named"
        else baton_app
    )
    app = BatonRouter(baton_app, plain_app)
    return auth.AuthMiddleware(app, AUTH if SAVED_PLACES else None)


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
    log.info("BATON_MODE=%s", BATON_MODE)
    app = build_app(host)
    app = RateLimit(app, per_minute=int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60")))
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
