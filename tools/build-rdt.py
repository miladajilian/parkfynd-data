#!/usr/bin/env python3
"""Build street parking files for municipalities from Transportstyrelsen's RDT register (national-data-spec.md §10).

    python3 docs/tools/build-rdt.py OUT_DIR --kommun solna --kommun sollentuna [--pages DIR] [--nvdb-cache FILE]

For each municipality it:
1. exports every rule in force from RDT (rdt-probe.py, about 2 s a page; reuses saved pages in --pages),
2. decodes the parking rules (rdt_rules.py),
3. merges the rules that cover the same stretch and side of a road link,
4. cuts the stretch out of Trafikverket's NVDB road link (API key from Config/Secrets.xcconfig), and
5. writes `<id>-ptillaten.json.deflate`, `<id>-servicedagar.json.deflate` and `<id>-snapshot-meta.json`.

Decisions (user, 2026-11-23): only stretches with a parking rule are shown, and rules without road
geometry make stays during their hours "Check the sign" (street rules on stretches of that street;
area rules on the stretches inside the outline the register draws for the decision, or everywhere in
the municipality when there is none: rdt_outlines.py). That is written as `PF_UNCERTAIN` on each stretch.
"""

import argparse
import collections
import datetime
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rules_mod = load("rdt_rules", "rdt_rules.py")
probe = load("rdt_probe", "rdt-probe.py")
nvdb = load("nvdb_probe", "nvdb-probe.py")
bm = load("build_malmo", "build-malmo.py")
outlines = load("rdt_outlines", "rdt_outlines.py")

# id → (name, kommunkod, RDT BerörKommunId). Extend as municipalities are added (config/municipalities.json).
MUNICIPALITIES = {
    "sollentuna": ("Sollentuna", "0163", 32),
    "solna": ("Solna", "0184", 36),
}
DATE_PARKING = re.compile(r"(udda|jämn[at]?) datum")
AREA_TEXT = re.compile(r"\b(område|områden|samtliga vägar|[Ii]nom [A-ZÅÄÖ])")
STREET_IN_TEXT = re.compile(r"\b[Pp]å\s+(?:markerade sträckor på\s+|markerad sträcka på\s+|)([A-ZÅÄÖ][\wåäöÅÄÖé.' -]*?)"
                            r"(?: enligt| vid| mellan| på| från|,|$)")


# MARK: - Rules → effects

STREET_NAME = re.compile(
    r"\b((?:[A-ZÅÄÖ][\wåäöéü:'-]*\s)*[A-ZÅÄÖ]?[\wåäöéü:'-]*?(?:vägen|väg|gatan|gata|gränd|allé|backen|backe|"
    r"stigen|stig|torget|torg|planen|plan|platsen|åsen|leden|led|promenaden|slingan|svängen|kroken|gången|kajen|"
    r"esplanaden|parken|bron|bro|liden|lid|höjden|vallen|tået|udden))\b")


def street_of(rule):
    """The street a rule names: the decoded VÄG, else "På X …", else the first street-like name."""
    if rule.street:
        return rule.street
    m = STREET_IN_TEXT.search(rule.text)
    if m:
        return m.group(1).strip()
    for m in STREET_NAME.finditer(rule.text):
        name = m.group(1).strip()
        if name[:1].isupper() and len(name) > 3:
            return name
    return None


def window_token(w):
    days = "1234567" if w.days is None else "".join(str(d) for d in sorted(w.days))
    return f"{days}@{w.start // 60:02d}{w.start % 60:02d}-{w.end // 60:02d}{w.end % 60:02d}"


def uncertain_tokens(rule, area=False):
    """What an unplaced rule means for stretches it might cover, as PF_UNCERTAIN tokens.

    For an area rule only its restrictions count: a ban or cleaning time inside the area can apply to
    any stretch there, but a permission ("max 15 min at the marked places") says nothing about others."""
    if rule.excluded:
        return set()
    if area:
        tokens = set()
        if DATE_PARKING.search(rule.text):
            # Datumparkering is worded as a permission ("on odd dates … on the side with odd numbers")
            # but restricts every street in the area during its hours.
            return {"ALL" if w is None else window_token(w) for w in rule.windows} or {"ALL"}
        if rule.ban_always and not rule.windows:
            tokens.add("BAN")
        tokens |= {window_token(w) for w in rule.cleaning}
        if rule.cleaning_unknown:
            tokens.add("ALL")
        return tokens
    tokens = set()
    if rule.ban_always and not rule.windows:
        # A ban with no hours: a parking-ban area ("förbud att parkera inom …") or a single spot.
        # An explicit permission on a stretch is the exception to it, so it only matters where
        # nothing permits parking (see describe).
        tokens.add("BAN")
    if (rule.time_unknown and not rule.windows) or rule.cleaning_unknown:
        tokens.add("ALL")
    for w in rule.windows:
        tokens.add("ALL" if w is None else window_token(w))
    if rule.max_minutes:
        tokens.add(f"MAX{rule.max_minutes}")
    if rule.fee:
        tokens.add("FEE")
    return tokens


def compact(tokens):
    if "ALL" in tokens:
        tokens = {t for t in tokens if t in ("ALL", "FEE", "BAN")}
    maxes = [int(t[3:]) for t in tokens if t.startswith("MAX")]
    tokens = {t for t in tokens if not t.startswith("MAX")} | ({f"MAX{min(maxes)}"} if maxes else set())
    return sorted(tokens)


# MARK: - Stretches

def stretches(rules):
    """Linked rules → {(link, side, start, end): [rule, …]} on non-overlapping pieces of each link side."""
    by_side = collections.defaultdict(list)
    for rule in rules:
        for link, start, end, side in rule.links:
            for s in ([side] if side else ["left", "right"]):
                by_side[(link, s)].append((start, end, rule))
    pieces = {}
    for (link, side), spans in by_side.items():
        cuts = sorted({round(x, 6) for a, b, _ in spans for x in (a, b)})
        for a, b in zip(cuts, cuts[1:]):
            if b - a < 1e-4:
                continue
            covering = [r for s, e, r in spans if s <= a + 1e-9 and e >= b - 1e-9]
            if covering:
                pieces[(link, side, a, b)] = covering
    return pieces


def fee_text(rule, kommun_name):
    """The rate text a tariff in config/tariffs.json is keyed by: the zone letter, the stretch's own fee
    hours when it states them, and the municipality, e.g. "Avgift taxa B vardag utom vardag före
    sön- och helgdag 09.00-19.00 (Sollentuna)" or "Avgift taxa A (Solna)". Anything else stays unknown."""
    m = re.match(r"^\s*([A-Za-z])\b", rule.fee or "")
    if not m:
        return f"Avgift okänd ({kommun_name})"
    hours = ""
    if rule.fee_hours:
        if len(rule.fee_hours) != 1 or not rule.fee_hours[0].daytype:
            return f"Avgift taxa {m.group(1).upper()} med tider ({kommun_name})"
        w = rule.fee_hours[0]
        hours = f" {w.daytype} {w.start // 60:02d}.{w.start % 60:02d}-{w.end // 60:02d}.{w.end % 60:02d}"
    return f"Avgift taxa {m.group(1).upper()}{hours} ({kommun_name})"


def rdt_url(designation):
    m = re.match(r"^(\d{4}) (\d{4}):(\S+)$", designation or "")
    if not m:
        return None
    return (f"https://rdt.transportstyrelsen.se/rdt/AF06_View.aspx?BeslutsMyndighetKod={m.group(1)}"
            f"&BeslutadAr={m.group(2)}&LopNr={m.group(3)}")


def describe(covering, area_tokens, street_tokens, kommun_name, unplaced_fee_somewhere):
    """Merged rules on one piece → (ptillaten properties, [servicedagar properties]) or None (not shown)."""
    if any(r.ban_always or r.excluded for r in covering):
        return None
    if not any(r.permit or r.fee or r.cleaning for r in covering):
        return None
    main = next((r for r in covering if r.fee), None) or next((r for r in covering if r.permit), covering[0])
    street = next(filter(None, (street_of(r) for r in covering)), None)
    uncertain = set(area_tokens) | set(street_tokens.get((street or "").lower(), ()))
    permitted = any(r.permit or r.fee for r in covering)
    if "BAN" in uncertain and not permitted:
        uncertain.add("ALL")                  # cleaning-only stretch inside a possible ban area
    uncertain.discard("BAN")
    fee_rule = next((r for r in covering if r.fee), None)
    if fee_rule:
        rate = fee_text(fee_rule, kommun_name)
    elif "FEE" in uncertain or unplaced_fee_somewhere and not street:
        rate = f"Avgift okänd ({kommun_name})"
    else:
        rate = "avgiftsfri"
    props = {
        "VEHICLE": "fordon",
        "CITATION": main.regulation or None,
        "RDT_URL": rdt_url(main.regulation),
        "STREET_NAME": street,
        "CITY_DISTRICT": kommun_name,
        "PARKING_RATE": rate,
        "OTHER_INFO": " ".join(sorted({f"{r.regulation} {r.title}".strip() for r in covering})) or None,
    }
    maxes = [r.max_minutes for r in covering if r.max_minutes]
    if maxes:
        props["MAX_MINUTES"] = min(maxes)
    if any(r.time_unknown for r in covering):
        props["PF_TIME"] = "unknown"
    if any(r.cleaning_unknown for r in covering):
        props["PF_CLEANING"] = "unknown"
    tokens = compact(uncertain - {"FEE"})
    if tokens:
        props["PF_UNCERTAIN"] = ",".join(tokens)
    bans = []
    for r in covering:
        for w in r.cleaning:
            for day in sorted(w.days):
                ban = {"CITATION": props["CITATION"], "START_WEEKDAY": rules_mod.WEEKDAYS[day - 1],
                       "START_TIME": (w.start // 60) * 100 + w.start % 60, "END_TIME": (w.end // 60) * 100 + w.end % 60}
                if w.parity:
                    ban["ODD_EVEN"] = w.parity
                if w.season:
                    ban.update(dict(zip(("START_MONTH", "START_DAY", "END_MONTH", "END_DAY"), w.season)))
                bans.append(ban)
    unique = {json.dumps(b, sort_keys=True): b for b in bans}
    return {k: v for k, v in props.items() if v is not None}, list(unique.values())


# MARK: - NVDB geometry

def fetch_links(ids, cache_path):
    cache = json.load(open(cache_path, encoding="utf-8")) if cache_path and os.path.exists(cache_path) else {}
    missing = sorted(set(ids) - set(cache))
    if missing:
        key = nvdb.api_key()
        for i in range(0, len(missing), 400):
            batch = missing[i:i + 400]
            body = (f'<REQUEST><LOGIN authenticationkey="{key}"/><QUERY objecttype="Vägtrafiknät" '
                    f'namespace="{nvdb.NAMESPACE}" schemaversion="{nvdb.SCHEMA}" limit="10000">'
                    f'<FILTER><IN name="Element_Id" value="{",".join(batch)}"/></FILTER>'
                    "<INCLUDE>Element_Id</INCLUDE><INCLUDE>Start_Measure</INCLUDE><INCLUDE>End_Measure</INCLUDE>"
                    "<INCLUDE>Geometry.WKT-WGS84-3D</INCLUDE></QUERY></REQUEST>")
            request = urllib.request.Request(nvdb.API, data=body.encode("utf-8"),
                                             headers={"Content-Type": "text/xml; charset=utf-8", "User-Agent": "Parkfynd-rdt"})
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.load(response)["RESPONSE"]["RESULT"][0]
            for record in result.get("Vägtrafiknät", []):
                wkt = record.get("Geometry", {}).get("WKT-WGS84-3D", "")
                coords = [[float(v) for v in p.split()[:2]] for p in wkt[wkt.find("(") + 1:wkt.rfind(")")].split(",") if p.strip()]
                cache.setdefault(record["Element_Id"], []).append([record["Start_Measure"], record["End_Measure"], coords])
            for link in batch:
                cache.setdefault(link, [])
        if cache_path:
            json.dump(cache, open(cache_path, "w", encoding="utf-8"))
    return cache


def length(coords):
    return sum(math.hypot((b[0] - a[0]) * 111_320 * math.cos(math.radians(a[1])), (b[1] - a[1]) * 110_574)
               for a, b in zip(coords, coords[1:]))


def cut(coords, f0, f1):
    """The part of a line between fractions f0 and f1 of its length."""
    total = length(coords)
    if total == 0:
        return None
    t0, t1 = f0 * total, f1 * total
    out, walked = [], 0.0
    for a, b in zip(coords, coords[1:]):
        seg = length([a, b])
        if seg == 0:
            continue
        s, e = walked, walked + seg
        if e >= t0 and s <= t1:
            u0, u1 = max(0, (t0 - s) / seg), min(1, (t1 - s) / seg)
            p0 = [a[0] + (b[0] - a[0]) * u0, a[1] + (b[1] - a[1]) * u0]
            p1 = [a[0] + (b[0] - a[0]) * u1, a[1] + (b[1] - a[1]) * u1]
            if not out:
                out.append(p0)
            out.append(p1)
        walked = e
    return [[round(x, 7), round(y, 7)] for x, y in out] if len(out) >= 2 else None


def slice_link(pieces, a, b):
    """NVDB pieces [(m0, m1, coords)] of one link → the line between relative distances a and b."""
    line = []
    for m0, m1, coords in sorted(pieces):
        lo, hi = max(a, m0), min(b, m1)
        if hi - lo <= 1e-9 or m1 <= m0:
            continue
        part = cut(coords, (lo - m0) / (m1 - m0), (hi - m0) / (m1 - m0))
        if part:
            line += part if not line else part[1:]
    return line if len(line) >= 2 else None


# MARK: - Build

def build(kommun_id, pages_dir, enums, cache_path, name=None):
    name = name or MUNICIPALITIES[kommun_id][0]
    rules = [r for path in sorted(os.listdir(pages_dir)) if path.startswith("page-")
             for r in rules_mod.page_rules(os.path.join(pages_dir, path), enums)]
    linked = [r for r in rules if r.links]
    unplaced = [r for r in rules if not r.links]
    area_tokens, street_tokens = set(), collections.defaultdict(set)
    stats_unplaced_spots = []
    unplaced_fee = False
    placed_areas, placed_regulations = [], set()     # [(rings, tokens)] area rules with an outline
    outline_cache = os.path.join(os.path.dirname(cache_path) or ".", "parkfynd-rdt-outlines.json") if cache_path else None
    for r in unplaced:
        street = street_of(r)
        if street and not r.area:
            tokens = uncertain_tokens(r)
            unplaced_fee |= "FEE" in tokens
            street_tokens[street.lower()] |= tokens
        elif r.area or AREA_TEXT.search(r.text):
            tokens = uncertain_tokens(r, area=True)
            rings = outlines.outline(r.regulation, outline_cache) if tokens else None
            if rings:
                placed_areas.append((rings, tokens))
                placed_regulations.add(r.regulation)
            else:
                area_tokens |= tokens
        else:
            stats_unplaced_spots.append(r.regulation)   # a named spot with no street: can't be placed
    pieces = stretches(linked)
    links = fetch_links({link for link, *_ in pieces}, cache_path)
    ptillaten, servicedagar = [], []
    stats = collections.Counter(rules=len(rules), linked=len(linked), unplaced=len(unplaced))
    for (link, side, a, b), covering in sorted(pieces.items()):
        line = slice_link(links.get(link, []), a, b)
        local = set(area_tokens)
        for rings, tokens in placed_areas:
            if line and not tokens <= local and outlines.touches(line, rings):
                local |= tokens
        described = describe(covering, local, street_tokens, name, unplaced_fee)
        if not described:
            stats["pieces not shown"] += 1
            continue
        if not line:
            stats["pieces without NVDB geometry"] += 1
            continue
        props, bans = described
        fid = int(hashlib.sha1(f"{link}|{side}|{a:.6f}|{b:.6f}".encode()).hexdigest()[:12], 16) * 10
        props = {"FID": fid, **props}
        geometry = {"type": "LineString", "coordinates": line}
        ptillaten.append({"type": "Feature", "geometry": geometry, "properties": props})
        for k, ban in enumerate(bans[:9]):
            servicedagar.append({"type": "Feature", "geometry": geometry, "properties": {"FID": fid + k + 1, **ban}})
        stats["shown"] += 1
        for key in ("PF_TIME", "PF_CLEANING", "PF_UNCERTAIN"):
            stats[key] += key in props
        stats["priced as free"] += props["PARKING_RATE"] == "avgiftsfri"
    stats["unplaced spots ignored"] = len(stats_unplaced_spots)
    stats["area tokens"] = ",".join(compact(area_tokens)) or "-"
    stats["area rules placed by outline"] = len(placed_regulations)
    return ptillaten, servicedagar, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out_dir")
    parser.add_argument("--kommun", action="append", required=True, choices=sorted(MUNICIPALITIES))
    parser.add_argument("--pages", help="folder with <kommun>/page-*.xml from earlier exports (reused, completed)")
    parser.add_argument("--catalogue", help="a saved datakatalog XML (else fetched)")
    parser.add_argument("--nvdb-cache", help="JSON cache of NVDB link geometry")
    parser.add_argument("--fetched-at")
    args = parser.parse_args()
    catalogue = open(args.catalogue, encoding="utf-8").read() if args.catalogue else probe.live_catalogue()
    enums = rules_mod.build_enums(catalogue)
    fetched_at = args.fetched_at or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for kommun in args.kommun:
        pages = os.path.join(args.pages or os.path.join(args.out_dir, ".rdt-pages"), kommun)
        if not args.fetched_at:
            probe.cmd_fetch(MUNICIPALITIES[kommun][2], pages, None)   # resumable: only missing pages
        ptillaten, servicedagar, stats = build(kommun, pages, enums, args.nvdb_cache)
        print(f"{kommun}: " + ", ".join(f"{k}: {v}" for k, v in stats.items()), flush=True)
        bm.write(args.out_dir, ptillaten, servicedagar, fetched_at, prefix=kommun)


if __name__ == "__main__":
    sys.exit(main())
