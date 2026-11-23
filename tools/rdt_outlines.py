"""Outlines of RDT area rules (national-data-spec.md §10).

Area rules ("inom ett område som avgränsas av kartbilagan", datumparkering) have no road-network
link in the export, but the register's public page for each decision
(`rdt.transportstyrelsen.se/rdt/AF06_View.aspx?…`) draws the area on a map from a GeoJSON outline in
SWEREF99 TM. This module reads that outline, converts it to WGS84, and tells whether a stretch lies
in it, so an area rule only makes the stretches inside it "Check the sign".

A decision is never changed after it's published (amendments are new decisions), so outlines are
cached for good, by designation. Anything unexpected (no outline, a fetch error, a line that isn't
closed) returns None, and the caller keeps treating the rule as covering the whole municipality.
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


# MARK: - Outlines

def rings_of(geojson):
    """GeoJSON (SWEREF99 TM) → [ring of [lon, lat]] or None. Polygons, multipolygons and closed lines
    count as areas; an open line or a point doesn't outline anything."""
    rings = []
    for feature in geojson.get("features", []):
        geometry = feature.get("geometry") or {}
        kind, coords = geometry.get("type"), geometry.get("coordinates")
        if kind == "Polygon":
            parts = coords
        elif kind == "MultiPolygon":
            parts = [ring for polygon in coords for ring in polygon]
        elif kind == "LineString" and len(coords) >= 4 and math.dist(coords[0][:2], coords[-1][:2]) < 1:
            parts = [coords]
        elif kind == "MultiLineString" and coords and all(len(c) >= 4 and math.dist(c[0][:2], c[-1][:2]) < 1 for c in coords):
            parts = coords
        else:
            return None
        rings += [[list(sweref99tm_to_wgs84(x, y)) for x, y, *_ in ring] for ring in parts]
    return rings or None


def outline(designation, cache_path=None, fetch=None):
    """The area of a decision ("0163 2018:135") as [ring of [lon, lat]], or None."""
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
        rings = rings_of(json.loads(found.group(1))) if found else None
    except Exception:          # a failed fetch isn't cached: the next run tries again
        return None
    cache[designation] = rings
    if cache_path:
        json.dump(cache, open(cache_path, "w", encoding="utf-8"))
    return rings


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
