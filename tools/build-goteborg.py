#!/usr/bin/env python3
"""Build Göteborg's street parking data in the shape of the Stockholm city snapshot (national-data-spec.md §9).

    python3 docs/tools/build-goteborg.py Parkfynd/Resources/CitySnapshot

The APPID is read from `GOTEBORG_APPID` in the environment or the git-ignored Config/Secrets.xcconfig,
and is never printed or written to the output. For test fixtures, build from saved answers instead:

    python3 docs/tools/build-goteborg.py OUT --from-files TOLL.json TIME.json CLEANING.json --fetched-at 2026-11-23T12:00:00Z

Inputs, Göteborgs Stad ParkingService v2.3 (data.goteborg.se, "fritt att använda, utan avgifter eller licenser"):
- PublicTollParkings: street parking with a fee. Regulation id, street, fee text, max time, own cleaning ban text.
- PublicTimeParkings: free street parking with a max time.
- CleaningZones: weekly cleaning bans with weekday, hours, season and week parity, used where a
  stretch has no cleaning text of its own.

Output: `goteborg-{ptillaten,servicedagar}.json.deflate` + `goteborg-snapshot-meta.json`, LTF-Tolken-shaped.
Extension keys as in build-malmo.py, plus `PF_TIME = "unknown"` when the max-time text isn't a plain limit.
"""

import argparse
import collections
import datetime
import importlib.util
import json
import os
import re
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("build_malmo", os.path.join(HERE, "build-malmo.py"))
bm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bm)

BASE = "https://data.goteborg.se/ParkingService/v2.3/"
DATASETS = ("PublicTollParkings", "PublicTimeParkings", "CleaningZones")
# 2026-11-23: 755 toll stretches, 1,857 free stretches, 1,237 cleaning zones. Refuse anything far off.
EXPECTED = {"PublicTollParkings": 755, "PublicTimeParkings": 1_857, "CleaningZones": 1_237}
SECRETS = os.path.join(HERE, "..", "..", "Config", "Secrets.xcconfig")

WEEKDAYS = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]
MONTHS = ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september",
          "oktober", "november", "december"]
DAY = r"(måndag|tisdag|onsdag|torsdag|fredag|lördag|söndag)(?:ar)?"
DATE = r"(\d{1,2})(?::[ae])? (" + "|".join(MONTHS) + r")"
# "P-förbud torsdagar klockan 02.00 - 07.00 jämna veckor under tiden 15:e mars - 30:e april."
BAN = re.compile(
    rf"^P-förbud {DAY}(?: (jämna|udda) veckor)? klockan (\d{{2}})\.(\d{{2}}) - (\d{{2}})\.(\d{{2}})"
    rf"(?: (jämna|udda) veckor)?(?:\s+(?:under tiden )?{DATE} - {DATE})?\.$")
MAX = re.compile(r"^(\d+) (min|tim|dygn)$")


def app_id():
    key = os.environ.get("GOTEBORG_APPID", "").strip()
    if not key and os.path.exists(SECRETS):
        for line in open(SECRETS, encoding="utf-8"):
            match = re.match(r"\s*GOTEBORG_APPID\s*=\s*(\S+)", line)
            if match:
                key = match.group(1)
    if not key:
        raise SystemExit("No GOTEBORG_APPID in the environment or Config/Secrets.xcconfig")
    return key


def fetch(dataset, key):
    request = urllib.request.Request(f"{BASE}{dataset}/{key}?format=json", headers={"User-Agent": "Parkfynd-goteborg-tool"})
    with urllib.request.urlopen(request, timeout=180) as response:
        data = json.load(response)
    if not isinstance(data, list):
        raise SystemExit(f"{dataset}: expected a list")
    expected = EXPECTED[dataset]
    if abs(len(data) - expected) > expected * 0.10:
        raise SystemExit(f"{dataset}: {len(data)} objects, expected about {expected} (±10 %). Look before updating EXPECTED.")
    return data


def parse_wkt(wkt):
    """Every line and polygon ring in a WKT geometry, as [[lon, lat], …] lists."""
    parts = []
    for group in re.findall(r"\(([^()]+)\)", wkt or ""):
        coords = [[float(v) for v in pair.split()[:2]] for pair in group.split(",") if pair.strip()]
        if len(coords) >= 2:
            parts.append(coords)
    return parts


def geometry(parts):
    if len(parts) == 1:
        return {"type": "LineString", "coordinates": parts[0]}
    return {"type": "MultiLineString", "coordinates": parts}


def fid(regulation_id, salt=0):
    """'1480 2007-01352' → 200701352 (+ salt), stable across runs."""
    match = re.match(r"^\d{4} (\d{4})-(\d+)$", regulation_id or "")
    if not match:
        return None
    return (int(match.group(1)) * 100_000 + int(match.group(2))) * 10 + salt


def rdt_url(regulation_id):
    match = re.match(r"^(\d{4}) (\d{4})-(\d+)$", regulation_id or "")
    if not match:
        return None
    return ("https://rdt.transportstyrelsen.se/rdt/AF06_View.aspx?"
            f"BeslutsMyndighetKod={match.group(1)}&BeslutadAr={match.group(2)}&LopNr={match.group(3)}")


def max_time(text):
    match = MAX.match((text or "").strip())
    if not match:
        return {}
    n, unit = int(match.group(1)), match.group(2)
    return {"min": {"MAX_MINUTES": n}, "tim": {"MAX_HOURS": n}, "dygn": {"MAX_DAYS": n}}[unit]


def ban_from_text(text):
    """LTF servicedagar properties for one plain weekly ban, or None."""
    match = BAN.match(re.sub(r"\s+", " ", text.strip()))
    if not match:
        return None
    day, parity1, h1, m1, h2, m2, parity2, d1, mon1, d2, mon2 = match.groups()
    if parity1 and parity2:
        return None
    props = {"START_WEEKDAY": day, "START_TIME": int(h1 + m1), "END_TIME": int(h2 + m2)}
    parity = parity1 or parity2
    if parity:
        props["ODD_EVEN"] = f"{parity} veckor"
    if d1:
        props.update({"START_MONTH": MONTHS.index(mon1) + 1, "START_DAY": int(d1),
                      "END_MONTH": MONTHS.index(mon2) + 1, "END_DAY": int(d2)})
    return props


def ban_from_zone(zone):
    """LTF servicedagar properties from a CleaningZones object's structured fields, or None."""
    weekday = zone.get("WeekDay")
    if not isinstance(weekday, int) or not 1 <= weekday <= 7 or zone.get("OnlyEvenWeeks") and zone.get("OnlyOddWeeks"):
        return None
    props = {"START_WEEKDAY": WEEKDAYS[weekday - 1], "START_TIME": zone["StartHour"] * 100, "END_TIME": zone["EndHour"] * 100}
    if zone.get("OnlyEvenWeeks"):
        props["ODD_EVEN"] = "jämna veckor"
    if zone.get("OnlyOddWeeks"):
        props["ODD_EVEN"] = "udda veckor"
    season = (zone.get("StartMonth"), zone.get("StartDay"), zone.get("EndMonth"), zone.get("EndDay"))
    if season != (1, 1, 12, 31):
        props.update(dict(zip(("START_MONTH", "START_DAY", "END_MONTH", "END_DAY"), season)))
    return props


def build(toll, time, cleaning):
    zone_index = bm.SegmentIndex()
    zones = []
    for zone in cleaning:
        for part in parse_wkt(zone.get("WKT")):
            zone_index.add(len(zones), part)
            zones.append(zone)

    ptillaten, servicedagar = [], []
    stats = collections.Counter()
    for kind, objects in (("toll", toll), ("time", time)):
        for obj in objects:
            parts = parse_wkt(obj.get("WKT"))
            number = fid(obj.get("Id"))
            if not parts or number is None:
                stats["skipped (no geometry or id)"] += 1
                continue
            geom = geometry(parts)
            props = {
                "FID": number,
                "VEHICLE": "fordon",
                "CITATION": obj["Id"],
                "RDT_URL": rdt_url(obj["Id"]),
                "STREET_NAME": obj.get("Name"),
                "CITY_DISTRICT": "Göteborg",
                "PARKING_RATE": f"{obj['ParkingCharge']}: {obj['ParkingCost']}" if kind == "toll" else "avgiftsfri",
                "VF_PLATSER": obj.get("ParkingSpaces"),
            }
            props.update(max_time(obj.get("MaxParkingTime")))
            if obj.get("MaxParkingTime") and not max_time(obj.get("MaxParkingTime")):
                props["PF_TIME"] = "unknown"
            limitation = obj.get("MaxParkingTimeLimitation") or ""
            if limitation and not limitation.startswith("Tidsbegränsningen gäller"):
                props["PF_TIME"] = "unknown"   # e.g. "Parkering är dock tillåten under högst 4 tim …"
            extra = (obj.get("ExtraInfo") or "").strip()
            props["OTHER_INFO"] = " ".join(t for t in (limitation, extra) if t) or None

            bans = []
            if extra:
                ban = ban_from_text(extra) if extra.count("P-förbud") == 1 else None
                if ban:
                    bans.append(ban)
                    stats["cleaning from own text"] += 1
                else:
                    props["PF_CLEANING"] = "unknown"
                    stats["cleaning unknown (text)"] += 1
            else:
                coords = [c for part in parts for c in part]
                on, near = collections.Counter(), set()
                for c in coords:
                    for owner, d in zone_index.distances(c, bm.NEARBY_METERS).items():
                        near.add(owner)
                        if d <= bm.MATCH_METERS:
                            on[owner] += 1
                matched = [o for o, n in on.items() if n >= bm.MATCH_SHARE * len(coords)]
                found = [ban_from_zone(zones[o]) for o in matched]
                if matched and all(found):
                    bans += found
                    stats["cleaning from zones"] += 1
                elif matched or near:
                    props["PF_CLEANING"] = "unknown"
                    stats["cleaning unknown (zones)"] += 1
                else:
                    stats["no cleaning nearby"] += 1
            for k, ban in enumerate(bans):
                servicedagar.append({"type": "Feature", "geometry": geom, "properties": {
                    "FID": number + k + 1, "CITATION": obj["Id"], **ban}})
            stats[kind] += 1
            ptillaten.append({"type": "Feature", "geometry": geom,
                              "properties": {k: v for k, v in props.items() if v is not None}})
    return ptillaten, servicedagar, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out_dir")
    parser.add_argument("--from-files", nargs=3, metavar=("TOLL", "TIME", "CLEANING"))
    parser.add_argument("--fetched-at")
    args = parser.parse_args()
    if args.from_files:
        toll, time, cleaning = (json.load(open(p, encoding="utf-8")) for p in args.from_files)
        fetched_at = args.fetched_at or "2026-11-23T00:00:00Z"
    else:
        key = app_id()
        fetched_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        toll, time, cleaning = (fetch(d, key) for d in DATASETS)
        print(f"fetched {len(toll)} toll, {len(time)} free, {len(cleaning)} cleaning objects")
    ptillaten, servicedagar, stats = build(toll, time, cleaning)
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(stats.items())))
    bm.write(args.out_dir, ptillaten, servicedagar, fetched_at, prefix="goteborg")


if __name__ == "__main__":
    main()
