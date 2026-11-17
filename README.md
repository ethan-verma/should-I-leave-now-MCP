# Should I Leave Now?

An MCP server that tells Claude **when you need to leave** to get somewhere on time, using live Google transit schedules, traffic, weather and air quality.

> "I have a midterm in Dwinelle at 9. When do I need to leave my apartment?"
> → *Leave by 8:30 AM. Walk 5 min to Telegraph & Durant, take the 6 toward Downtown Berkeley at 8:40, arrive 8:55.*

## Tools

| Tool | What it answers |
| --- | --- |
| `when_to_leave` | The latest time to leave to arrive by a given time (transit, walk, bike or drive), with a safety buffer |
| `compare_ways_to_get_there` | Walk vs bike vs transit vs drive for one trip, fastest first |
| `weather_along_the_way` | Rain, temperature, wind and air quality at both ends of the trip |
| `plan_my_day` | A schedule for several stops: when to leave for each, free time between them, nearby spots to fill it, and warnings for stops you can't make on time |
| `save_place`, `my_places`, `forget_place` | Optional sign-in: save home, work or any place, then say "home" in any trip |
| `transit_options` | Several train or bus departures for one trip ("which Caltrain should I catch?"), each with door-to-door directions |

## Run it locally

1. Create a Google Cloud project and enable: **Routes API, Geocoding API, Time Zone API, Weather API, Air Quality API, Places API (New)**.
2. Create an API key (APIs & Services → Credentials). Restrict it to those six APIs.
3. Start the server:

```bash
cp .env.example .env   # then paste your key into .env
set -a; source .env; set +a
uv run leavenow
```

The server listens at `http://localhost:8080/mcp`.

To try it in Claude Code:

```bash
claude mcp add --transport http leave-now http://localhost:8080/mcp
```

## Tests

```bash
uv run pytest
```

Tests use a fake Google, so they need no key and cost nothing.

## Deploy to Render

1. Push this repo to GitHub.
2. In [Render](https://dashboard.render.com), click **New → Blueprint** and pick the repo. Render reads `render.yaml`.
3. When asked, paste your key into `GOOGLE_MAPS_API_KEY`. Never commit it.
4. When the deploy finishes, your MCP endpoint is `https://<your-service>.onrender.com/mcp`.

The free plan sleeps after 15 minutes idle, and the first request after that takes about a minute. Use the Starter plan for real users.

## Add it to Claude

In Claude, open **Settings → Connectors → Add custom connector**, name it "Should I Leave Now?", and paste the `/mcp` URL.

## Deploy to Cloud Run (alternative)

Install the [gcloud CLI](https://cloud.google.com/sdk/docs/install), then:

```bash
gcloud run deploy leave-now --source . --region us-west1 --allow-unauthenticated \
  --set-env-vars GOOGLE_MAPS_API_KEY=YOUR_KEY --max-instances 3
```

The command prints a URL. Your public MCP endpoint is that URL plus `/mcp`. People add it in Claude under **Settings → Connectors → Add custom connector**.

For production, store the key in Secret Manager (`--set-secrets GOOGLE_MAPS_API_KEY=maps-key:latest`) instead of a plain env var.

## Keeping costs down

- Identical requests are cached in memory (routes 2 min, weather, air and place searches 10 min, addresses 24 h).
- Each client IP gets 60 requests per minute (`RATE_LIMIT_PER_MINUTE`).
- `--max-instances 3` caps how far Cloud Run scales.
- Set a budget alert in Google Cloud Billing, and per-API quotas under APIs & Services → Quotas.

## Privacy

Places and times are sent to Google Maps Platform to compute each answer. Tool calls, including inputs and results, are recorded by [Baton](https://goodtiming.ai) analytics to improve the connector. Signing in is optional; it stores your WorkOS user ID, email and saved places in Firestore. See the full [privacy policy](PRIVACY.md).

## Support

Open an issue at [github.com/ethan-verma/should-I-leave-now-MCP/issues](https://github.com/ethan-verma/should-I-leave-now-MCP/issues).

## Roadmap

- Usage and friction reporting (from Grubcheck's annotation SDK)
- Listings in the MCP registry, Smithery and mcp.so
