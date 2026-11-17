"""Saved places ("home", "work", "school", or any short name) for signed-in users.

Stored per WorkOS user id in Firestore: users/{user_id} → {"places": {name: place}}.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Protocol

from mcp.server.mcpserver.exceptions import ToolError

from .google import GoogleAPIError, GoogleClient

MAX_PLACES = 20
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")

# Everyday ways of saying a saved place's name.
ALIASES = {
    "home": "home", "my home": "home", "my house": "home", "my place": "home", "my apartment": "home",
    "my apt": "home", "my dorm": "home",
    "work": "work", "my work": "work", "office": "work", "my office": "work", "my job": "work",
    "school": "school", "my school": "school", "campus": "school", "my campus": "school",
}


class PlaceStore(Protocol):
    async def get(self, user_id: str) -> dict[str, dict]: ...
    async def put(self, user_id: str, name: str, place: dict) -> None: ...
    async def delete(self, user_id: str, name: str) -> bool: ...


class MemoryStore:
    """For tests and local runs. Forgets everything on restart."""

    def __init__(self):
        self.data: dict[str, dict[str, dict]] = {}

    async def get(self, user_id):
        return dict(self.data.get(user_id, {}))

    async def put(self, user_id, name, place):
        self.data.setdefault(user_id, {})[name] = place

    async def delete(self, user_id, name):
        return self.data.get(user_id, {}).pop(name, None) is not None


class FirestoreStore:
    def __init__(self, credentials_json: str):
        from google.cloud import firestore
        from google.oauth2 import service_account

        info = json.loads(credentials_json)
        creds = service_account.Credentials.from_service_account_info(info)
        self._fs = firestore
        self.db = firestore.AsyncClient(project=info["project_id"], credentials=creds)

    def _doc(self, user_id):
        return self.db.collection("users").document(user_id)

    async def get(self, user_id):
        snap = await self._doc(user_id).get()
        return dict((snap.to_dict() or {}).get("places") or {}) if snap.exists else {}

    async def put(self, user_id, name, place):
        await self._doc(user_id).set({"places": {name: place}}, merge=True)

    async def delete(self, user_id, name):
        places = await self.get(user_id)
        if name not in places:
            return False
        await self._doc(user_id).update({f"places.{name}": self._fs.DELETE_FIELD})
        return True


def store_from_env() -> PlaceStore | None:
    if os.environ.get("PLACES_STORE") == "memory":
        return MemoryStore()
    creds = os.environ.get("FIRESTORE_CREDENTIALS_JSON")
    return FirestoreStore(creds) if creds else None


def clean_name(name: str) -> str:
    n = ALIASES.get(name.strip().lower(), name.strip().lower())
    n = re.sub(r"[\s]+", "_", n)
    if not NAME_RE.match(n):
        raise ToolError("Use a short name for the place, like home, work, school, gym or parents (letters, numbers, - or _).")
    return n


def saved_name_for(value: str) -> str | None:
    """The saved-place name a trip input refers to, if it looks like one. 'home' → 'home'."""
    v = value.strip().lower()
    if v in ALIASES:
        return ALIASES[v]
    if v.startswith("my "):
        v = v[3:]
    v = re.sub(r"\s+", "_", v)
    return v if NAME_RE.match(v) else None


# ---- tool logic ----------------------------------------------------------------


async def save_place(store: PlaceStore, client: GoogleClient, user_id: str, name: str, address: str) -> dict:
    key = clean_name(name)
    places = await store.get(user_id)
    if key not in places and len(places) >= MAX_PLACES:
        raise ToolError(f"You can save up to {MAX_PLACES} places. Forget one first with forget_place.")
    try:
        found = await client.geocode(address)
    except GoogleAPIError as e:
        raise ToolError(str(e))
    place = {"address": found.address, "lat": found.lat, "lng": found.lng, "saved_at": int(time.time())}
    await store.put(user_id, key, place)
    return {
        "saved": key,
        "address": found.address,
        "replaced": key in places,
        "use_it_as": f"Say '{key}' as a place in any trip, e.g. origin='{key}'.",
    }


async def my_places(store: PlaceStore, user_id: str) -> dict:
    places = await store.get(user_id)
    return {
        "places": [{"name": k, "address": v["address"]} for k, v in sorted(places.items())],
        "note": None if places else "No saved places yet. Use save_place to add home, work or school.",
    }


async def forget_place(store: PlaceStore, user_id: str, name: str) -> dict:
    key = clean_name(name)
    if not await store.delete(user_id, key):
        raise ToolError(f"No saved place called '{key}'. Use my_places to see what's saved.")
    return {"forgotten": key}


async def resolve(store: PlaceStore | None, user_id: str | None, value: str, cache: dict) -> str:
    """Turn 'home' (or any saved name) into its address. Everything else passes through."""
    name = saved_name_for(value)
    if name is None or store is None:
        return value
    if user_id is None:
        if value.strip().lower() in ALIASES:
            raise ToolError(
                f"'{value}' is a saved place, and saved places need sign-in. Call my_places to sign in, "
                "then try again. Or ask the user for the address."
            )
        return value
    if "places" not in cache:
        cache["places"] = await store.get(user_id)
    place = cache["places"].get(name)
    if place:
        return place["address"]
    if value.strip().lower() in ALIASES:
        raise ToolError(f"No '{name}' saved yet. Ask the user for the address, then save it with save_place.")
    return value
