# Privacy Policy: Should I Leave Now?

Effective November 17, 2026

Should I Leave Now? is an MCP connector that answers trip-timing questions (when to leave, which train to catch, the weather on the way) using live Google Maps Platform data. This policy explains what data the connector handles when an AI assistant such as Claude calls its tools.

## What we collect

Using the trip tools needs no account. Signing in is optional and only needed to save places. The connector receives what the assistant sends in each tool call:

- **Places**: the addresses or place names given as a trip's start, stops and destination.
- **Times**: when you want to leave or arrive, and how long you plan to stay at each stop.
- **Trip options**: travel mode, buffer minutes and similar settings.
- **A short purpose note**: the assistant may include a brief description of what the call is for (for example "find when to leave for a 2pm meeting"). It is used only to improve the connector.
- **Technical data**: your IP address, the time of the request, and the name of the assistant app making it.

If you sign in (to save places):

- **Account**: your WorkOS user ID and email address, from the sign-in.
- **Saved places**: the names you choose (like "home" or "work") and their addresses and map coordinates.

We do not read your conversation, your assistant's memory, your chat history or your files.

## How we use it

- To answer the request: places and times are sent to Google Maps Platform (Routes, Geocoding, Time Zone, Places, Weather and Air Quality APIs) to compute routes, travel times, forecasts and nearby places.
- To keep the service running: per-IP rate limiting and short-lived caching of identical requests.
- To remember your saved places, so "home" or "work" works in any trip.
- To improve the connector: we review which tools are called, with what inputs and results, and where calls fail.

We do not sell your data, use it for advertising, or build profiles of individuals.

## Who we share it with

| Service | What it receives | Why | Its policy |
| --- | --- | --- | --- |
| Google Maps Platform | Places, times and travel options in each request | To compute the answer | [Google Privacy Policy](https://policies.google.com/privacy) |
| Baton by Good Timing | Tool names, inputs, results, errors, the purpose note and the assistant app's name. Your email only if you are one of our named testers. | Usage analytics to improve the connector | [Good Timing Privacy Policy](https://goodtiming.ai/privacy) |
| Render | Standard request logs (IP address, time, path) | Hosting the server | [Render Privacy Policy](https://render.com/privacy) |
| WorkOS | Your sign-in, if you sign in | Signing you in | [WorkOS Privacy Policy](https://workos.com/privacy) |
| Google Cloud Firestore | Your user ID and saved places, if you save any | Storing saved places | [Google Privacy Policy](https://policies.google.com/privacy) |

We share data with no one else, except where required by law.

## How long we keep it

- **On our server:** identical requests are cached in memory for up to 24 hours (place lookups) or a few minutes (routes, weather), then discarded. Our own logs record only which tool was called.
- **Saved places:** kept until you remove them with forget_place, or until you ask us to delete your account data.
- **Baton analytics:** kept according to Good Timing's privacy policy.
- **Hosting logs:** kept according to Render's privacy policy.

## Your choices

- Use place names or cross streets instead of exact home addresses if you prefer.
- See your saved places with my_places and delete any of them with forget_place.
- Remove the connector from your assistant at any time to stop all data flow.
- To ask about or request deletion of analytics data, contact us below.

## Children

The connector is not directed at children under 13 and does not knowingly collect their data.

## Changes

We will update this page when our practices change, and revise the effective date above.

## Contact

Open an issue at [github.com/ethan-verma/should-I-leave-now-MCP/issues](https://github.com/ethan-verma/should-I-leave-now-MCP/issues).
