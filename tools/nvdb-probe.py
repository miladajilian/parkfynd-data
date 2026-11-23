#!/usr/bin/env python3
"""Check whether Trafikverket's open API gives the NVDB road links that RDT parking rules point at.

M11 spike tool (docs/national-data-spec.md §6.1). It is not used by the app.

    TRAFIKVERKET_API_KEY=… python3 docs/tools/nvdb-probe.py docs/fixtures/rdt/*.xml

The key comes from the environment, or else from `TRAFIKVERKET_API_KEY = …` in the git-ignored
Config/Secrets.xcconfig. It is never printed.

It downloads `Vägtrafiknät` (namespace `vägdata.nvdb_dk_o`, schema 1.2) for a box around Sollentuna
and Solna, prints the fields of one record, and reports how many RDT `locationInstance` values
(e.g. `3:1044452`) it can find among the record values.
"""

import json
import os
import re
import sys
import urllib.request

API = "https://api.trafikinfo.trafikverket.se/v2/data.json"
NAMESPACE = "vägdata.nvdb_dk_o"
SCHEMA = "1.2"
PAGE = 10_000
# SWEREF 99 TM box covering Sollentuna and Solna (approximate, generous).
BOX = (658_000, 6_578_000, 676_000, 6_600_000)
SECRETS = os.path.join(os.path.dirname(__file__), "..", "..", "Config", "Secrets.xcconfig")


def api_key() -> str:
    key = os.environ.get("TRAFIKVERKET_API_KEY", "").strip()
    if not key and os.path.exists(SECRETS):
        for line in open(SECRETS, encoding="utf-8"):
            match = re.match(r"\s*TRAFIKVERKET_API_KEY\s*=\s*(\S+)", line)
            if match:
                key = match.group(1)
    if not key:
        raise SystemExit("No TRAFIKVERKET_API_KEY in the environment or Config/Secrets.xcconfig")
    return key


def query(key: str, objecttype: str, skip: int) -> list:
    xmin, ymin, xmax, ymax = BOX
    body = (
        f'<REQUEST><LOGIN authenticationkey="{key}"/>'
        f'<QUERY objecttype="{objecttype}" namespace="{NAMESPACE}" schemaversion="{SCHEMA}" limit="{PAGE}" skip="{skip}">'
        f'<FILTER><INTERSECTS name="Geometry.WKT-SWEREF99TM-3D" shape="box" value="{xmin} {ymin}, {xmax} {ymax}"/></FILTER>'
        "</QUERY></REQUEST>"
    )
    request = urllib.request.Request(API, data=body.encode("utf-8"), headers={
        "Content-Type": "text/xml; charset=utf-8", "User-Agent": "Parkfynd-nvdb-probe"})
    with urllib.request.urlopen(request, timeout=180) as response:
        answer = json.load(response)
    result = answer["RESPONSE"]["RESULT"][0]
    if "ERROR" in result:
        raise SystemExit(f"API error: {result['ERROR']}")
    return result.get(objecttype, [])


def flatten(value, prefix=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from flatten(v, f"{prefix}{k}.")
    elif isinstance(value, list):
        for v in value:
            yield from flatten(v, prefix)
    else:
        yield prefix.rstrip("."), value


def main() -> None:
    rdt_refs = set()
    for path in sys.argv[1:]:
        rdt_refs.update(re.findall(r'<locationInstance uuidref="([^"]+)"', open(path, encoding="utf-8").read()))
    print(f"RDT link refs in fixtures: {len(rdt_refs)} (e.g. {sorted(rdt_refs)[:3]})")

    key = api_key()
    records, skip = [], 0
    while True:
        page = query(key, "Vägtrafiknät", skip)
        records += page
        if len(page) < PAGE:
            break
        skip += PAGE
    print(f"Vägtrafiknät records in box: {len(records)}")
    if not records:
        return
    print("fields of the first record:")
    for name, value in flatten(records[0]):
        text = str(value)
        print(f"  {name:40} {text[:70]}{'…' if len(text) > 70 else ''}")

    # Try to match RDT refs `<a>:<b>` against any value, whole or by the part after the colon.
    values = {}
    for record in records:
        for name, value in flatten(record):
            if not name.startswith("Geometry"):
                values.setdefault(str(value), set()).add(name)
    whole = {ref for ref in rdt_refs if ref in values}
    tail = {ref for ref in rdt_refs if ref.split(":")[-1] in values}
    print(f"RDT refs found as a whole value: {len(whole)}; by the number after ':': {len(tail)}")
    fields = set()
    for ref in whole:
        fields |= values[ref]
    for ref in tail:
        fields |= values[ref.split(":")[-1]]
    print(f"fields holding the matches: {sorted(fields)}")
    missing = sorted(rdt_refs - whole - tail)
    by_prefix = {}
    for ref in missing:
        by_prefix[ref.split(":")[0]] = by_prefix.get(ref.split(":")[0], 0) + 1
    print(f"not found: {len(missing)} by prefix {by_prefix} (e.g. {missing[:5]})")
    pieces = {}
    for record in records:
        pieces[record.get("Element_Id")] = pieces.get(record.get("Element_Id"), 0) + 1
    print(f"records per Element_Id: max {max(pieces.values())}, "
          f"{sum(1 for n in pieces.values() if n > 1)} of {len(pieces)} links are split into measure ranges")


if __name__ == "__main__":
    main()
