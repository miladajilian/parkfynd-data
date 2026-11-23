#!/usr/bin/env python3
"""Probe Transportstyrelsen's national register of traffic rules (RDT / STFS) for one municipality.

This is the Phase 0 spike tool for nationwide data (docs/national-data-spec.md). It is not used by the app.

    python3 docs/tools/rdt-probe.py katalog OUT_DIR                # save the data catalogue (about 3 MB)
    python3 docs/tools/rdt-probe.py kommun Sollentuna              # print the catalogue enum for a kommun
    python3 docs/tools/rdt-probe.py fetch 32 OUT_DIR [--max-pages N] # fetch every rule in force for kommun enum 32
    python3 docs/tools/rdt-probe.py summary OUT_DIR                 # count rule types, parking rules, NVDB links

The service is public and needs no key:
    https://rdt.transportstyrelsen.se/rdtws/ExternalWebService.asmx  (SOAP 1.1, namespace http://www.vv.se/rdt/)

Bulk export follows "RDT – Formatspecifikation för XML" (2021-03-29) chapter 5: one
`BeräknaAntalSökDelresultat` call gives the page count, then `HämtaSökDelresultat` is called once per page.
Both requests are wrapped in a `<GI>` envelope with one `<CR_ChangeTransaction>`.
"""

import argparse
import collections
import glob
import html
import os
import re
import sys
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

URL = "https://rdt.transportstyrelsen.se/rdtws/ExternalWebService.asmx"
NS = "http://www.vv.se/rdt/"
TIMEOUT = 120
RETRIES = 4
PAUSE_SECONDS = 1.0  # be gentle: one request at a time with a pause

# Rule types that matter for street parking (from the data catalogue, version 35).
PARKING_RULE_TYPES = {
    "Trafikregeltyp12": "stannande och parkering (TrF 10:1 p16)",
    "Trafikregeltyp13": "villkor för parkering (TrF 10:1 p17)",
    "Mall25": "parkering av fordon med farligt gods",
    "Mall56": "parkeringstaxor",
    "Mall57": "ändrade parkeringstaxor",
}
FREE_TEXT_TYPE = "Trafikregeltyp22"  # "kan inte maskinellt tolkas"

GI_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?><GI version="1.0.0"><exchangeMetadata><datasetCitation>'
    "<title>Parkfynd</title><date><date>{today}</date><dateType>Creation</dateType></date>"
    "<citedResponsibleParty><organisationName>Parkfynd</organisationName><role>user</role></citedResponsibleParty>"
    "</datasetCitation><applicationSchemaCitation><title>SS 63 70 04</title><date><date>2005-10-01</date>"
    "<dateType>Revision</dateType></date><citedResponsibleParty><organisationName>SIS/Stanli</organisationName>"
    "<role>Publisher</role></citedResponsibleParty></applicationSchemaCitation><encoding><ruleCitation>"
    "<title>SS-EN ISO 19118:2006</title><date><date>2006-07-07</date><dateType>Revision</dateType></date>"
    "</ruleCitation><toolName>Parkfynd</toolName><toolVersion>1.0</toolVersion></encoding></exchangeMetadata>"
)


def soap(method: str, inner: str) -> str:
    body = (
        '<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<soap:Body><{method} xmlns="{NS}">{inner}</{method}></soap:Body></soap:Envelope>'
    )
    request = urllib.request.Request(URL, data=body.encode("utf-8"), headers={
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": f'"{NS}{method}"',
        "User-Agent": "Parkfynd-rdt-probe",
    })
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                text = response.read().decode("utf-8")
            break
        except (TimeoutError, OSError) as error:  # URLError is an OSError
            if attempt == RETRIES - 1:
                raise
            wait = 10 * 2 ** attempt
            print(f"  {method}: {error}; retrying in {wait} s", file=sys.stderr, flush=True)
            time.sleep(wait)
    match = re.search(rf"<{method}Result>(.*)</{method}Result>", text, re.S)
    if not match:
        raise SystemExit(f"{method}: unexpected answer: {text[:300]}")
    return html.unescape(match.group(1))


def transaction(kind: str, params: list) -> str:
    today = time.strftime("%Y-%m-%d")
    items = "".join(
        f"<transactionInformation><tag>{k}</tag><value>{escape(str(v))}</value></transactionInformation>"
        for k, v in [("TransaktionsTyp", kind)] + params
    )
    tx_id = str(uuid.uuid4()).upper()
    return GI_HEAD.format(today=today) + (
        f"<dataset><CR_ChangeTransaction><transactionid>{tx_id}</transactionid>{items}"
        "</CR_ChangeTransaction></dataset></GI>"
    )


def search_params(catalogue_version: int, kommun: int) -> list:
    return [
        ("DatakatalogVersionNummer", catalogue_version),
        ("EndastSaknarStruVna", 0),
        ("BifogaDokument", 0),
        ("BerörLänId", -1),
        ("BerörKommunId", kommun),
        ("BerörOrtId", -1),
        ("PubliceradeAvBeslutsmyndighetTypId", -1),
        ("PubliceradeAvBeslutsmyndighetId", -1),
        ("PubliceradeAvSkaTasMed", 1),
        ("BeteckningBeslutsmyndighetKod", ""),
        ("BeteckningÅr", -1),
        ("BeteckningLöpNr", ""),
        ("OmfattningAllaNuGällande", 1),  # full extract of everything in force
        ("OmfattningFörändringarSen", "2000-01-01T00:00:00"),
    ]


def tag_value(xml: str, tag: str) -> str | None:
    match = re.search(rf"<tag>{tag}</tag>\s*<value>(.*?)</value>", xml, re.S)
    return match.group(1) if match else None


def check_status(xml: str, what: str) -> None:
    if tag_value(xml, "Status") == "Fel":
        raise SystemExit(f"{what}: RDT answered Fel: {tag_value(xml, 'Kommentar')}")


def catalogue_version(katalog_xml: str) -> int:
    return int(re.search(r"<versionNumber>(\d+)</versionNumber>", katalog_xml).group(1))


def cmd_katalog(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    xml = soap("HamtaDataKatalog", "")
    path = os.path.join(out_dir, "datakatalog.xml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml)
    print(f"catalogue version {catalogue_version(xml)} → {path}")


def live_catalogue() -> str:
    return soap("HamtaDataKatalog", "")


def cmd_kommun(name: str) -> None:
    xml = live_catalogue()
    for match in re.finditer(r"<description>3;([^<]*?) kommun</description>\s*<code>(\d+)</code>", xml):
        if name.lower() in match.group(1).lower():
            print(f"{match.group(1)}: BerörKommunId={match.group(2)}")


def cmd_fetch(kommun: int, out_dir: str, max_pages: int | None) -> None:
    os.makedirs(out_dir, exist_ok=True)
    version = catalogue_version(live_catalogue())
    count_xml = soap("HamtaForeteelser", f"<ssXml>{escape(transaction('BeräknaAntalSökDelresultat', search_params(version, kommun)))}</ssXml>")
    check_status(count_xml, "count")
    pages = int(tag_value(count_xml, "AntalSökDelresultat"))
    per_page = tag_value(count_xml, "MaxAntalFöreskrifterPerDelresultat")
    print(f"kommun {kommun}: {pages} pages of at most {per_page} rules (catalogue {version})")
    last = pages if max_pages is None else min(pages, max_pages)
    started = time.time()
    for page in range(1, last + 1):
        path = os.path.join(out_dir, f"page-{page:04d}.xml")
        if os.path.exists(path):
            continue  # resumable
        params = search_params(version, kommun)
        params.insert(1, ("Delresultat", page))
        xml = soap("HamtaForeteelser", f"<ssXml>{escape(transaction('HämtaSökDelresultat', params))}</ssXml>")
        check_status(xml, f"page {page}")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(xml)
        os.replace(tmp, path)
        if page % 20 == 0:
            print(f"  {page}/{last} pages, {time.time() - started:.0f} s", flush=True)
        time.sleep(PAUSE_SECONDS)
    print(f"done: {last} pages in {time.time() - started:.0f} s → {out_dir}")


def type_name(element) -> str:
    """`RDT_DK;35;Mall38;…` → `Mall38`."""
    type_of = element.find("typeOf")
    return type_of.get("uuidref").split(";")[2] if type_of is not None else "?"


def features(path: str):
    """Yield every top-level feature (a regulation, a rule type or a template) on one page."""
    root = ET.parse(path).getroot()
    for element in root.iter():
        if element.tag in ("FI_ChangedFeatureWithHistory", "FI_ChangedFeatureWithoutHistory"):
            yield element


def cmd_summary(out_dir: str) -> None:
    files = sorted(glob.glob(os.path.join(out_dir, "page-*.xml")))
    kinds = collections.Counter()       # Trafikföreskrift, TrafikregeltypNN, MallNN
    templates = collections.Counter()   # MallNN → count, for templates that belong to a parking rule type
    phrases = collections.Counter()     # FrasNNN_* phrase names inside parking templates
    linked = 0
    unlinked = 0
    links = set()
    for path in files:
        for feature in features(path):
            name = type_name(feature)
            kinds[name if not name.startswith("Mall") else "Mall*"] += 1
            if not name.startswith("Mall"):
                continue
            associations = {type_name(a).split("_", 1)[-1] for a in feature.iter("FI_AssociationInstance")}
            if not associations & {"Trafikregeltyp12", "Trafikregeltyp13"} and name not in PARKING_RULE_TYPES:
                continue
            templates[name] += 1
            for member in feature.iter("typeOf"):
                part = member.get("uuidref").split(";")[2]
                if part.startswith("Fras") and "_" in part:
                    phrases[part] += 1
            refs = [loc.get("uuidref") for loc in feature.iter("locationInstance")]
            links.update(refs)
            if refs:
                linked += 1
            else:
                unlinked += 1
    print(f"pages: {len(files)}  regulations: {kinds['Trafikföreskrift']}")
    print("rule types:")
    for name, n in sorted(kinds.items()):
        if name.startswith("Trafikregeltyp"):
            label = PARKING_RULE_TYPES.get(name, "free text, not machine-readable" if name == FREE_TEXT_TYPE else "")
            print(f"  {name:20} {n:6}  {label}")
    print(f"parking templates: {sum(templates.values())}  with NVDB link refs: {linked}  without: {unlinked}  "
          f"distinct links: {len(links)}")
    for name, n in templates.most_common():
        print(f"  {name:10} {n:6}")
    print("most common phrases in parking templates:")
    for name, n in phrases.most_common(25):
        print(f"  {name:55} {n:6}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("katalog"); p.add_argument("out_dir")
    p = sub.add_parser("kommun"); p.add_argument("name")
    p = sub.add_parser("fetch"); p.add_argument("kommun", type=int); p.add_argument("out_dir"); p.add_argument("--max-pages", type=int)
    p = sub.add_parser("summary"); p.add_argument("out_dir")
    args = parser.parse_args()
    if args.cmd == "katalog":
        cmd_katalog(args.out_dir)
    elif args.cmd == "kommun":
        cmd_kommun(args.name)
    elif args.cmd == "fetch":
        cmd_fetch(args.kommun, args.out_dir, args.max_pages)
    else:
        cmd_summary(args.out_dir)


if __name__ == "__main__":
    sys.exit(main())
