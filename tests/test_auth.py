"""Optional sign-in and saved places, tested over real HTTP against the full app."""

import contextlib
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from leavenow import auth, saved, server
from leavenow.auth import User

from .test_tools import NOW, TRANSIT_ROUTE, FakeGoogle, make  # noqa: F401  (frozen clock fixture is autouse there)
from . import test_tools

GOOD = "good-token"
TESTER = "tester-token"


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    monkeypatch.setattr(test_tools.tools, "now_utc", lambda: NOW)


@pytest.fixture
def fake(monkeypatch):
    f = FakeGoogle(routes={"TRANSIT": TRANSIT_ROUTE})
    monkeypatch.setattr(server, "_client", make(f))
    monkeypatch.setattr(server, "STORE", saved.MemoryStore())

    async def verify(token):
        users = {GOOD: User("user_1", "someone@example.com"), TESTER: User("user_2", "tester@example.com")}
        if token not in users:
            raise jwt.InvalidTokenError("bad")
        return users[token]

    monkeypatch.setattr(server.AUTH, "verify", verify)
    return f


@contextlib.asynccontextmanager
async def serve():
    """The full app (MCP transport behind the auth middleware), started inside the test's own task."""
    app = server.server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    app = auth.AuthMiddleware(app, server.AUTH)
    async with server.server.session_manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            yield c


def call(name, args, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": name, "arguments": args}}


async def post(http, body, token=None):
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return await http.post("/mcp", json=body, headers=headers)


def result_text(r):
    res = r.json()["result"]
    return res.get("isError"), res["content"][0]["text"]


async def test_metadata_points_claude_at_workos(fake):
    async with serve() as http:
        r = await http.get("/.well-known/oauth-protected-resource/mcp")
        assert r.status_code == 200
        assert r.json()["resource"] == "https://should-i-leave-now.onrender.com/mcp"
        assert r.json()["authorization_servers"] == ["https://test.authkit.app"]
        assert (await http.get("/.well-known/oauth-protected-resource")).status_code == 200


async def test_trip_tools_work_signed_out(fake):
    async with serve() as http:
        r = await post(http, call("when_to_leave", {"origin": "home", "destination": "Dwinelle Hall", "arrive_by": "2026-11-04T09:00"}))
        assert r.status_code == 200
        is_error, text = result_text(r)
        # 'home' needs sign-in; the error tells the model how to get it.
        assert is_error and "my_places to sign in" in text
        r = await post(http, call("when_to_leave", {"origin": "2600 Durant Ave", "destination": "Dwinelle Hall", "arrive_by": "2026-11-04T09:00"}))
        assert r.status_code == 200 and not result_text(r)[0]


async def test_saved_place_tools_ask_for_sign_in(fake):
    async with serve() as http:
        r = await post(http, call("my_places", {}))
        assert r.status_code == 401
        challenge = r.headers["www-authenticate"]
        assert challenge.startswith("Bearer ")
        assert 'resource_metadata="https://should-i-leave-now.onrender.com/.well-known/oauth-protected-resource/mcp"' in challenge
        # Listing tools never needs sign-in.
        r = await post(http, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert r.status_code == 200
        names = [t["name"] for t in r.json()["result"]["tools"]]
        assert {"save_place", "my_places", "forget_place"} <= set(names)


async def test_bad_token_gets_a_fresh_sign_in(fake):
    async with serve() as http:
        r = await post(http, call("when_to_leave", {"origin": "a", "destination": "b", "arrive_by": "2026-11-04T09:00"}), token="expired")
        assert r.status_code == 401 and "invalid_token" in r.headers["www-authenticate"]


async def test_save_home_then_use_it_in_a_trip(fake):
    async with serve() as http:
        r = await post(http, call("save_place", {"name": "My Home", "address": "home"}), token=GOOD)
        is_error, text = result_text(r)
        assert not is_error, text
        assert json.loads(text)["saved"] == "home"
        assert json.loads(text)["address"] == "2600 Durant Ave, Berkeley, CA"

        _, text = result_text(await post(http, call("my_places", {}), token=GOOD))
        assert json.loads(text)["places"] == [{"name": "home", "address": "2600 Durant Ave, Berkeley, CA"}]

        r = await post(http, call("when_to_leave", {"origin": "home", "destination": "Dwinelle Hall", "arrive_by": "2026-11-04T09:00"}), token=GOOD)
        assert not result_text(r)[0]
        assert fake.route_bodies[-1]["origin"]["address"] == "2600 Durant Ave, Berkeley, CA"

        # Another user doesn't see it.
        _, text = result_text(await post(http, call("my_places", {}), token=TESTER))
        assert json.loads(text)["places"] == []

        _, text = result_text(await post(http, call("forget_place", {"name": "home"}), token=GOOD))
        assert json.loads(text) == {"forgotten": "home"}


async def test_plan_my_day_keeps_saved_names_as_labels(fake, monkeypatch):
    async with serve() as http:
        monkeypatch.setattr(fake, "routes", test_tools.bus_route)
        await post(http, call("save_place", {"name": "work", "address": "soda hall"}), token=GOOD)
        r = await post(http, call("plan_my_day", {"start": "2600 Durant Ave", "stops": [{"place": "work", "arrive_by": "2026-11-04T09:00"}]}), token=GOOD)
        is_error, text = result_text(r)
        assert not is_error, text
        stop = [s for s in json.loads(text)["schedule"] if s["type"] == "stop"][0]
        assert stop["label"] == "work" and stop["place"] == "Soda Hall, Berkeley, CA"


async def test_save_place_rejects_bad_names_and_too_many(fake):
    store = saved.MemoryStore()
    with pytest.raises(saved.ToolError, match="short name"):
        await saved.save_place(store, server._client, "u", "my favorite place in the whole world!!", "home")
    for i in range(saved.MAX_PLACES):
        await store.put("u", f"p{i}", {"address": "x"})
    with pytest.raises(saved.ToolError, match="up to 20"):
        await saved.save_place(store, server._client, "u", "home", "home")


# ---- real token checks ---------------------------------------------------------


@pytest.fixture
def signer(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = auth.AuthSettings("test.authkit.app")

    class Found:
        def __init__(self, k):
            self.key = k

    monkeypatch.setattr(settings.jwks, "get_signing_key_from_jwt", lambda token: Found(key.public_key()))

    def sign(**overrides):
        claims = {
            "sub": "user_9", "iss": "https://test.authkit.app", "aud": settings.resource,
            "exp": int(time.time()) + 300, "email": "e@x.com",
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, key, algorithm="RS256")

    return settings, sign


async def test_valid_workos_token_gives_the_user(signer):
    settings, sign = signer
    user = await settings.verify(sign())
    assert user == User("user_9", "e@x.com")


@pytest.mark.parametrize(
    "bad",
    [{"aud": "https://someone-else.example/mcp"}, {"iss": "https://evil.authkit.app"}, {"exp": int(time.time()) - 10},
     {"sub_profile": "ai_agent"}, {"sub": None}],
)
async def test_bad_tokens_are_rejected(signer, bad):
    settings, sign = signer
    with pytest.raises(jwt.PyJWTError):
        await settings.verify(sign(**bad))


# ---- Baton tagging ---------------------------------------------------------------


def test_baton_names_only_listed_testers():
    t = auth._current.set(User("user_2", "Tester@Example.com"))
    try:
        p = server.baton_principal(type("Ctx", (), {"headers": {}})())
        assert p is not None and p.labels == ["baton"] and p.display_name == "Tester@Example.com"
    finally:
        auth._current.reset(t)
    t = auth._current.set(User("user_1", "someone@example.com"))
    try:
        assert server.baton_principal(type("Ctx", (), {"headers": {}})()) is None
    finally:
        auth._current.reset(t)
    assert server.baton_principal(type("Ctx", (), {"headers": {}})()) is None
