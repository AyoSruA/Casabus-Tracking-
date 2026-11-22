from datetime import datetime, timedelta
import json
import os
import secrets
import threading
from zoneinfo import ZoneInfo

from cachetools import TTLCache
from flask import Flask, Response, g, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import requests

from lines import (KNOWN_REFS, LINES, STATION_BY_NO, TIMETABLES,
                   estimate_leg, straight_line_meters)

# Saved leg times from build_paths.py. Lines without them are estimated from
# coordinates until a refresh calls HERE.
try:
  from line_paths import LINE_PATHS
except ImportError:
  LINE_PATHS = {}

app = Flask(__name__)
# 160/day keeps HERE safely under 5000/month.
limiter = Limiter(get_remote_address, app=app, default_limits=["160 per day", "10 per hour"],
                  storage_uri="memory://")
cache = TTLCache(maxsize=100, ttl=90)  # HERE times. Not thread-safe: use cache_lock.
cache_lock = threading.Lock()
state_memory = {}  # (line, direction, station_no) -> last refreshed answer
# Last stop refreshed on any device. A browser with no saved choice opens it.
last_fetched = {}

# Shown on a first visit; afterwards the page remembers each visitor's choice.
# Default line: DEFAULT_LINE if it has stops, else the first line with stops
# and a timetable.
DEFAULT_LINE = next((r for r in (os.getenv("DEFAULT_LINE"), *KNOWN_REFS) if r in LINES and TIMETABLES.get(r)),
                    next(iter(LINES)))
DEFAULT_DIRECTION = os.getenv("DEFAULT_DIRECTION", "A")
DEFAULT_STOP = os.getenv("DEFAULT_STOP", "")  # stop name; empty = first stop after the terminus
# Lines without a timetable get a bus every DEFAULT_HEADWAY_MIN, flagged as
# estimated. At least 1: 0 would loop forever.
DEFAULT_HEADWAY_MIN = max(1, int(os.getenv("DEFAULT_HEADWAY_MIN", "15")))
SERVICE_START = os.getenv("SERVICE_START", "06:00")
SERVICE_END = os.getenv("SERVICE_END", "22:00")
WEEKEND_DAYS = {5, 6}  # Monday = 0
# Timetables are Casablanca time; the container clock may be UTC.
LOCAL_TZ = ZoneInfo(os.getenv("TZ_NAME", "Africa/Casablanca"))

HERE_API_KEY = os.getenv("HERE_API_KEY", "")
HERE_DAILY_BUDGET = int(os.getenv("HERE_DAILY_BUDGET", "160"))  # max real HERE requests per day
here_budget = {"day": None, "count": 0}
here_budget_lock = threading.Lock()
# Waypoints between terminus and stop. Stop coordinates can be approximate, so
# waypoints can sit off the road and force detours. Keep 0 until they are exact.
MAX_VIA = int(os.getenv("HERE_MAX_VIA", "0"))
MAX_DETOUR_RATIO = 2.0  # a route longer than this times the straight line is a detour

PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), encoding="utf-8").read()


def route_stop_numbers(line, direction, station_no):
  """Stops from the departure terminus up to station_no, in order."""
  order = LINES[line][direction]["order"]
  if station_no not in order[1:]:
    raise ValueError("Station not served after the terminus in this direction")
  return order[:order.index(station_no) + 1]


def saved_travel_seconds(line, direction, station_no):
  """Sum of saved leg times. Legs match by stop name, so a path built from
  older stop numbers never hits the wrong stop; missing legs are estimated."""
  route = route_stop_numbers(line, direction, station_no)
  saved = {(l["from_name"], l["to_name"]): l["seconds"]
           for l in LINE_PATHS.get(line, {}).get(direction, {}).get("legs", [])
           if l.get("from_name") and l.get("to_name")}
  st = STATION_BY_NO[line][direction]
  return round(sum(v if (v := saved.get((st[a]["name"], st[b]["name"]))) is not None
                   else estimate_leg(st[a], st[b])[0] for a, b in zip(route, route[1:])))


def here_budget_left(take=False):
  """True if a HERE request is allowed. take=True also counts it, in the same
  step, so two refreshes cannot both pass the last slot."""
  with here_budget_lock:
    if here_budget["day"] != datetime.now().date():
      here_budget.update(day=datetime.now().date(), count=0)
    ok = here_budget["count"] < HERE_DAILY_BUDGET
    here_budget["count"] += int(ok and take)
    return ok


def here_usage():
  here_budget_left()  # rolls the counter over at midnight
  used = here_budget["count"]
  return {"used": used, "limit": HERE_DAILY_BUDGET, "left": max(HERE_DAILY_BUDGET - used, 0)}


def pick_via_stops(stops):
  """Few evenly spaced middle stops: every waypoint can force a detour."""
  middle = stops[1:-1]
  if len(middle) <= MAX_VIA:
    return middle
  step = len(middle) / (MAX_VIA + 1)
  return [middle[round(step * (i + 1)) - 1] for i in range(MAX_VIA)]


def fetch_here_travel_time(line, direction, station_no, live=False):
  """Terminus to stop, in seconds. HERE only when live (explicit refresh)."""
  key = (line, direction, station_no)
  with cache_lock:
    hit = cache.get(key)
  if hit is not None:
    return hit
  if live and HERE_API_KEY and here_budget_left(take=True):
    stops = [STATION_BY_NO[line][direction][n] for n in route_stop_numbers(line, direction, station_no)]

    def ll(s):
      return f"{s['lat']},{s['lng']}"
    params = [("transportMode", "car"), ("origin", ll(stops[0])), ("destination", ll(stops[-1])),
              ("return", "summary"), ("apiKey", HERE_API_KEY)]
    params += [("via", ll(s) + "!passThrough=true") for s in pick_via_stops(stops)]
    try:
      data = requests.get("https://router.hereapi.com/v8/routes", params=params, timeout=10).json()
      if data.get("routes"):
        sections = data["routes"][0]["sections"]
        seconds = sum(s["summary"].get("duration", 0) for s in sections)
        meters = sum(s["summary"].get("length", 0) for s in sections)
        straight = straight_line_meters(stops[0], stops[-1])
        print(f"[HERE] {line}/{direction} -> stop {station_no}: {seconds}s, {meters}m, straight {straight:.0f}m")
        if straight and meters > MAX_DETOUR_RATIO * straight:
          print("[HERE Warning] Route is a detour. Using saved path time instead.")
        else:
          with cache_lock:
            cache[key] = seconds
          return seconds
      else:
        print(f"[HERE API Warning] No route: {data.get('title') or data}")
    except Exception as e:
      # Type only: the error text holds the URL, and the URL holds the key.
      print(f"[HERE API Warning] Could not fetch live travel time: {type(e).__name__}")
  if live and not HERE_API_KEY:
    print("[HERE Warning] HERE_API_KEY is empty. Using saved path time.")
  elif live and not here_budget_left():
    print("[HERE Warning] Daily budget used up. Using saved path time.")
  return saved_travel_seconds(line, direction, station_no)


def day_type(day):
  return "weekend" if day.weekday() in WEEKEND_DAYS else "weekday"


def timetable_for(line, direction, day):
  """(departures, estimated) for one date. A missing timetable becomes a bus
  every DEFAULT_HEADWAY_MIN, flagged estimated."""
  known = TIMETABLES.get(line, {}).get(direction, {}).get(day_type(day))
  if known:
    return known, False
  start, end = ((int(h) * 60 + int(m)) for h, m in (t.split(":") for t in (SERVICE_START, SERVICE_END)))
  return [f"{m // 60:02d}:{m % 60:02d}" for m in range(start, end + 1, DEFAULT_HEADWAY_MIN)], True


def calculate_bus_schedule_data(line, direction, station_no, live=False):
  """Nearest bus = earliest ETA still in the future, plus its neighbours."""
  now = datetime.now(LOCAL_TZ).replace(tzinfo=None)
  travel_seconds = fetch_here_travel_time(line, direction, station_no, live)
  travel = timedelta(seconds=travel_seconds)
  # Yesterday + today + tomorrow, each day with its own timetable.
  departures, estimated = [], []
  for offset in (-1, 0, 1):
    date = now.date() + timedelta(days=offset)
    times, est = timetable_for(line, direction, date)
    departures += [datetime.strptime(f"{date:%Y-%m-%d} {t}", "%Y-%m-%d %H:%M") for t in times]
    estimated += [est] * len(times)
  i = next((i for i, dep in enumerate(departures) if dep + travel >= now), len(departures) - 1)
  i = min(max(i, 1), len(departures) - 2)  # keep a previous and a next bus
  timetable_estimated = any(estimated[i - 1:i + 2])

  def bus(dep):
    arr = dep + travel
    return {"scheduled_departure": dep.strftime("%Y-%m-%d %H:%M:%S"), "dep_time_only": dep.strftime("%H:%M"),
            "estimated_arrival": arr.strftime("%Y-%m-%d %H:%M:%S"), "eta_time_only": arr.strftime("%H:%M:%S"),
            "waiting_time_minutes": round((dep - now).total_seconds() / 60),
            "arrives_in_minutes": round((arr - now).total_seconds() / 60),
            "travel_time_minutes": round(travel_seconds / 60)}

  d, st = LINES[line][direction], STATION_BY_NO[line][direction]
  return {"line": line, "operator": "Casabus (Alsa)", "direction": direction, "direction_label": d["label"],
          "origin": st[d["order"][0]]["name"], "station_no": station_no,
          "destination_stop": st[station_no]["name"], "day_type": day_type(now.date()),
          "timetable_estimated": timetable_estimated,
          "headway_minutes": DEFAULT_HEADWAY_MIN if timetable_estimated else None,
          "previous_bus": bus(departures[i - 1]), "nearest_bus": bus(departures[i]),
          "next_bus": bus(departures[i + 1]), "query_timestamp": now.strftime("%Y-%m-%d %H:%M:%S")}


class BadSelection(Exception):
  pass


@app.errorhandler(BadSelection)
def bad_selection(e):
  return jsonify({"error": str(e)}), 400


def no_stops(ref):
  return f"Line {ref} has no stops yet. Add them to manual_lines.py."


def parse_selection():
  """?line=&direction=&station= -> (line, direction, station_no)."""
  ref = request.args.get("line", DEFAULT_LINE)
  if ref not in LINES:
    raise BadSelection(no_stops(ref) if ref in KNOWN_REFS else "Unknown line")
  direction = request.args.get("direction", DEFAULT_DIRECTION)
  if direction not in LINES[ref]:
    raise BadSelection("Unknown direction")
  try:
    stops = LINES[ref][direction]["stations"]
    default = next((s["no"] for s in stops[1:] if s["name"] == DEFAULT_STOP), stops[1]["no"])
    station_no = int(request.args.get("station", default))
    route_stop_numbers(ref, direction, station_no)
  except (ValueError, IndexError):
    raise BadSelection("Invalid station for this direction")
  return ref, direction, station_no


@app.before_request
def block_cross_site():
  """Refuses API calls that browsers mark as coming from another website.
  POST needs X-Casabus: another site cannot add it without a CORS grant this
  app never gives, so a hostile page cannot spend the HERE budget."""
  if request.path.startswith("/api/"):
    site = request.headers.get("Sec-Fetch-Site")
    if site and site not in ("same-origin", "none"):
      return jsonify({"error": "Cross-site request refused"}), 403
    if request.method == "POST" and request.headers.get("X-Casabus") != "1":
      return jsonify({"error": "Missing request header"}), 403


@app.after_request
def security_headers(response):
  response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                           "Referrer-Policy": "same-origin"})
  if g.get("csp_nonce"):
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; script-src 'nonce-{g.csp_nonce}'; "
        "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
  if request.path.startswith("/api/"):
    response.headers["Cache-Control"] = "no-store"
  return response


@app.route("/")
@limiter.exempt
def home_dashboard():
  """Plain string replacement, no template engine: nothing in the page is
  ever run as template code."""
  g.csp_nonce = secrets.token_urlsafe(16)
  js = lambda v: json.dumps(v).replace("<", "\\u003c")  # safe inside <script>
  html = (PAGE.replace("__LINE__", js(DEFAULT_LINE)).replace("__DIR__", js(DEFAULT_DIRECTION))
          .replace("__STOP__", js(DEFAULT_STOP)).replace("__LAST__", js(last_fetched or None))
          .replace("<script>", f'<script nonce="{g.csp_nonce}">', 1))
  return Response(html, mimetype="text/html")


@app.route("/api/lines")
@limiter.exempt
def get_lines():
  return jsonify({"lines": KNOWN_REFS})


@app.route("/api/line")
@limiter.exempt
def get_line():
  """Both directions of one line, from manual_lines.py. Never calls HERE."""
  ref = request.args.get("line", DEFAULT_LINE)
  if ref not in LINES:
    return jsonify({"line": ref, "error": no_stops(ref) if ref in KNOWN_REFS else "Unknown line"}), \
        404 if ref in KNOWN_REFS else 400
  return jsonify({"line": ref, "directions": [
      # Terminus left out: buses leave from there.
      {"key": k, "label": d["label"], "stop_count": len(d["stations"]),
       "stations": [{"no": s["no"], "name": s["name"]} for s in d["stations"][1:]]}
      for k, d in LINES[ref].items()]})


@app.route("/api/usage")
@limiter.exempt
def get_usage():
  return jsonify(here_usage())


@app.route("/api/state")
@limiter.exempt
def get_memory_state():
  """Last refreshed answer, else saved path times. Never calls HERE."""
  sel = parse_selection()
  return jsonify(state_memory.get(sel) or calculate_bus_schedule_data(*sel))


@app.route("/api/refresh", methods=["POST"])
@limiter.limit("160/day;10/hour")
def refresh_memory_state():
  """The only route that calls HERE (capped by HERE_DAILY_BUDGET)."""
  sel = parse_selection()
  with cache_lock:
    cache.pop(sel, None)
  state_memory[sel] = data = calculate_bus_schedule_data(*sel, live=True)
  last_fetched.update(line=sel[0], direction=sel[1], stop=data["destination_stop"])
  return jsonify({**data, "usage": here_usage()})


@app.errorhandler(429)
def ratelimit_handler(e):
  return jsonify({"error": "Monthly quota limit protection triggered",
                  "message": "Too many requests. Limit enforced to stay under 5,000/month."}), 429


if __name__ == "__main__":
  # On a phone, run with HOST=127.0.0.1 so only the phone can reach the app.
  host, port = os.getenv("HOST", "0.0.0.0"), int(os.getenv("PORT", "5000"))
  try:
    # Fixed worker threads: a flood of slow requests queues instead of
    # creating unlimited threads.
    from waitress import serve
  except ImportError:
    serve = None
  if serve:
    serve(app, host=host, port=port, threads=int(os.getenv("THREADS", "8")))
  else:
    app.run(host=host, port=port, threaded=True)
