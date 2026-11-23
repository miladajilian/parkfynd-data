"""Decode RDT parking rules (Mall38) into plain Python values (national-data-spec.md §10).

Used by build-rdt.py. Pure: no network. Input is one exported page (`<GI>` XML from rdt-probe.py)
plus the data catalogue's enum table (`enums.json`, written by `build-rdt.py enums`).

A rule becomes a `Rule` with only what Parkfynd can act on safely. Anything the decoder doesn't
understand sets `time_unknown` or `cleaning_unknown`, which the app shows as "Check the sign".
"""

import dataclasses
import re
import xml.etree.ElementTree as ET

CAR_VEHICLES = {"fordon", "bil", "personbil"}


@dataclasses.dataclass
class Window:
    """A weekly time window. `days` holds ISO weekdays 1–7; None means "day types" we can't place
    on weekdays (vardag, sön- och helgdag …), which is only usable as an uncertain window."""
    days: frozenset | None
    start: int
    end: int
    parity: str | None = None          # "jämna veckor" / "udda veckor"
    season: tuple | None = None        # (startMonth, startDay, endMonth, endDay)
    daytype: str | None = None         # the DAGSLAG text when days is None, e.g. "vardag utom vardag före sön- och helgdag"


@dataclasses.dataclass
class Rule:
    regulation: str                    # "0163 2021:69"
    title: str
    links: list                        # [(element_id, start, end, side)] side: "left" / "right" / None
    street: str | None = None
    text: str = ""
    ban_always: bool = False
    permit: bool = False
    fee: str | None = None             # the taxa reference, e.g. "A"
    fee_hours: list = dataclasses.field(default_factory=list)  # [Window] when the fee applies ("avgiftsplikten gäller …")
    max_minutes: int | None = None
    cleaning: list = dataclasses.field(default_factory=list)   # [Window] bans with known times
    windows: list = dataclasses.field(default_factory=list)    # [Window] every time the rule mentions
    time_unknown: bool = False
    cleaning_unknown: bool = False
    excluded: bool = False             # reserved for other vehicles or permit holders
    area: bool = False                 # covers an area, not a named street


# MARK: - The phrase tree

class Node:
    def __init__(self, name, value=None, children=()):
        self.name, self.value, self.children = name, value, list(children)

    def find(self, suffix):
        for node in self.walk():
            if node is not self and node.name.endswith(suffix):
                return node
        return None

    def find_all(self, suffix):
        return [n for n in self.walk() if n is not self and n.name.endswith(suffix)]

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def text(self):
        return " ".join(str(n.value) for n in self.walk() if n.value not in (None, ""))


def type_name(element):
    t = element.find("typeOf")
    return t.get("uuidref").split(";")[2] if t is not None else "?"


def type_guid(element):
    t = element.find("typeOf")
    return t.get("uuidref").split(";")[-1] if t is not None else ""


def build_enums(catalogue_xml):
    """Data catalogue → {attribute GUID: {code: Swedish text}}. Names differ in case between the
    catalogue and the data (beteckning_MA / Beteckning_MA); the GUID is the same."""
    attribute_domain = {}
    for m in re.finditer(r'<members uuid="RDT_DK;\d+;[^;"]+;([0-9A-F-]+)">(.*?)</members>', catalogue_xml, re.S):
        if "<name>" in m.group(2):
            domains = re.findall(r'<domain idref="(id_[^"]+)"', m.group(2))
            if domains:
                attribute_domain[m.group(1)] = domains[-1]
    domains = {}
    for m in re.finditer(r'<(\w+) id="(id_[0-9A-F-]+)"[^>]*>(.*?)</\1>', catalogue_xml, re.S):
        values = re.findall(r'<FT_ValidEnumeration>\s*<description>(.*?)</description>.*?<code>(\d+)</code>\s*</FT_ValidEnumeration>',
                            m.group(3), re.S)
        if values:
            domains[m.group(2)] = {code: text for text, code in values}
    return {guid: domains[d] for guid, d in attribute_domain.items() if d in domains}


def tree(element, enums):
    """An FI_AttributeInstance / members element → Node, enum codes decoded to their Swedish text."""
    name = type_name(element)
    value, children = None, []
    for v in element.findall("values/*"):
        if v.tag == "FI_ThematicAttributeValue":
            leaf = v.find("value/*")
            if leaf is None:
                continue
            guid = type_guid(element)
            if leaf.tag == "number" and guid in enums:
                value = enums[guid].get(str(int(float(leaf.text))), f"?{leaf.text}")
            else:
                value = leaf.text or ""
        elif v.tag == "FI_StructuredAttributeValue":
            children += [tree(m, enums) for m in v.findall("members")]
    return Node(name, value, children)


# MARK: - Values

def minutes_of(clock):
    """'07.00' → 420, '24.00' → 1440."""
    m = re.match(r"^(\d{1,2})\.(\d{2})$", clock or "")
    if not m:
        raise ValueError(clock)
    return int(m.group(1)) * 60 + int(m.group(2))


def duration_minutes(text):
    m = re.match(r"^(\d+) (min|tim|dygn)$", text or "")
    if not m:
        return None
    return int(m.group(1)) * {"min": 1, "tim": 60, "dygn": 1440}[m.group(2)]


WEEKDAYS = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]
MONTHS = ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september",
          "oktober", "november", "december"]
LAST_DAY = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def day_number(text):
    m = re.match(r"^(\d{1,2})(?::[ae])?$", text or "")
    if not m:
        raise ValueError(text)
    return int(m.group(1))


class Unsupported(Exception):
    pass


def clock_pair(node):
    k1, k2 = node.find("KLOCKSLAG1"), node.find("KLOCKSLAG2")
    if not k1 or not k2:
        raise Unsupported("no clock pair")
    return minutes_of(k1.value), minutes_of(k2.value)


def days_of(node):
    """dagar / veckodag nodes → frozenset of ISO weekdays, or None for day types."""
    spec = node.find("veckodag_specifik_VECKODAG")
    if spec:
        return frozenset([WEEKDAYS.index(spec.value) + 1])
    first, last = node.find("VECKODAG1"), node.find("VECKODAG2")
    if first and last:
        a, b = WEEKDAYS.index(first.value) + 1, WEEKDAYS.index(last.value) + 1
        return frozenset(range(a, b + 1)) if a <= b else frozenset(list(range(a, 8)) + list(range(1, b + 1)))
    if first:
        return frozenset([WEEKDAYS.index(first.value) + 1])
    if node.find("DAGSLAG"):
        return None
    raise Unsupported("no days")


def season_of(node):
    period = node.find("komplex_tid_period")
    if not period:
        return None
    months = period.find("period_månad_intervall")
    if months:
        m1, m2 = months.find("MÅNAD1").value, months.find("MÅNAD2").value
        if m1 not in MONTHS or m2 not in MONTHS:
            raise Unsupported("month text")
        a, b = MONTHS.index(m1) + 1, MONTHS.index(m2) + 1
        return (a, 1, b, LAST_DAY[b - 1])
    dates = period.find("period_dag_månad_intervall")
    if dates:
        m1, m2 = dates.find("MÅNAD1").value, dates.find("MÅNAD2").value
        if m1 not in MONTHS or m2 not in MONTHS:
            raise Unsupported("month text")
        return (MONTHS.index(m1) + 1, day_number(dates.find("DAG1").value),
                MONTHS.index(m2) + 1, day_number(dates.find("DAG2").value))
    raise Unsupported("period")


def windows_of(tid):
    """A `tid` node → [Window]. A slot can hold several parts ("onsdag 18–22" + "udda veckor");
    a lone even/odd-weeks part narrows the others. Raises Unsupported for shapes we don't read."""
    parts = [c for c in tid.children if not c.name.endswith("tid_udda_jämn_tid")]
    parities = [c.find("UDDA_JÄMNA").value for c in tid.children if c.name.endswith("tid_udda_jämn_tid") and c.find("UDDA_JÄMNA")]
    if not parts:
        raise Unsupported("only even/odd")
    if any(p not in ("jämna veckor", "udda veckor") for p in parities) or len(set(parities)) > 1:
        raise Unsupported("parity")
    found = [w for part in parts for w in windows_of_part(part)]
    if parities:
        for w in found:
            if w.parity and w.parity != parities[0]:
                raise Unsupported("parity clash")
            w.parity = parities[0]
    return found


def windows_of_part(node):
    kind = node.name
    if kind.endswith("tid_komplex_tid"):
        dk = node.find("komplex_tid_dagar_klockan_udda_jämna")
        if not dk:
            raise Unsupported("komplex without days")
        parity_node = dk.find("UDDA_JÄMNA")
        parity = parity_node.value if parity_node else None
        if parity and parity not in ("jämna veckor", "udda veckor"):
            raise Unsupported(parity)    # udda/jämna datum
        dagar_klockan = dk.find("dagar_klockan_udda_jämna_dagar_klockan")
        if not dagar_klockan:
            raise Unsupported("no dagar_klockan")
        start, end = clock_pair(dagar_klockan.find("dagar_klockan_klockan"))
        dagar = dagar_klockan.find("dagar_klockan_dagar")
        daytype = dagar.find("DAGSLAG").value if dagar and dagar.find("DAGSLAG") else None
        return [Window(days_of(dagar), start, end, parity, season_of(node), daytype)]
    if kind.endswith("tid_veckodagtid") or kind.endswith("tid_dagslagtid"):
        daytype = node.find("DAGSLAG").value if node.find("DAGSLAG") else None
        if not node.find("KLOCKSLAG1"):
            return [Window(days_of(node), 0, 1440, daytype=daytype)]
        start, end = clock_pair(node)
        return [Window(days_of(node), start, end, daytype=daytype)]
    if kind.endswith("tid_dagslagtider") or kind.endswith("tid_veckodagtider"):
        days = days_of(node)
        daytype = node.find("DAGSLAG").value if node.find("DAGSLAG") else None
        return [Window(days, *clock_pair(k), daytype=daytype) for k in node.find_all("_klockan")]
    if kind.endswith("tid_tidpunkt"):
        return [Window(frozenset(range(1, 8)), *clock_pair(node))]
    if kind.endswith("tid_tidpunkter"):
        return [Window(frozenset(range(1, 8)), *clock_pair(k)) for k in node.find_all("tidpunkter_klockan")]
    raise Unsupported(kind or "empty tid")


# MARK: - Rules

def phrases(feature, enums):
    return [tree(ai, enums) for ai in feature.iter("FI_AttributeInstance") if type_name(ai).startswith("Mall38_Fras")]


def links_of(feature):
    out = []
    for extent in feature.iter("NW_LineExtent"):
        loc = extent.find("locationInstance")
        if loc is None:
            continue
        side = (extent.findtext("lateralPosition") or "").strip() or None
        start = float(extent.findtext("startPosition/NW_LinkPositionRelDist/relativeDistance") or 0)
        end = float(extent.findtext("endPosition/NW_LinkPositionRelDist/relativeDistance") or 1)
        out.append((loc.get("uuidref"), min(start, end), max(start, end), side if side in ("left", "right") else None))
    return out


def vehicle_of(node):
    """The vehicle or user group a ban or permission is for: a class (fordon-mm_B), a special group
    such as permit holders (fordon-mm_C), or free text."""
    b = node.find("fordon-mm_B")
    if b:
        return b.value
    c = node.find("fordon-mm_C")
    if c:
        return c.value
    free = node.find("fordon-mm_FRITEXT")
    return free.value.strip() if free and free.value else None


TID = re.compile(r"(^|_)tid\d?$")


def tid_nodes(node):
    """The time slots directly under a phrase ("Fras121_tid", "Fras119_tid1", "…_parkera_tid"),
    not the inner parts of a time ("tid_komplex_tid")."""
    return [n for n in node.walk() if n is not node and TID.search(n.name) and "komplex" not in n.name]


def apply_tid(rule, node, ban):
    for tid in tid_nodes(node):
        try:
            found = windows_of(tid)
        except (Unsupported, ValueError, AttributeError):
            rule.cleaning_unknown |= ban
            rule.time_unknown |= not ban
            rule.windows.append(None)
            continue
        rule.windows += found
        if ban:
            if all(w.days is not None for w in found):
                rule.cleaning += found
            else:
                rule.cleaning_unknown = True


def decode(feature, enums, regulation, title):
    rule = Rule(regulation=regulation, title=title, links=links_of(feature))
    nodes = phrases(feature, enums)
    rule.text = " | ".join(n.text() for n in nodes)
    for node in nodes:
        fras = node.name.split("_", 1)[1] if "_" in node.name else node.name
        street = node.find("VÄG_gata")
        if street and street.value and not rule.street:
            rule.street = street.value.strip()
        if node.find("läge_område"):
            rule.area = True
        q = node.find("_Q")
        if q and duration_minutes(q.value):
            rule.max_minutes = min(filter(None, [rule.max_minutes, duration_minutes(q.value)]))

        if fras == "Fras272":
            if node.find("tillstånd_för_rörelsehindrad"):
                rule.excluded = True            # bays for disabled-permit holders only
                continue
            ban = node.find("läge_förbud_att_stanna_parkera")
            permit = node.find("läge_tillåtelse_att_stanna_parkera") or node.find("tillåtelse_att_stanna_parkera")
            if ban is None and permit is None:
                rule.time_unknown = True        # a location phrase of a kind we don't read
                continue
            target = ban or permit
            if target is not None and vehicle_of(target) not in (None, *CAR_VEHICLES):
                rule.excluded |= bool(permit)
                continue
            if ban:
                if tid_nodes(ban):
                    apply_tid(rule, ban, ban=True)
                else:
                    rule.ban_always = True
            elif permit:
                rule.permit = True
                if permit.find("AHC"):
                    rule.fee = rule.fee or "?"
                apply_tid(rule, permit, ban=False)
        elif fras == "Fras198":
            if node.find("tillstånd_för_rörelsehindrad"):
                rule.excluded = True
            ban = node.find("förbud_att_stanna_parkera")
            permit = node.find("tillåtelse_att_stanna_parkera")
            if ban:
                if vehicle_of(ban) not in (None, *CAR_VEHICLES):
                    continue
                if tid_nodes(ban):
                    apply_tid(rule, ban, ban=True)
                else:
                    rule.ban_always = True
            if permit:
                if vehicle_of(permit) not in (None, *CAR_VEHICLES):
                    rule.excluded = True
                rule.permit = True
                if permit.find("AHC"):
                    rule.fee = rule.fee or "?"
        elif fras == "Fras121":
            allowed = (node.find("Fras121_OA").value if node.find("Fras121_OA") else "") == "tillåten"
            has_time = bool(tid_nodes(node))
            if allowed:
                rule.permit = True
                if has_time and not q:
                    rule.time_unknown = True    # "parking allowed <only at these times>"
                apply_tid(rule, node, ban=False)
            elif has_time:
                apply_tid(rule, node, ban=True)
            else:
                rule.ban_always = True
        elif fras in ("Fras127", "Fras128"):
            aha = node.find("ptaxa_AHA")
            rule.fee = (aha.value or "?").split(",")[0].strip() if aha else "?"
        elif fras == "Fras118":
            af = node.find("Fras118_AF")
            vehicle = vehicle_of(node) or (node.find("Fras118_B").value if node.find("Fras118_B") else None)
            mode = af.value if af else ""
            if mode == "får endast" and vehicle not in CAR_VEHICLES:
                rule.excluded = True
            elif mode == "får inte" and vehicle in CAR_VEHICLES:
                if tid_nodes(node):
                    apply_tid(rule, node, ban=True)
                else:
                    rule.ban_always = True
            elif mode in ("får", "får endast"):
                rule.permit = True
        elif fras == "Fras132":
            ab = node.find("Fras132_AB")
            if ab and ab.value == "endast":
                rule.excluded = True
        elif fras in ("Fras119", "Fras199", "Fras200"):
            # When a time limit or fee applies. The max (Q) is already taken; the hours only narrow it,
            # so ignoring them is conservative. Record them for uncertain windows.
            # Fee hours: Fras200 ("N § gäller …" next to a fee phrase citing the same paragraph, as in
            # 0163 2021:94 "Avgiftsplikten gäller vardag utom … 09.00 - 19.00") or Fras119 "Avgiftsplikten".
            ada = node.find("Fras119_ADA")
            fee_hours = fras == "Fras200" or (ada is not None and "vgiftsplikten" in (ada.value or ""))
            found = []
            for tid in tid_nodes(node):
                try:
                    found += windows_of(tid)
                except (Unsupported, ValueError, AttributeError):
                    rule.windows.append(None)
                    fee_hours = False
            rule.windows += found
            if fee_hours and found:
                rule.fee_hours += found
        elif fras in ("Fras288", "Fras290"):
            pass                            # "the ban in § N doesn't apply …": refers to another paragraph
        elif fras == "Fras222" and re.match(r"^\s*(Förbudet|Föreskriften|Bestämmelsen) gäller (dock )?inte\b", node.text()):
            pass                            # free text that only narrows a ban
        else:
            rule.time_unknown = True        # other free text (Fras222) and templates we don't read
    if rule.fee and not rule.permit and not rule.ban_always:
        rule.permit = True
    return rule


def page_rules(path, enums):
    """Every Mall38 rule on one exported page, with its regulation designation and heading."""
    root = ET.parse(path).getroot()
    features = {}
    for element in root.iter():
        if element.tag.startswith("FI_ChangedFeature"):
            features[element.get("id")] = element

    def associated(element, suffix):
        for a in element.iter("FI_AssociationInstance"):
            if type_name(a).endswith(suffix):
                target = a.find("associationTo")
                if target is not None:
                    return features.get(target.get("idref"))
        return None

    def attribute(element, name):
        for ai in element.iter("FI_AttributeInstance"):
            if type_name(ai) == name:
                for leaf in ai.iter():
                    if leaf.tag in ("string", "number") and leaf.text:
                        return leaf.text.strip()
        return None

    def decoded(member):
        leaf = next((x for x in member.iter() if x.tag in ("string", "number") and x.text), None)
        if leaf is None:
            return None
        table = enums.get(type_guid(member))
        if leaf.tag == "number" and table:
            return table.get(str(int(float(leaf.text))), leaf.text)
        return leaf.text.strip()

    def designation_of(regulation):
        """MA/MB/MC from the regulation's own heading (Trafikföreskrift_Rubrik → Rubrik_Beteckning),
        not from a regulation it replaces or repeals."""
        for ai in regulation.iter("FI_AttributeInstance"):
            if type_name(ai) != "Trafikföreskrift_Rubrik":
                continue
            values = {}
            for member in ai.iter("members"):
                name = type_name(member)
                if name in ("Beteckning_MA", "Beteckning_MB", "Beteckning_MC"):
                    value = decoded(member)
                    if value is not None:
                        values.setdefault(name[-2:], value)
            if len(values) == 3:
                # MA decodes to e.g. "3;0184" (authority type;code); keep the four-digit code.
                authority = values["MA"].split(";")[-1].strip()
                year = int(float(values["MB"])) if re.match(r"^[\d.]+$", values["MB"]) else values["MB"]
                return f"{authority} {year}:{values['MC']}"
        return ""

    for element in features.values():
        if type_name(element) != "Mall38":
            continue
        rule_type = associated(element, "_Trafikregeltyp12")
        regulation = associated(rule_type, "_Trafikföreskrift") if rule_type is not None else None
        designation, title = "", ""
        if regulation is not None:
            designation = designation_of(regulation)
            title = attribute(regulation, "Rubrik_RubrikSomText") or ""
        yield decode(element, enums, designation, title)
