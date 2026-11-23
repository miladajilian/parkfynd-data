"""Outlines of RDT area rules (national-data-spec.md §10).

Area rules ("inom ett område som avgränsas av kartbilagan", datumparkering) have no road-network
link in the export, but the register's public page for each decision
(`rdt.transportstyrelsen.se/rdt/AF06_View.aspx?…`) draws the area on a map from a GeoJSON outline in
SWEREF99 TM. This module reads that outline, converts it to WGS84, and tells whether a stretch lies
in it, so an area rule only makes the stretches inside it "Check the sign".

Street rules use the same maps: a permission "enligt markering på kartbild" covers only the marked
spot, often a few meters on one side, while its road-network link covers the whole road section.
`narrow` cuts the linked stretch to the marked part and side.

A decision is never changed after it's published (amendments are new decisions), so maps are
cached for good, by designation. Anything unexpected (no map, a fetch error, an area that isn't
closed) returns None, and the caller keeps the rule as it was.
"""

import json
import math
import os
import re
import urllib.request

VIEW = "https://rdt.transportstyrelsen.se/rdt/AF06_View.aspx?BeslutsMyndighetKod={0}&BeslutadAr={1}&LopNr={2}"
REGISTER = re.compile(r"Register\.init\('[0-9a-f-]+',\s*'(\{.*?\})'\);", re.S)
# A stretch this close to the outline counts as inside: the outlines are drawn by hand along streets,
# so a boundary street may fall just outside the line.
MARGIN_METERS = 30


# MARK: - SWEREF99 TM → WGS84 (Gauss–Krüger, GRS 80; Lantmäteriet's formulas)

def sweref99tm_to_wgs84(easting, northing):
    a, f = 6378137.0, 1 / 298.257222101
    lon0, k0, fn, fe = math.radians(15.0), 0.9996, 0.0, 500000.0
    e2 = f * (2 - f)
    n = f / (2 - f)
    a_hat = a / (1 + n) * (1 + n ** 2 / 4 + n ** 4 / 64)
    d1 = n / 2 - 2 * n ** 2 / 3 + 37 * n ** 3 / 96 - n ** 4 / 360
    d2 = n ** 2 / 48 + n ** 3 / 15 - 437 * n ** 4 / 1440
    d3 = 17 * n ** 3 / 480 - 37 * n ** 4 / 840
    d4 = 4397 * n ** 4 / 161280
    xi = (northing - fn) / (k0 * a_hat)
    eta = (easting - fe) / (k0 * a_hat)
    xi_p = (xi - d1 * math.sin(2 * xi) * math.cosh(2 * eta) - d2 * math.sin(4 * xi) * math.cosh(4 * eta)
            - d3 * math.sin(6 * xi) * math.cosh(6 * eta) - d4 * math.sin(8 * xi) * math.cosh(8 * eta))
    eta_p = (eta - d1 * math.cos(2 * xi) * math.sinh(2 * eta) - d2 * math.cos(4 * xi) * math.sinh(4 * eta)
             - d3 * math.cos(6 * xi) * math.sinh(6 * eta) - d4 * math.cos(8 * xi) * math.sinh(8 * eta))
    phi_star = math.asin(math.sin(xi_p) / math.cosh(eta_p))
    dlon = math.atan(math.sinh(eta_p) / math.cos(xi_p))
    a_ = e2 + e2 ** 2 + e2 ** 3 + e2 ** 4
    b_ = -(7 * e2 ** 2 + 17 * e2 ** 3 + 30 * e2 ** 4) / 6
    c_ = (224 * e2 ** 3 + 889 * e2 ** 4) / 120
    d_ = -(4279 * e2 ** 4) / 1260
    s = math.sin(phi_star)
    phi = phi_star + s * math.cos(phi_star) * (a_ + b_ * s ** 2 + c_ * s ** 4 + d_ * s ** 6)
    return round(math.degrees(lon0 + dlon), 7), round(math.degrees(phi), 7)


# MARK: - Maps

def shapes_of(geojson):
    """GeoJSON (SWEREF99 TM) → [{"type": geometry type, "parts": [[[lon, lat], …], …]}] in WGS84.
    Polygons give their rings, lines their lines and points one-point parts."""
    shapes = []
    for feature in geojson.get("features", []):
        geometry = feature.get("geometry") or {}
        kind, coords = geometry.get("type"), geometry.get("coordinates")
        if not coords:
            continue
        parts = {"Polygon": lambda: coords, "MultiPolygon": lambda: [ring for polygon in coords for ring in polygon],
                 "LineString": lambda: [coords], "MultiLineString": lambda: coords,
                 "Point": lambda: [[coords]], "MultiPoint": lambda: [coords]}.get(kind)
        if parts:
            shapes.append({"type": kind, "parts": [[list(sweref99tm_to_wgs84(x, y)) for x, y, *_ in part] for part in parts()]})
    return shapes


def rings_from(shapes):
    """Map shapes → [ring of [lon, lat]] or None. Polygons, multipolygons and closed lines count as
    areas; an open line or a point doesn't outline anything."""
    rings = []
    for shape in shapes or []:
        parts = shape["parts"]
        closed = all(len(p) >= 4 and math.dist(_meters(p[0][1])(p[0]), _meters(p[0][1])(p[-1])) < 1 for p in parts)
        if shape["type"] in ("Polygon", "MultiPolygon") or shape["type"] in ("LineString", "MultiLineString") and closed:
            rings += parts
        else:
            return None
    return rings or None


def rings_of(geojson):
    return rings_from(shapes_of(geojson))


def map_of(designation, cache_path=None, fetch=None):
    """The shapes on the register's map of a decision ("0163 2022:70"): a list, empty when the page
    has no map, or None when the page couldn't be read (not cached, so the next run tries again)."""
    cache = {}
    if cache_path and os.path.exists(cache_path):
        cache = json.load(open(cache_path, encoding="utf-8"))
    if designation in cache:
        return cache[designation]
    m = re.match(r"^(\d{4}) (\d{4}):(\S+)$", designation or "")
    if not m:
        return None
    try:
        html = (fetch or _fetch)(VIEW.format(*m.groups()))
        found = REGISTER.search(html)
        shapes = shapes_of(json.loads(found.group(1))) if found else []
    except Exception:
        return None
    cache[designation] = shapes
    if cache_path:
        json.dump(cache, open(cache_path, "w", encoding="utf-8"))
    return shapes


def outline(designation, cache_path=None, fetch=None):
    """The area of a decision ("0163 2018:135") as [ring of [lon, lat]], or None."""
    return rings_from(map_of(designation, cache_path, fetch))


def _fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Parkfynd-rdt"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8", "replace")


# MARK: - Geometry

def _meters(origin_lat):
    kx, ky = 111_320 * math.cos(math.radians(origin_lat)), 110_574
    return lambda p: (p[0] * kx, p[1] * ky)


def _inside(point, rings):
    """Even–odd rule over every ring, so holes are outside."""
    x, y = point
    inside = False
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def _distance_to_segment(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def touches(line, rings, margin=MARGIN_METERS):
    """Whether a stretch ([lon, lat] points) lies in the area or within `margin` meters of its edge."""
    if not line or not rings:
        return False
    to_m = _meters(line[0][1])
    ring_m = [[to_m(p) for p in ring] for ring in rings]
    points = []
    line_m = [to_m(p) for p in line]
    for a, b in zip(line_m, line_m[1:]):
        steps = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) // 10))
        points += [(a[0] + (b[0] - a[0]) * i / steps, a[1] + (b[1] - a[1]) * i / steps) for i in range(steps)]
    points.append(line_m[-1])
    for p in points:
        if _inside(p, ring_m):
            return True
        if any(_distance_to_segment(p, a, b) <= margin for ring in ring_m for a, b in zip(ring, ring[1:] + ring[:1])):
            return True
    return False


# MARK: - Marked spots on street rules

# A marking farther than this from a road section isn't on it (it marks another section of the rule).
MARK_DISTANCE = 20
# A marking this far to one side of the road's center line is on that side; closer is the road itself.
SIDE_OFFSET = 1.5
# Added at each end of the marked part, for drawing slack.
PAD_METERS = 2
# Marked parts further apart than this along the road are separate spots.
GAP_METERS = 6


def _densify(part, step=2.0):
    points = [part[0]]
    for a, b in zip(part, part[1:]):
        n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) // step))
        points += [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(1, n + 1)]
    return points


def _closed(part):
    return len(part) >= 4 and math.dist(part[0], part[-1]) < 1


def narrow(line, shapes):
    """Where a road section ([lon, lat] points, in the link's direction) is marked on a decision's map:
    [(start fraction, end fraction, side)], one per marked spot, with side "left", "right" or None
    (both or unclear). None when nothing on the map lies on this section.

    Maps often draw the whole road section as a line along the road and the parking places as boxes
    beside it, so when a map has closed shapes only those count."""
    if not line or len(line) < 2 or not shapes:
        return None
    to_m = _meters(line[0][1])
    road = [to_m(p) for p in line]
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(road, road[1:])]
    total = sum(lengths)
    if total == 0:
        return None
    parts = [[to_m(q) for q in part] for shape in shapes for part in shape["parts"]]
    if any(_closed(part) for part in parts):
        parts = [part for part in parts if _closed(part)]
    marks = []                                           # (position along, side or None)
    for part in parts:
        for p in _densify(part):
            best = None                                  # (distance, position along, signed offset)
            walked = 0.0
            for (a, b), seg in zip(zip(road, road[1:]), lengths):
                if seg > 0:
                    dx, dy = b[0] - a[0], b[1] - a[1]
                    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (seg * seg)
                    tc = max(0.0, min(1.0, t))
                    distance = math.hypot(p[0] - a[0] - tc * dx, p[1] - a[1] - tc * dy)
                    beyond = (t < 0 and walked == 0) or (t > 1 and walked + seg >= total - 1e-9)
                    if not beyond and (best is None or distance < best[0]):
                        offset = (dx * (p[1] - a[1]) - dy * (p[0] - a[0])) / seg   # > 0: left of travel
                        best = (distance, walked + tc * seg, offset)
                walked += seg
            if best and best[0] <= MARK_DISTANCE:
                side = None if abs(best[2]) < SIDE_OFFSET else ("left" if best[2] > 0 else "right")
                marks.append((best[1], side))
    if not marks:
        return None
    marks.sort(key=lambda mark: mark[0])
    groups = [[marks[0]]]                                # marked spots: runs without a gap of more than GAP_METERS
    for mark in marks[1:]:
        if mark[0] - groups[-1][-1][0] > GAP_METERS:
            groups.append([])
        groups[-1].append(mark)
    spots = []
    for group in groups:
        # Points on the center line itself say nothing about the side (maps and NVDB differ by a meter or two).
        sides = {side for _, side in group if side}
        side = next(iter(sides)) if len(sides) == 1 else None
        spots.append((max(0.0, (group[0][0] - PAD_METERS) / total), min(1.0, (group[-1][0] + PAD_METERS) / total), side))
    return spots
