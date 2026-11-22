# Fin Tobis

Next-bus ETA for Casablanca Casabus lines. Pick a line, a direction and your stop, and it tells you when the next bus should arrive. It works on Home Servers and connects using tailscale.

## About this project

Hi! I'm a med student, and this is a side project I built in my free time. I vibe coded it with Claude Opus 5.5, because with my studies that was the only way I could make it happen.

For the same reason, updates may come quite late. A local app for the phone is on the way.

If you run into a problem, feel free to contact me by opening an issue on this repository. I'll answer, no problem.

## How it works

- Stops and coordinates of each line come from OpenStreetMap (`stops_data.py`).
- Travel time from the terminus to your stop is estimated from the distance between stops until you press Refresh.
- Refresh asks the HERE routing API for a live travel time. It is the only action that uses HERE, and it is capped per day.
- The next bus is the first scheduled departure that has not yet reached your stop, plus the one before and the one after.
- If a line has no timetable, a bus every 15 minutes is assumed and marked as estimated.
- Your browser remembers your last line, direction and stop.

## Missing for now

- No stops yet, so no ETA: lines 1, 52, 302, 308, 511.
- Stops but no timetable, so a bus every 15 minutes is assumed: lines BW1, BW2, 40, 79, 97B, 169, and line 109 direction B.

## Run

    pip install -r requirements.txt
    HERE_API_KEY=your_key python app.py

The key is optional. Without it, times are estimated only.

## Deploy

`docker-compose.yml` runs the app with a Tailscale sidecar. It ships with empty `HERE_API_KEY=` and `TS_AUTHKEY=`. Fill them in on your server only, and never commit them.

## Data credit

Stops and coordinates: (c) OpenStreetMap contributors, ODbL. https://www.openstreetmap.org/copyright
