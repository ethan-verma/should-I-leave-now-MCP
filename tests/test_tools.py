"""Tests run against a fake Google, so they need no API key and cost nothing."""

import json
from datetime import datetime, timezone

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from leavenow import server, tools
from leavenow.google import GoogleClient

NOW = datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc)  # 8:00 AM in Berkeley (PST)

PLACES = {
    "home": ("2600 Durant Ave, Berkeley, CA", 37.8680, -122.2560),
    "dwinelle hall": ("Dwinelle Hall, Berkeley, CA", 37.8706, -122.2606),
    "2600 durant ave": ("2600 Durant Ave, Berkeley, CA", 37.8680, -122.2560),
    "soda hall": ("Soda Hall, Berkeley, CA", 37.8756, -122.2588),
}

TRANSIT_ROUTE = {
    "duration": "1500s",
    "distanceMeters": 3200,
    "legs": [
        {
            "steps": [
                {"travelMode": "WALK", "staticDuration": "300s"},
                {
                    "travelMode": "TRANSIT",
                    "staticDuration": "720s",
                    "transitDetails": {
                        "stopDetails": {
                            "departureStop": {"name": "Telegraph & Durant"},
                            "arrivalStop": {"name": "Bancroft & College"},
                            "departureTime": "2026-11-04T16:40:00Z",
                            "arrivalTime": "2026-11-04T16:52:00Z",
                        },
                        "headsign": "Downtown Berkeley",
                        "stopCount": 4,
                        "transitLine": {"nameShort": "6", "vehicle": {"type": "BUS"}},
                    },
                },
                {"travelMode": "WALK", "staticDuration": "180s"},
            ]
        }
    ],
    "travelAdvisory": {"transitFare": {"currencyCode": "USD", "units": "2", "nanos": 500000000}},
}


class FakeGoogle:
    """Answers like Google would. `routes` maps travel mode → route (or None for no route)."""

    def __init__(self, routes=None, forecast=None, air=None, places=None):
        self.routes = routes or {}
        self.places = places or []
        self.place_bodies = []
        self.forecast = forecast or []
        self.air = air or {}
        self.route_bodies = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "geocode" in url:
            name = request.url.params["address"].lower()
            if name not in PLACES:
                return httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []})
            addr, lat, lng = PLACES[name]
            return httpx.Response(
                200,
                json={"status": "OK", "results": [{"formatted_address": addr, "geometry": {"location": {"lat": lat, "lng": lng}}}]},
            )
        if "timezone" in url:
            return httpx.Response(200, json={"status": "OK", "timeZoneId": "America/Los_Angeles"})
        if "computeRoutes" in url:
            body = json.loads(request.content)
            self.route_bodies.append(body)
            route = self.routes(body) if callable(self.routes) else self.routes.get(body["travelMode"])
            return httpx.Response(200, json={"routes": [route]} if route else {})
        if "forecast/hours" in url:
            return httpx.Response(200, json={"forecastHours": self.forecast})
        if "searchText" in url:
            self.place_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"places": self.places})
        if "airquality" in url:
            return httpx.Response(200, json=self.air)
        return httpx.Response(404, json={"error": {"message": "unexpected url " + url}})


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(tools, "now_utc", lambda: NOW)


def make(fake: FakeGoogle) -> GoogleClient:
    return GoogleClient("test-key", transport=httpx.MockTransport(fake))


async def test_transit_leave_time_counts_walking_and_buffer():
    fake = FakeGoogle(routes={"TRANSIT": TRANSIT_ROUTE})
    out = await tools.when_to_leave(make(fake), "home", "Dwinelle Hall", "2026-11-04T09:00")

    # Naive 9:00 is read as Berkeley local time = 17:00 UTC.
    assert fake.route_bodies[0]["arrivalTime"] == "2026-11-04T17:00:00Z"
    # Bus boards 8:40, minus 5 min walk, minus 5 min buffer = 8:30.
    assert out["leave_by"]["local_time"] == "8:30 AM"
    assert out["minutes_until_leave"] == 30
    assert out["status"] == "leave in 30 min"
    # Gets off 8:52 + 3 min walk.
    assert out["expected_arrival"]["local_time"] == "8:55 AM"
    walk_to, ride, walk_from = out["trip"]["directions"]
    assert walk_to == {"do": "walk", "minutes": 5, "to": "Telegraph & Durant"}
    assert ride["line"] == "6" and ride["board_at"] == "Telegraph & Durant" and ride["departs"] == "8:40 AM"
    assert walk_from == {"do": "walk", "minutes": 3, "to": "your destination"}
    assert out["trip"]["transfers"] == 0
    assert out["trip"]["door_to_door_minutes"] == 25
    assert out["trip"]["fare"] == "2.50 USD"


async def test_driving_adjusts_departure_for_traffic():
    fake = FakeGoogle(routes={"DRIVE": {"duration": "1200s", "distanceMeters": 8000, "legs": []}})
    out = await tools.when_to_leave(make(fake), "home", "Dwinelle Hall", "2026-11-04T09:00", mode="car")
    # 20 min drive before 9:00 = 8:40, minus 5 min buffer.
    assert out["leave_by"]["local_time"] == "8:35 AM"
    assert all("departureTime" in b and b["routingPreference"] == "TRAFFIC_AWARE" for b in fake.route_bodies)


async def test_driving_arrival_includes_the_buffer_as_slack():
    # From Baton feedback: a 21 min drive to a 9:00 meeting with 15 min for parking.
    fake = FakeGoogle(routes={"DRIVE": {"duration": "1260s", "distanceMeters": 16000, "legs": []}})
    out = await tools.when_to_leave(
        make(fake), "home", "Dwinelle Hall", "2026-11-04T09:00", mode="drive", buffer_minutes=15
    )
    assert out["leave_by"]["local_time"] == "8:24 AM"
    # Leaving at 8:24 and driving 21 min gets you there at 8:45, not 8:59.
    assert out["expected_arrival"]["local_time"] == "8:45 AM"
    assert out["slack_minutes"] == 15


async def test_transit_buffer_is_waiting_time_so_arrival_stays_put():
    fake = FakeGoogle(routes={"TRANSIT": TRANSIT_ROUTE})
    out = await tools.when_to_leave(make(fake), "home", "Dwinelle Hall", "2026-11-04T09:00")
    # Same bus whether you leave at 8:30 or 8:35, so arrival is still 8:55.
    assert out["expected_arrival"]["local_time"] == "8:55 AM"
    assert out["slack_minutes"] == 5


async def test_late_status_when_leave_time_has_passed():
    fake = FakeGoogle(routes={"WALK": {"duration": "3600s", "distanceMeters": 5000, "legs": []}})
    out = await tools.when_to_leave(make(fake), "home", "Dwinelle Hall", "2026-11-04T08:30", mode="walk")
    assert out["status"].startswith("late")
    assert out["minutes_until_leave"] == -35


async def test_rejects_arrival_time_in_the_past():
    with pytest.raises(ToolError, match="already passed"):
        await tools.when_to_leave(make(FakeGoogle()), "home", "Dwinelle Hall", "2026-11-04T07:00")


async def test_unknown_place_gives_helpful_error():
    with pytest.raises(ToolError, match="Couldn't find 'Narnia'"):
        await tools.when_to_leave(make(FakeGoogle()), "home", "Narnia", "2026-11-04T09:00")


async def test_bad_mode_and_bad_time():
    with pytest.raises(ToolError, match="Unknown mode"):
        await tools.when_to_leave(make(FakeGoogle()), "home", "Dwinelle Hall", "2026-11-04T09:00", mode="teleport")
    with pytest.raises(ToolError, match="Couldn't read the time"):
        await tools.when_to_leave(make(FakeGoogle()), "home", "Dwinelle Hall", "9am tomorrow")


async def test_compare_sorts_fastest_first_and_reports_missing_modes():
    fake = FakeGoogle(
        routes={
            "TRANSIT": TRANSIT_ROUTE,
            "WALK": {"duration": "1800s", "distanceMeters": 2000, "legs": []},
            "BICYCLE": {"duration": "600s", "distanceMeters": 2100, "legs": []},
            # no DRIVE route
        }
    )
    out = await tools.compare_ways_to_get_there(make(fake), "home", "Dwinelle Hall")
    assert out["fastest"] == "bicycle"
    assert [o["mode"] for o in out["options"]] == ["bicycle", "transit", "walk", "drive"]
    assert out["options"][-1]["error"] == "no route"
    assert out["options"][1]["arrives"] == "8:55 AM"


def _hour(start, end, rain, temp, kind=None):
    return {
        "interval": {"startTime": start, "endTime": end},
        "weatherCondition": {
            "type": kind or ("RAIN" if rain > 50 else "CLOUDY"),
            "description": {"text": "Rain" if rain > 50 else "Cloudy"},
        },
        "temperature": {"degrees": temp, "unit": "FAHRENHEIT"},
        "precipitation": {"probability": {"percent": rain}},
        "wind": {"speed": {"value": 8, "unit": "MILES_PER_HOUR"}},
    }


async def test_weather_picks_the_travel_hour_and_flags_rain():
    fake = FakeGoogle(
        forecast=[
            _hour("2026-11-04T16:00:00Z", "2026-11-04T17:00:00Z", 10, 52),
            _hour("2026-11-04T17:00:00Z", "2026-11-04T18:00:00Z", 70, 54),
        ],
        air={
            "indexes": [
                {"code": "uaqi", "aqi": 80, "category": "Good air quality"},
                {"code": "usa_epa", "displayName": "AQI (US)", "aqi": 35, "category": "Good air quality", "dominantPollutant": "pm25"},
            ],
            "healthRecommendations": {"generalPopulation": "Enjoy outdoor activities."},
        },
    )
    out = await tools.weather_along_the_way(make(fake), "home", "Dwinelle Hall", time="2026-11-04T09:15")
    assert out["origin"]["weather"]["chance_of_rain_percent"] == 70
    assert out["origin"]["weather"]["temperature"] == "54°F"
    assert out["rain_likely_on_trip"] is True
    assert out["destination"]["air"]["aqi"] == 35  # prefers the local (US) index
    assert out["destination"]["air"]["advice"] == "Enjoy outdoor activities."


async def test_cache_avoids_repeat_google_calls():
    fake = FakeGoogle(routes={"TRANSIT": TRANSIT_ROUTE})
    c = make(fake)
    await tools.when_to_leave(c, "home", "Dwinelle Hall", "2026-11-04T09:00")
    await tools.when_to_leave(c, "home", "Dwinelle Hall", "2026-11-04T09:00")
    assert len(fake.route_bodies) == 1


async def test_tool_runs_through_the_mcp_server(monkeypatch):
    monkeypatch.setattr(server, "_client", make(FakeGoogle(routes={"TRANSIT": TRANSIT_ROUTE})))
    result = await server.server.call_tool(
        "when_to_leave", {"origin": "2600 Durant Ave", "destination": "Dwinelle Hall", "arrive_by": "2026-11-04T09:00"}
    )
    assert not result.is_error
    assert "8:30 AM" in result.content[0].text


async def test_rate_limit_blocks_after_limit():
    async def ok_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = server.RateLimit(ok_app, per_minute=2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        codes = [(await c.post("/mcp")).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        assert (await c.get("/health")).status_code == 200


# ---- plan_my_day -------------------------------------------------------------


def bus_route(body):
    """A fake bus trip: 3 min walk, 10 min ride, 2 min walk. Fits whatever time was asked for."""
    from datetime import timedelta

    def z(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    if "arrivalTime" in body:
        arrive = datetime.fromisoformat(body["arrivalTime"])
        board = arrive - timedelta(minutes=15)
    else:
        board = datetime.fromisoformat(body["departureTime"]) + timedelta(minutes=5)
    alight = board + timedelta(minutes=10)
    return {
        "duration": "900s",
        "distanceMeters": 2500,
        "legs": [
            {
                "steps": [
                    {"travelMode": "WALK", "staticDuration": "180s"},
                    {
                        "travelMode": "TRANSIT",
                        "transitDetails": {
                            "stopDetails": {
                                "departureStop": {"name": "Stop A"},
                                "arrivalStop": {"name": "Stop B"},
                                "departureTime": z(board),
                                "arrivalTime": z(alight),
                            },
                            "transitLine": {"nameShort": "51B", "vehicle": {"type": "BUS"}},
                        },
                    },
                    {"travelMode": "WALK", "staticDuration": "120s"},
                ]
            }
        ],
    }


CAFE = {
    "displayName": {"text": "Free Speech Movement Cafe"},
    "formattedAddress": "Moffitt Library, Berkeley, CA",
    "rating": 4.3,
    "googleMapsUri": "https://maps.google.com/?cid=1",
    "location": {"latitude": 37.8726, "longitude": -122.2606},  # ~220 m from Dwinelle
    "regularOpeningHours": {
        "weekdayDescriptions": [f"{d}: 8:00 AM – 6:00 PM" for d in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]]
    },
}


async def test_plan_my_day_builds_schedule_with_free_time_suggestions():
    far_cafe = {"displayName": {"text": "Far Away Coffee"}, "location": {"latitude": 37.8697, "longitude": -122.2950}}
    fake = FakeGoogle(routes=bus_route, places=[far_cafe, CAFE])
    stops = [
        tools.Stop(place="Dwinelle Hall", arrive_by="2026-11-04T09:00", stay_minutes=50, label="Lecture"),
        tools.Stop(place="Soda Hall", arrive_by="2026-11-04T14:00", stay_minutes=120, label="Lab"),
    ]
    out = await tools.plan_my_day(make(fake), "home", stops, fill_gaps_with="quiet cafe")

    assert out["warnings"] == []
    assert out["first_leave_by"]["local_time"] == "8:37 AM"  # board 8:45 - 3 min walk - 5 min buffer
    kinds = [s["type"] for s in out["schedule"]]
    # Only 37 min free before the first trip (under 45), so no free block at the start.
    assert kinds == ["travel", "stop", "free_time", "travel", "stop"]

    lecture = out["schedule"][1]
    assert (lecture["from"], lecture["until"]) == ("9:00 AM", "9:50 AM")

    gap = out["schedule"][2]
    assert (gap["at"], gap["from"], gap["until"], gap["minutes"]) == ("Lecture", "9:50 AM", "1:37 PM", 227)
    # The 3 km-away cafe is dropped even though Google returned it first.
    assert [c["name"] for c in gap["suggestions"]] == ["Free Speech Movement Cafe"]
    cafe = gap["suggestions"][0]
    assert cafe["walk_minutes"] == 3
    assert cafe["hours_that_day"].startswith("Wednesday")  # Nov 4 2026 is a Wednesday
    # Search box is around Dwinelle (where they are), not home.
    box = fake.place_bodies[0]["locationRestriction"]["rectangle"]
    assert box["low"]["latitude"] < 37.8706 < box["high"]["latitude"]
    assert out["total_travel_minutes"] == 30


async def test_plan_my_day_warns_when_a_stop_cant_be_reached_on_time():
    fake = FakeGoogle(routes=bus_route)
    stops = [
        tools.Stop(place="Dwinelle Hall", arrive_by="2026-11-04T09:00", stay_minutes=50, label="Lecture"),
        tools.Stop(place="Soda Hall", arrive_by="2026-11-04T10:00", label="Section"),
    ]
    out = await tools.plan_my_day(make(fake), "home", stops)

    assert len(out["warnings"]) == 1
    w = out["warnings"][0]
    assert "Section" in w and "until 9:50 AM" in w and "about 7 min late" in w
    to_section = out["schedule"][2]
    assert to_section["leave_by"]["local_time"] == "9:52 AM"  # leaves when lecture ends
    assert out["schedule"][3]["from"] == "10:07 AM"


async def test_plan_my_day_stop_without_time_follows_previous():
    fake = FakeGoogle(routes=bus_route)
    stops = [
        tools.Stop(place="Dwinelle Hall", arrive_by="2026-11-04T09:00", stay_minutes=60),
        tools.Stop(place="Soda Hall", stay_minutes=30),
    ]
    out = await tools.plan_my_day(make(fake), "home", stops)
    second_trip = out["schedule"][2]
    assert second_trip["from"] == "Dwinelle Hall" and second_trip["to"] == "Soda Hall"
    # Lecture ends 10:00, bus leaves 10:05, walk starts 10:02, arrive 10:17.
    assert second_trip["leave_by"]["local_time"] == "10:02 AM"
    assert out["schedule"][3]["until"] == "10:47 AM"


async def test_plan_my_day_rejects_past_stop_and_too_many_stops():
    with pytest.raises(ToolError, match="has passed"):
        await tools.plan_my_day(
            make(FakeGoogle(routes=bus_route)), "home", [tools.Stop(place="Dwinelle Hall", arrive_by="2026-11-04T07:00")]
        )
    with pytest.raises(ToolError, match="up to 8"):
        await tools.plan_my_day(make(FakeGoogle()), "home", [tools.Stop(place="Dwinelle Hall")] * 9)


async def test_plan_my_day_through_the_mcp_server(monkeypatch):
    monkeypatch.setattr(server, "_client", make(FakeGoogle(routes=bus_route)))
    result = await server.server.call_tool(
        "plan_my_day",
        {"start": "2600 Durant Ave", "stops": [{"place": "Dwinelle Hall", "arrive_by": "2026-11-04T09:00", "label": "Lecture"}]},
    )
    assert not result.is_error, result.content[0].text
    assert "8:37 AM" in result.content[0].text


async def test_light_rain_counts_as_rain_even_with_low_chance():
    fake = FakeGoogle(forecast=[_hour("2026-11-04T16:00:00Z", "2026-11-04T17:00:00Z", 20, 55, kind="LIGHT_RAIN")])
    out = await tools.weather_along_the_way(make(fake), "home", "Dwinelle Hall")
    assert out["origin"]["weather"]["rain_likely"] is True
    assert out["origin"]["place"] == "home"  # the user's own words, with Google's address alongside
    assert out["origin"]["address"] == "2600 Durant Ave, Berkeley, CA"


def test_span_reads_like_a_person():
    assert tools.span(741) == "12 h 21 min"
    assert tools.span(40) == "40 min"
    assert tools.span(-120) == "2 h"


async def test_long_wait_status_uses_hours():
    fake = FakeGoogle(routes={"WALK": {"duration": "600s", "distanceMeters": 800, "legs": []}})
    out = await tools.when_to_leave(make(fake), "home", "Dwinelle Hall", "2026-11-04T20:00", mode="walk")
    assert out["status"] == "leave in 11 h 45 min"
    assert out["destination"] == "Dwinelle Hall"


async def test_plan_my_day_drive_leg_arrives_early_by_the_buffer():
    fake = FakeGoogle(routes={"DRIVE": {"duration": "1200s", "distanceMeters": 8000, "legs": []}})
    stops = [tools.Stop(place="Dwinelle Hall", arrive_by="2026-11-04T09:00")]
    out = await tools.plan_my_day(make(fake), "home", stops, mode="drive", buffer_minutes=10)
    leg = out["schedule"][0]
    assert leg["leave_by"]["local_time"] == "8:30 AM"
    assert leg["arrive"]["local_time"] == "8:50 AM"
    assert leg["slack_minutes"] == 10


# ---- transit_options ---------------------------------------------------------


async def test_transit_options_lists_next_departures_in_order():
    # bus_route boards 5 min after the asked time, so stepping a minute past each
    # boarding gives a bus every 6 min.
    fake = FakeGoogle(routes=bus_route)
    out = await tools.transit_options(make(fake), "home", "Dwinelle Hall", depart_after="2026-11-04T08:00", count=3)
    leaves = [o["leave_door"] for o in out["options"]]
    assert leaves == ["8:02 AM", "8:08 AM", "8:14 AM"]  # board 8:05/8:11/8:17 minus 3 min walk
    first = out["options"][0]
    assert first["arrive_door"] == "8:17 AM"
    assert first["door_to_door_minutes"] == 15
    assert [d["do"] for d in first["directions"]] == ["walk", "ride", "walk"]
    assert first["directions"][1]["line"] == "51B"
    assert len(fake.route_bodies) == 3


async def test_transit_options_works_back_from_arrive_by():
    fake = FakeGoogle(routes=bus_route)
    out = await tools.transit_options(make(fake), "home", "Dwinelle Hall", arrive_by="2026-11-04T09:00", count=3)
    leaves = [o["leave_door"] for o in out["options"]]
    # Earliest first; each arrives before 9:00.
    assert leaves == sorted(leaves, key=lambda s: datetime.strptime(s, "%I:%M %p"))
    assert out["latest_that_makes_it"] == leaves[-1] == "8:42 AM"
    assert all(o["arrive_door"] <= "8:57 AM" for o in out["options"])


async def test_transit_options_skips_trips_that_leave_in_the_past():
    fake = FakeGoogle(routes=bus_route)
    # 8:20 arrival: the only buses that make it would mean leaving before 8:00 (now).
    out = await tools.transit_options(make(fake), "home", "Dwinelle Hall", arrive_by="2026-11-04T08:20", count=4)
    assert all(o["leave_door"] >= "8:00 AM" for o in out["options"])
    assert len(out["options"]) < 4 and "note" in out


async def test_transit_options_rejects_both_times():
    with pytest.raises(ToolError, match="not both"):
        await tools.transit_options(
            make(FakeGoogle()), "home", "Dwinelle Hall", depart_after="2026-11-04T08:00", arrive_by="2026-11-04T09:00"
        )


async def test_transit_options_through_the_mcp_server(monkeypatch):
    monkeypatch.setattr(server, "_client", make(FakeGoogle(routes=bus_route)))
    result = await server.server.call_tool(
        "transit_options", {"origin": "2600 Durant Ave", "destination": "Dwinelle Hall", "count": 2}
    )
    assert not result.is_error, result.content[0].text
    assert "leave_door" in result.content[0].text


async def test_every_tool_meets_the_directory_annotation_rule():
    # Claude's connector directory rejects tools without a title and a read/write hint.
    for t in await server.server.list_tools():
        assert t.title, f"{t.name} has no title"
        assert t.annotations and t.annotations.title, f"{t.name} has no annotations.title"
        # ChatGPT's directory also requires all three hints set explicitly.
        for hint in ("read_only_hint", "destructive_hint", "open_world_hint"):
            assert getattr(t.annotations, hint) is not None, f"{t.name} has no explicit {hint}"
        assert len(t.name) <= 64
