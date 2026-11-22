"""All lines in one place.

LINES[ref][dir] = {"label", "stations", "order"}; STATION_BY_NO[ref][dir][no].
TIMETABLES[ref][dir]["weekday"|"weekend"] = ["HH:MM", ...] (missing = estimated).
KNOWN_REFS: every line in the picker (lines with stops or a timetable).
Stops and coordinates come from stops_data.py (an OpenStreetMap export) and
manual_lines.py, which wins for the same line. A line without stops is listed
but cannot be calculated.
"""

import difflib
from math import asin, cos, radians, sin, sqrt
import os
import re
import unicodedata

from manual_lines import MANUAL_LINES, MANUAL_TIMETABLES
from stops_data import STOPS
# Required: without it the app would offer only the manual lines.
from timetables_data import TIMETABLE_SOURCES

MANUAL_REFS = set(MANUAL_LINES)
AVG_BUS_KMH = float(os.getenv("AVG_BUS_KMH", "16"))
ROAD_FACTOR = 1.3  # road distance vs straight line
DWELL_SECONDS = 20  # time lost at each stop

LINES, STATION_BY_NO, TIMETABLES = {}, {}, {}


def norm_ref(ref):
  """'L022', '022' and '22' are the same line. '9E' stays '9E'."""
  ref = ref.strip().upper()
  if ref[:1] == "L" and ref[1:2].isdigit():
    ref = ref[1:]
  return ref.lstrip("0") or "0"


_SOURCES = {}
# Manual timetables first: for the same direction and day, the first one wins.
for _e in MANUAL_TIMETABLES + TIMETABLE_SOURCES:
  _SOURCES.setdefault(norm_ref(_e["line"]), []).append(_e)


def straight_line_meters(a, b):
  lat1, lng1, lat2, lng2 = map(radians, (a["lat"], a["lng"], b["lat"], b["lng"]))
  h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lng2 - lng1) / 2) ** 2
  return 2 * 6371000 * asin(sqrt(h))


def estimate_leg(a, b):
  """(seconds, meters) of a bus between two stops, from coordinates."""
  meters = straight_line_meters(a, b) * ROAD_FACTOR
  return meters / (AVG_BUS_KMH / 3.6) + DWELL_SECONDS, meters


def _norm(name):
  name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
  name = re.sub(r"\bterminus\b", "", re.sub(r"\bouad\b", "oued", re.sub(r"[^a-z0-9 ]", " ", name.lower())))
  return " ".join(name.split())


def _similar(a, b):
  a, b = _norm(a), _norm(b)
  if not a or not b:
    return 0.0
  return 0.95 if a in b or b in a else difflib.SequenceMatcher(None, a, b).ratio()


def _near(name, stops):
  """Best match among a terminus and its neighbours: timetables and maps
  often name the terminus after a nearby stop."""
  return max(_similar(name, s["name"]) for s in stops)


def _match_timetables(ref, dirs):
  """Timetables are keyed by terminus name, not by A/B, so
  each one goes to the direction whose end stops match best."""
  result = {}
  for e in _SOURCES.get(ref, []):
    scores = sorted((((_near(e["origin"], d["stations"][:3]) + _near(e["destination"], d["stations"][-3:])) / 2, k)
                     for k, d in dirs.items()), key=lambda t: -t[0])
    score, key = scores[0]
    margin = score - scores[1][0] if len(scores) > 1 else 1
    # A clear match, or a weaker one that still clearly beats the other direction.
    if score >= 0.5 or (score >= 0.4 and margin >= 0.1):
      result.setdefault(key, {}).setdefault(e["day"], e["times"])
  return result


def _put(ref, dirs):
  dirs = {k: {**d, "order": [s["no"] for s in d["stations"]]} for k, d in dirs.items()}
  STATION_BY_NO[ref] = {k: {s["no"]: s for s in d["stations"]} for k, d in dirs.items()}
  TIMETABLES[ref] = _match_timetables(ref, dirs)
  LINES[ref] = dirs


for _ref, _dirs in {**STOPS, **MANUAL_LINES}.items():
  _put(norm_ref(_ref), _dirs)

KNOWN_REFS = sorted(set(LINES) | set(_SOURCES), key=lambda r: (int("".join(filter(str.isdigit, r)) or 10**9), r))
