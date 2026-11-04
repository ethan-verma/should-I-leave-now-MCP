"""Run every tool once against the real Google APIs to check your key works.

    uv run python scripts/smoke_test.py

Reads GOOGLE_MAPS_API_KEY from .env. Makes roughly 20 Google API calls (a few cents at most).
"""

import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from leavenow import tools
from leavenow.google import GoogleClient

ORIGIN = "2600 Durant Ave, Berkeley, CA"
DEST = "Dwinelle Hall, UC Berkeley, Berkeley, CA"
LAB = "Soda Hall, UC Berkeley, Berkeley, CA"


def load_key() -> str:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("GOOGLE_MAPS_API_KEY="):
                os.environ.setdefault("GOOGLE_MAPS_API_KEY", line.split("=", 1)[1].strip())
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
    if not key or key == "your-key-here":
        sys.exit("No key yet. Paste your key into .env after GOOGLE_MAPS_API_KEY= and run again.")
    return key


async def main() -> None:
    client = GoogleClient(load_key())
    day = (date.today() + timedelta(days=1)).isoformat()
    checks = {
        "when_to_leave": tools.when_to_leave(client, ORIGIN, DEST, f"{day}T09:00"),
        "compare_ways_to_get_there": tools.compare_ways_to_get_there(client, ORIGIN, DEST),
        "weather_along_the_way": tools.weather_along_the_way(client, ORIGIN, DEST),
        "plan_my_day": tools.plan_my_day(
            client,
            ORIGIN,
            [
                tools.Stop(place=DEST, arrive_by=f"{day}T09:00", stay_minutes=50, label="Lecture"),
                tools.Stop(place=LAB, arrive_by=f"{day}T14:00", stay_minutes=120, label="Lab"),
            ],
            fill_gaps_with="quiet cafe",
        ),
    }
    failed = 0
    for name, call in checks.items():
        print(f"\n=== {name} ===")
        try:
            print(json.dumps(await call, indent=2, ensure_ascii=False))
        except ToolError as e:
            failed += 1
            print(f"FAILED: {e}")
    await client.aclose()
    print(f"\n{len(checks) - failed}/{len(checks)} tools worked.")
    if failed:
        print("A 403 'API has not been used' or 'not enabled' error means that API isn't turned on yet.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
