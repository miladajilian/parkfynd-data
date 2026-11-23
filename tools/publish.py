#!/usr/bin/env python3
"""Refresh the published parking files for the Parkfynd app (national-data-spec.md §11).

    python3 tools/publish.py docs/v1 --budget-minutes 330

Run nightly by .github/workflows/publish.yml. Keys come from the environment (GitHub Actions secrets):
TRAFIKVERKET_API_KEY (NVDB, for Malmö street names and RDT geometry) and GOTEBORG_APPID.

Each run:
1. rebuilds Malmö and Göteborg (minutes),
2. then rebuilds RDT municipalities, the ones never built or built longest ago first, until the
   time budget is used. A full pass over all 289 takes several nights; after that each is refreshed
   about every two weeks.
3. writes `index.json` after every municipality, so a run that is stopped still publishes its work.

A municipality whose new stretch count falls by more than 10 % keeps its previous files (the source
probably failed); the run logs it. Stockholm is not published here: the app reads the City's API.
"""

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bm = load("build_malmo", "build-malmo.py")
bg = load("build_goteborg", "build-goteborg.py")
br = load("build_rdt", "build-rdt.py")
probe = br.probe
rules_mod = br.rules_mod

INDEX = "index.json"
# RDT BerörKommunId of municipalities with their own source (Stockholm in the app; Malmö, Göteborg here).
OWN_SOURCES = {"1": "stockholm", "130": "malmo", "172": "goteborg"}
TOLERANCE = 0.10


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(name):
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def kommuner(catalogue):
    """[(rdt code, plain name)] from the data catalogue: "3;Solna kommun" codes, named by the list
    that holds "294;Solna" (the first is genitive: "Stockholms", "Falu")."""
    genitive = {c: n for n, c in re.findall(r"<description>3;([^<]*?) kommun</description>\s*<code>(\d+)</code>", catalogue)}
    i = catalogue.find("<description>294;Sollentuna</description>")
    segment = catalogue[catalogue.rfind(' id="id_', 0, i):catalogue.find("</entries>", i)]
    plain = {c: n for n, c in re.findall(r"<description>\d+;([^<]*)</description>\s*(?:<restrictedValidity>.*?</restrictedValidity>\s*)?<code>(\d+)</code>",
                                         segment, re.S)}
    return sorted((c, plain.get(c, g)) for c, g in genitive.items())


def file_info(path):
    data = open(path, "rb").read()
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def describe(site, prefix, entry):
    """Bounds, center and file hashes for a freshly written municipality."""
    features = json.loads(zlib.decompress(open(os.path.join(site, f"{prefix}-ptillaten.json.deflate"), "rb").read(), -15))["features"]
    coords = [c for f in features for part in ([f["geometry"]["coordinates"]] if f["geometry"]["type"] == "LineString"
                                              else f["geometry"]["coordinates"]) for c in part]
    entry["count"] = len(features)
    if coords:
        lons, lats = [c[0] for c in coords], [c[1] for c in coords]
        entry["bounds"] = {"minLatitude": round(min(lats) - 0.01, 4), "maxLatitude": round(max(lats) + 0.01, 4),
                           "minLongitude": round(min(lons) - 0.02, 4), "maxLongitude": round(max(lons) + 0.02, 4)}
        entry["center"] = {"latitude": round(sum(lats) / len(lats), 5), "longitude": round(sum(lons) / len(lons), 5)}
        codes = [f["properties"].get("CITATION", "")[:4] for f in features if re.match(r"^\d{4} ", f["properties"].get("CITATION", ""))]
        if codes:
            entry["kommunkod"] = max(set(codes), key=codes.count)
    entry["files"] = {name: file_info(os.path.join(site, f"{prefix}-{name}"))
                      for name in ("ptillaten.json.deflate", "servicedagar.json.deflate", "snapshot-meta.json")}


def publish(site, prefix, entry, previous, write):
    """Writes to a staging folder, checks the count against the previous build, then moves the files in."""
    with tempfile.TemporaryDirectory() as staging:
        write(staging)
        describe(staging, prefix, entry)
        old = (previous or {}).get("count")
        if old and entry["count"] < old * (1 - TOLERANCE):
            print(f"  {prefix}: {entry['count']} stretches, was {old}: keeping the previous files", flush=True)
            return False
        for name in os.listdir(staging):
            shutil.move(os.path.join(staging, name), os.path.join(site, name))
    return True


def save_index(site, index):
    index["generatedAt"] = now()
    tmp = os.path.join(site, INDEX + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, os.path.join(site, INDEX))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("site")
    parser.add_argument("--budget-minutes", type=float, default=330)
    parser.add_argument("--only", action="append", help="RDT municipality ids to build (default: stalest first)")
    parser.add_argument("--skip-cities", action="store_true", help="don't rebuild Malmö and Göteborg")
    args = parser.parse_args()
    deadline = time.monotonic() + args.budget_minutes * 60
    os.makedirs(args.site, exist_ok=True)
    index_path = os.path.join(args.site, INDEX)
    index = json.load(open(index_path, encoding="utf-8")) if os.path.exists(index_path) else {"version": 1, "municipalities": {}}
    entries = index["municipalities"]

    if not args.skip_cities:
        stamp = now()
        try:
            fees, cleaning = bm.fetch_json(bm.FEES_URL)["features"], bm.fetch_json(bm.CLEANING_URL)["features"]
            names = bm.fetch_names()
            p, s, _ = bm.build([f for f in fees if f.get("geometry")], [f for f in cleaning if f.get("geometry")], names)
            entry = {"id": "malmo", "name": "Malmö", "kommunkod": "1280", "source": "malmo", "fetchedAt": stamp}
            if publish(args.site, "malmo", entry, entries.get("malmo"), lambda d: bm.write(d, p, s, stamp, prefix="malmo")):
                entries["malmo"] = entry
        except (Exception, SystemExit) as error:   # one source failing (or a missing key) must not stop the others
            print(f"  malmo failed: {error}", flush=True)
        try:
            key = bg.app_id()
            toll, free, zones = (bg.fetch(d, key) for d in bg.DATASETS)
            p, s, _ = bg.build(toll, free, zones)
            entry = {"id": "goteborg", "name": "Göteborg", "kommunkod": "1480", "source": "goteborg", "fetchedAt": stamp}
            if publish(args.site, "goteborg", entry, entries.get("goteborg"), lambda d: bm.write(d, p, s, stamp, prefix="goteborg")):
                entries["goteborg"] = entry
        except (Exception, SystemExit) as error:
            print(f"  goteborg skipped: {error}", flush=True)
        save_index(args.site, index)

    catalogue = probe.live_catalogue()
    enums = rules_mod.build_enums(catalogue)
    todo = [(code, name, slug(name)) for code, name in kommuner(catalogue) if code not in OWN_SOURCES]
    if args.only:
        todo = [t for t in todo if t[2] in args.only]
    attempted = index.setdefault("attempted", {})
    todo.sort(key=lambda t: attempted.get(t[2], ""))       # never tried first, then oldest
    cache = os.path.join(tempfile.gettempdir(), "parkfynd-nvdb-cache.json")
    for code, name, kommun_id in todo:
        if time.monotonic() > deadline:
            print("time budget used", flush=True)
            break
        stamp = now()
        print(f"{kommun_id} ({name}, RDT {code})", flush=True)
        with tempfile.TemporaryDirectory() as pages:
            try:
                probe.cmd_fetch(int(code), pages, None)
                p, s, stats = br.build(kommun_id, pages, enums, cache, name=name)
            except (Exception, SystemExit) as error:
                print(f"  failed: {error}", flush=True)
                continue
        attempted[kommun_id] = stamp
        if not p:
            entries.pop(kommun_id, None)
            print("  no parking stretches with geometry", flush=True)
            save_index(args.site, index)
            continue
        entry = {"id": kommun_id, "name": name, "source": "rdt", "fetchedAt": stamp}
        if publish(args.site, kommun_id, entry, entries.get(kommun_id), lambda d: bm.write(d, p, s, stamp, prefix=kommun_id)):
            entries[kommun_id] = entry
            print(f"  {entry['count']} stretches", flush=True)
        save_index(args.site, index)


if __name__ == "__main__":
    sys.exit(main())
