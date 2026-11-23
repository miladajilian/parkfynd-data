#!/usr/bin/env python3
"""Build Malmö's parking data in the same shape as the Stockholm city snapshot (national-data-spec.md §8).

    TRAFIKVERKET_API_KEY=… python3 docs/tools/build-malmo.py Parkfynd/Resources/CitySnapshot

For test fixtures, build from local files instead (no network, no count check):

    python3 docs/tools/build-malmo.py OUT --from-files FEES.geojson CLEANING.geojson NAMES.json --fetched-at 2026-11-23T12:00:00Z

Inputs (all open data):
- Malmö stad `parkeringsavgifter` (CC0): one line per stretch with a fee, `taxa` text.
- Malmö stad `miljoparkering` (CC0): monthly street-cleaning bans, `day` + `tid`.
- Trafikverket NVDB `Gatunamn` (CC0, API Öppna Data, key from Config/Secrets.xcconfig): street names.

Output, LTF-Tolken-shaped GeoJSON so the app's decoder, index and engines work unchanged:

    malmo-ptillaten.json.deflate     one feature per fee line (FID, STREET_NAME, PARKING_RATE, VEHICLE, …)
    malmo-servicedagar.json.deflate  one feature per matched cleaning window, same geometry as its fee line
    malmo-snapshot-meta.json         {"fetchedAt", "ptillaten", "servicedagar"}

Parkfynd extension keys (not in LTF-Tolken, decoded by ParkfyndCore):
- `PF_MONTH_DAY` on servicedagar: the ban is on this day of every month (1–31) instead of a weekday.
- `PF_CLEANING = "unknown"` on ptillaten: a cleaning line lies within 20 m but doesn't match, or its
  rule text isn't a plain monthly ban. Legality is then unknown ("Check the sign").
"""

import argparse
import collections
import datetime
import importlib.util
import json
import math
import os
import re
import tempfile
import urllib.request
import zlib

FEES_URL = ("https://opendata-api.malmo.se/dataset/c06939a6-65b4-4078-9cb5-b49aa48b9140/resource/"
            "1a6bd68b-30ca-40a5-9d62-01e2a566982e/download/parkeringsavgifter.geojson")
CLEANING_URL = ("https://opendata-api.malmo.se/dataset/c3f1cdfd-fd67-4b3f-ba96-d97c00339b6a/resource/"
                "73490f00-0d71-4b17-903c-f77ab7664a53/download/miljoparkeringar.geojson")
# 2026-11-23: 3,397 fee lines and 2,275 cleaning lines. Refuse anything far off.
EXPECTED = {"fees": 3_397, "cleaning": 2_275}
TOLERANCE = 0.10
# Malmö municipality in SWEREF 99 TM, generous.
NAMES_BOX = (360_000, 6_150_000, 390_000, 6_170_000)

MATCH_METERS = 3.0       # a fee vertex this close to a cleaning line counts as "on" it
MATCH_SHARE = 0.5        # this share of fee vertices must be on one cleaning line to match it
NEARBY_METERS = 20.0     # an unmatched cleaning line this close makes cleaning unknown
NAME_METERS = 15.0       # median vertex distance to a named street must be within this
MONTHLY = re.compile(r"(den \d+:[ea] i månaden|\d+:[ea] dagen i månaden|den \d+:[ea] i månaden|\d+:[ea] i månaden)")
TID = re.compile(r"^\s*(\d{4})\s*-\s*(\d{4})\s*$")

LAT0 = math.radians(55.6)
M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON = 111_320.0 * math.cos(LAT0)


def xy(c):
    return (c[0] * M_PER_DEG_LON, c[1] * M_PER_DEG_LAT)


def point_segment(p, a, b):
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class SegmentIndex:
    """Line segments in a 50 m grid, for nearest-line lookups in metres."""
    CELL = 50.0

    def __init__(self):
        self.cells = collections.defaultdict(list)

    def add(self, owner, coords):
        pts = [xy(c) for c in coords]
        for a, b in zip(pts, pts[1:]):
            x0, x1 = sorted((a[0], b[0]))
            y0, y1 = sorted((a[1], b[1]))
            for i in range(int(x0 // self.CELL), int(x1 // self.CELL) + 1):
                for j in range(int(y0 // self.CELL), int(y1 // self.CELL) + 1):
                    self.cells[(i, j)].append((owner, a, b))

    def distances(self, point, radius):
        """{owner: shortest distance} for every line within `radius` metres of `point`."""
        p = xy(point)
        reach = int(radius // self.CELL) + 1
        ci, cj = int(p[0] // self.CELL), int(p[1] // self.CELL)
        best = {}
        for i in range(ci - reach, ci + reach + 1):
            for j in range(cj - reach, cj + reach + 1):
                for owner, a, b in self.cells.get((i, j), ()):
                    d = point_segment(p, a, b)
                    if d <= radius and d < best.get(owner, math.inf):
                        best[owner] = d
        return best


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Parkfynd-malmo-tool"})
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def fetch_names():
    """NVDB Gatunamn for Malmö: [(name, [[lon, lat], …]), …]."""
    spec = importlib.util.spec_from_file_location("nvdb_probe", os.path.join(os.path.dirname(__file__), "nvdb-probe.py"))
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    probe.BOX = NAMES_BOX
    key = probe.api_key()
    records, skip = [], 0
    while True:
        page = probe.query(key, "Gatunamn", skip)
        records += page
        if len(page) < probe.PAGE:
            break
        skip += probe.PAGE
    names = []
    for record in records:
        wkt = (record.get("Geometry") or {}).get("WKT-WGS84-3D", "")
        name = record.get("Namn")
        if not name or not wkt.startswith("LINESTRING"):
            continue
        coords = [[float(v) for v in pair.split()[:2]] for pair in wkt[wkt.index("(") + 1:wkt.rindex(")")].split(",")]
        names.append((name, coords))
    return names


def cleaning_window(props):
    """(day, startHHmm, endHHmm) for a plain monthly ban, else None."""
    text = re.sub(r"\s+", " ", props.get("copy_value") or "")
    tid = TID.match(props.get("tid") or "")
    day = str(props.get("day") or "")
    if not MONTHLY.search(text) or not tid or not day.isdigit() or not 1 <= int(day) <= 31:
        return None
    return int(day), int(tid.group(1)), int(tid.group(2))


def street_name(coords, names_index, names):
    votes = collections.defaultdict(list)
    for c in coords:
        for owner, d in names_index.distances(c, NAME_METERS).items():
            votes[names[owner][0]].append(d)
    if not votes:
        return None
    # The name that is near the most vertices, then the closest one.
    name, dists = max(votes.items(), key=lambda kv: (len(kv[1]), -sorted(kv[1])[len(kv[1]) // 2]))
    return name if len(dists) * 2 >= len(coords) else None


def build(fees, cleaning, names):
    names_index = SegmentIndex()
    for i, (_, coords) in enumerate(names):
        names_index.add(i, coords)
    clean_index = SegmentIndex()
    for i, feature in enumerate(cleaning):
        clean_index.add(i, feature["geometry"]["coordinates"])

    ptillaten, servicedagar = [], []
    stats = collections.Counter()
    for feature in fees:
        p = feature["properties"]
        coords = feature["geometry"]["coordinates"]
        props = {
            "FID": int(p["gid"]),
            "VEHICLE": "fordon",
            "PARKING_RATE": p.get("taxa") or "",
            "CITY_DISTRICT": "Malmö",
            "STREET_NAME": street_name(coords, names_index, names),
        }
        stats["named" if props["STREET_NAME"] else "unnamed"] += 1

        on = collections.Counter()   # cleaning line → fee vertices on it
        near = set()
        for c in coords:
            for owner, d in clean_index.distances(c, NEARBY_METERS).items():
                near.add(owner)
                if d <= MATCH_METERS:
                    on[owner] += 1
        matched = [owner for owner, n in on.items() if n >= MATCH_SHARE * len(coords)]
        windows = [cleaning_window(cleaning[i]["properties"]) for i in matched]
        if matched and all(windows):
            for (day, start, end), owner in zip(windows, matched):
                servicedagar.append({"type": "Feature", "geometry": feature["geometry"], "properties": {
                    "FID": int(cleaning[owner]["properties"]["gid"]),
                    "PF_MONTH_DAY": day, "START_TIME": start, "END_TIME": end,
                    "OTHER_INFO": re.sub(r"\s+", " ", cleaning[owner]["properties"].get("copy_value") or "").strip(),
                }})
            props["OTHER_INFO"] = " ".join(sorted({s["properties"]["OTHER_INFO"] for s in servicedagar[-len(matched):]}))
            stats["cleaning matched"] += 1
        elif matched or near:
            props["PF_CLEANING"] = "unknown"
            stats["cleaning unknown"] += 1
        else:
            stats["no cleaning nearby"] += 1
        ptillaten.append({"type": "Feature", "geometry": feature["geometry"],
                          "properties": {k: v for k, v in props.items() if v is not None}})
    return ptillaten, servicedagar, stats


def check(name, count):
    expected = EXPECTED[name]
    if abs(count - expected) > expected * TOLERANCE:
        raise SystemExit(f"{name}: {count} features, expected about {expected} (±{TOLERANCE:.0%}). "
                         "Look at the data before updating EXPECTED.")


def write(out_dir, ptillaten, servicedagar, fetched_at, prefix="malmo"):
    os.makedirs(out_dir, exist_ok=True)
    files = {}
    for name, features in (("ptillaten", ptillaten), ("servicedagar", servicedagar)):
        raw = json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
        compressor = zlib.compressobj(9, zlib.DEFLATED, -15)  # raw DEFLATE = Apple's .zlib
        files[f"{prefix}-{name}.json.deflate"] = compressor.compress(raw) + compressor.flush()
    meta = {"fetchedAt": fetched_at, "ptillaten": len(ptillaten), "servicedagar": len(servicedagar)}
    files[f"{prefix}-snapshot-meta.json"] = (json.dumps(meta, indent=2) + "\n").encode("utf-8")
    staged = []
    for name, data in files.items():
        fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".tmp-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; the files are committed and bundled
        staged.append((tmp, os.path.join(out_dir, name)))
    for tmp, final in staged:
        os.replace(tmp, final)
    for name, data in files.items():
        print(f"  {name}: {len(data):,} bytes")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out_dir")
    parser.add_argument("--from-files", nargs=3, metavar=("FEES", "CLEANING", "NAMES"))
    parser.add_argument("--fetched-at")
    args = parser.parse_args()
    if args.from_files:
        fees = json.load(open(args.from_files[0], encoding="utf-8"))["features"]
        cleaning = json.load(open(args.from_files[1], encoding="utf-8"))["features"]
        names = [tuple(n) for n in json.load(open(args.from_files[2], encoding="utf-8"))]
        fetched_at = args.fetched_at or "2026-11-23T00:00:00Z"
    else:
        fetched_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fees = fetch_json(FEES_URL)["features"]
        cleaning = fetch_json(CLEANING_URL)["features"]
        check("fees", len(fees))
        check("cleaning", len(cleaning))
        names = fetch_names()
        print(f"fetched {len(fees)} fee lines, {len(cleaning)} cleaning lines, {len(names)} NVDB name lines")
    fees = [f for f in fees if f.get("geometry") and f["geometry"]["type"] == "LineString"]
    cleaning = [f for f in cleaning if f.get("geometry") and f["geometry"]["type"] == "LineString"]
    ptillaten, servicedagar, stats = build(fees, cleaning, names)
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(stats.items())))
    write(args.out_dir, ptillaten, servicedagar, fetched_at)


if __name__ == "__main__":
    main()
