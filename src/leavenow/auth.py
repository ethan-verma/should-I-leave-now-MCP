"""Optional sign-in with WorkOS AuthKit.

Anyone can use the trip tools with no account. Only the saved-places tools need
sign-in. When a signed-out user calls one of them, the HTTP request is answered
with 401 and a WWW-Authenticate header pointing at our protected resource
metadata. Claude (or ChatGPT) then runs the WorkOS sign-in and retries the call.

Turned on by setting AUTHKIT_DOMAIN. Without it the server stays fully authless.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass

import anyio
import httpx
import jwt
from starlette.responses import JSONResponse

log = logging.getLogger("leavenow.auth")

PROTECTED_TOOLS = frozenset({"save_place", "my_places", "forget_place"})
METADATA_PATHS = ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp")
# With lazy sign-in, the metadata lives only here, so Claude doesn't find it when
# the connector is added and only asks for sign-in when a saved-places tool is called.
LAZY_METADATA_PATH = "/oauth/protected-resource"
DEFAULT_RESOURCE = "https://should-i-leave-now.onrender.com/mcp"
# What Claude asks WorkOS for. offline_access gives a refresh token, so users
# stay signed in past the 5-minute access token.
SCOPES = ("openid", "profile", "email", "offline_access")


@dataclass(frozen=True)
class User:
    id: str
    email: str | None = None


_current: ContextVar[User | None] = ContextVar("current_user", default=None)


def current_user() -> User | None:
    return _current.get()


class AuthSettings:
    def __init__(
        self,
        authkit_domain: str,
        resource: str = DEFAULT_RESOURCE,
        workos_api_key: str | None = None,
        lazy: bool = False,
    ):
        self.lazy = lazy
        domain = authkit_domain.strip().rstrip("/")
        self.issuer = domain if domain.startswith("https://") else f"https://{domain}"
        self.resource = resource
        self.workos_api_key = workos_api_key
        self.jwks = jwt.PyJWKClient(f"{self.issuer}/oauth2/jwks", cache_keys=True, lifespan=3600)
        # Verified tokens → user, so a token is only checked once while it's valid.
        self._seen: OrderedDict[str, tuple[float, User]] = OrderedDict()
        self._emails: dict[str, str] = {}

    @classmethod
    def from_env(cls) -> AuthSettings | None:
        domain = os.environ.get("AUTHKIT_DOMAIN")
        if not domain:
            return None
        return cls(
            domain,
            os.environ.get("PUBLIC_URL", DEFAULT_RESOURCE),
            os.environ.get("WORKOS_API_KEY"),
            lazy=os.environ.get("LAZY_SIGNIN", "").lower() in ("1", "true", "yes"),
        )

    def metadata_paths(self) -> tuple[str, ...]:
        return (LAZY_METADATA_PATH,) if self.lazy else METADATA_PATHS

    # ---- what Claude reads ---------------------------------------------------

    def metadata_url(self) -> str:
        origin = self.resource.split("/", 3)
        base = "/".join(origin[:3])
        return base + (LAZY_METADATA_PATH if self.lazy else "/.well-known/oauth-protected-resource/mcp")

    def metadata(self) -> dict:
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "bearer_methods_supported": ["header"],
            "scopes_supported": list(SCOPES),
            "resource_name": "Should I Leave Now?",
        }

    def challenge(self, description: str, error: str = "invalid_token") -> JSONResponse:
        header = (
            f'Bearer error="{error}", error_description="{description}", '
            f'resource_metadata="{self.metadata_url()}", scope="{" ".join(SCOPES)}"'
        )
        return JSONResponse(
            {"error": error, "error_description": description},
            status_code=401,
            headers={"WWW-Authenticate": header},
        )

    # ---- tokens ---------------------------------------------------------------

    def _decode(self, token: str) -> dict:
        key = self.jwks.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            key.key,
            algorithms=["RS256", "ES256"],
            audience=self.resource,
            issuer=self.issuer,
            options={"require": ["exp", "sub"]},
        )

    async def verify(self, token: str) -> User:
        """The signed-in user, or raises jwt.PyJWTError."""
        hit = self._seen.get(token)
        if hit and hit[0] > time.time():
            return hit[1]
        claims = await anyio.to_thread.run_sync(self._decode, token)
        if claims.get("sub_profile") == "ai_agent":
            raise jwt.InvalidTokenError("agent tokens are not accepted")
        user = User(id=claims["sub"], email=claims.get("email") or await self._email_for(claims["sub"]))
        self._seen[token] = (float(claims["exp"]), user)
        while len(self._seen) > 5000:
            self._seen.popitem(last=False)
        return user

    def user_for_token(self, token: str) -> User | None:
        """A token this process already verified, without network calls."""
        hit = self._seen.get(token)
        return hit[1] if hit and hit[0] > time.time() else None

    async def _email_for(self, user_id: str) -> str | None:
        """WorkOS access tokens carry the user id; the email comes from the WorkOS API."""
        if not self.workos_api_key:
            return None
        if user_id in self._emails:
            return self._emails[user_id]
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(
                    f"https://api.workos.com/user_management/users/{user_id}",
                    headers={"Authorization": f"Bearer {self.workos_api_key}"},
                )
            email = r.json().get("email") if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            email = None
        if email:
            self._emails[user_id] = email
        return email


def _bearer(scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            parts = value.decode("latin-1").split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
                return parts[1].strip()
    return None


def _calls_protected_tool(body: bytes) -> bool:
    try:
        msg = json.loads(body or b"null")
    except ValueError:
        return False
    for m in msg if isinstance(msg, list) else [msg]:
        if isinstance(m, dict) and m.get("method") == "tools/call":
            if (m.get("params") or {}).get("name") in PROTECTED_TOOLS:
                return True
    return False


class AuthMiddleware:
    """Serves the metadata, checks tokens, and asks for sign-in only where needed."""

    def __init__(self, app, settings: AuthSettings | None):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        s = self.settings
        if scope["type"] != "http" or s is None:
            return await self.app(scope, receive, send)

        if scope["method"] == "GET" and scope.get("path") in s.metadata_paths():
            resp = JSONResponse(s.metadata(), headers={"Access-Control-Allow-Origin": "*"})
            return await resp(scope, receive, send)

        token = _bearer(scope)
        if token:
            try:
                user = await s.verify(token)
            except jwt.PyJWTError as e:
                log.info("rejected token: %s", type(e).__name__)
                return await s.challenge("Sign-in expired or invalid. Sign in again.")(scope, receive, send)
            t = _current.set(user)
            try:
                return await self.app(scope, receive, send)
            finally:
                _current.reset(t)

        if scope["method"] != "POST":
            return await self.app(scope, receive, send)

        # Signed out: read the body to see which tool is being called, then replay it.
        chunks, more = [], True
        while more:
            msg = await receive()
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        body = b"".join(chunks)
        if _calls_protected_tool(body):
            return await s.challenge("Sign in to use saved places.")(scope, receive, send)

        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
