#!/usr/bin/env python3
"""Build the static half of the bathing water tool: who the beaches are, where
the outfalls are, and which outfalls sit near which beach.

This is the SLOW, RARELY-RUN half. Designated bathing waters change once a year;
outfall locations barely move. Running this monthly is plenty.

Doing the distance matching here rather than at request time is the whole trick
that keeps the live side cheap: 941 beaches against ~18,600 outfalls is millions
of comparisons, which is nothing on a laptop once a month and impossible inside
a 10ms Cloudflare Worker.

Writes:
    swim/sites.json      every designated bathing water + coords + classification
    swim/outfalls.json   only outfalls within reach of a beach, with coords
    swim/nearby.json     beach id -> [[outfall key, distance km], ...]

Usage:  python3 tools/build_swim_register.py [--quick] [--force]
        --quick skips the outfall half and refreshes the beach register only.
        --force writes even when a feed failed or the result shrank (see write()).
"""
import gzip
import io
import json
import math
import os
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import places
import swim_sources as S
from swim_irishgrid import irish_grid_to_wgs84

# Same two-homes problem as the collector: this file lives in the website repo
# under tools/, and is also published in the standalone collector repo where
# there is no swim/ directory above it.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("SWIM_OUT") or (
    os.path.join(ROOT, "swim") if os.path.isdir(os.path.join(ROOT, "swim")) else os.getcwd())

# How far a spill can be from a beach and still matter. 5km is generous for a
# river outfall and about right for the coast; the page shows the distance so a
# 4.8km match can be judged on its merits rather than silently colouring a dot.
RADIUS_KM = 5.0

CTX = ssl.create_default_context()


def fetch(url, tries=3, timeout=90):
    """GET a URL and return bytes. Retries, because these are public services
    that occasionally 502 under load and a whole rebuild should not die for it."""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": S.USER_AGENT,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            })
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return raw
        except Exception as e:                      # noqa: BLE001 - report and retry
            last = e
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError("failed after %d tries: %s (%s)" % (tries, url[:90], last))


def fetch_json(url, **kw):
    return json.loads(fetch(url, **kw).decode("utf-8", "replace"))


def arcgis_count(base, where="1=1"):
    """What the service says it holds, so a short read can be spotted."""
    try:
        d = fetch_json(base + "?" + urllib.parse.urlencode(
            {"where": where, "returnCountOnly": "true", "f": "json"}))
        return d.get("count")
    except Exception:                               # noqa: BLE001
        return None


def arcgis_all(base, fields="*", geometry=False, where="1=1", page=1000, expect=None):
    """Page through an ArcGIS FeatureServer until it stops truncating.

    ArcGIS answers a plain query with at most maxRecordCount features and sets
    exceededTransferLimit=true. Several of these services hold more than that
    (Severn Trent 2412, United Utilities 2252), so a single call silently loses
    hundreds of outfalls — including, potentially, the one that is discharging.
    """
    out, offset = [], 0
    while True:
        url = base + "?" + urllib.parse.urlencode({
            "where": where,
            "outFields": fields,
            "returnGeometry": "true" if geometry else "false",
            "outSR": "4326",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": page,
        })
        d = fetch_json(url)
        if "error" in d:
            raise RuntimeError("ArcGIS error: %s" % json.dumps(d["error"])[:200])
        feats = d.get("features", [])
        out.extend(feats)
        if len(feats) < page and not d.get("exceededTransferLimit"):
            break
        if not feats:
            break
        offset += len(feats)
        if offset > 60000:                          # runaway guard
            break
    if expect is not None and len(out) < expect:
        # A short read here is the dangerous one: the outfalls that go missing
        # take their beaches' nearby lists with them, and the live side then
        # reports "nothing within 2km" — a green tick standing on absent data.
        raise RuntimeError("truncated: got %d of %d records" % (len(out), expect))
    return out


def attr(rec, *names):
    """Read a field case-insensitively.

    South West Water uses lowerCamelCase where Thames and Wessex use TitleCase,
    and Anglian spells its object id 'ObjectId' where everyone else shouts
    'OBJECTID'. One adapter, so the differences get absorbed here.
    """
    a = rec.get("attributes", rec)
    lower = {k.lower(): v for k, v in a.items()}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def lit(v):
    """Unwrap a linked-data literal.

    environment.data.gov.uk does not return plain strings. A name arrives as
    {"_value": "Spittal", "_lang": "en"}, a district as a LIST whose first entry
    is an object with a wrapped name inside it, and a type as a list of URIs.
    Reading these naively yields dicts where the page expects text, which then
    sorts, renders and compares in nonsense ways.
    """
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, dict):
        if "_value" in v:
            return lit(v["_value"])
        if "name" in v:
            return lit(v["name"])
        if "label" in v:
            return lit(v["label"])
        return None
    if isinstance(v, (list, tuple)):
        for item in v:
            got = lit(item)
            if got:
                return got
        return None
    return str(v)


def year_from_uri(obj):
    """Pull the year out of a compliance URI: .../point/03600/year/2025."""
    about = obj.get("_about") if isinstance(obj, dict) else None
    if not about:
        return None
    parts = str(about).rstrip("/").split("/")
    for i, p in enumerate(parts):
        if p == "year" and i + 1 < len(parts) and parts[i + 1].isdigit():
            return int(parts[i + 1])
    return None


def type_from_uris(v):
    """The type is a list of URIs; the useful one is the specific subtype."""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        for u in v:
            tail = str(u).rstrip("/").split("/")[-1]
            if tail and tail != "BathingWater":
                return tail
    return None


def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def valid(lat, lon):
    """Inside a box around Britain and Ireland, and not a null-island placeholder."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (49.0 <= lat <= 61.5 and -11.5 <= lon <= 2.5):
        return None
    return (round(lat, 5), round(lon, 5))


# ---------------------------------------------------------------------------
# The beach registers
# ---------------------------------------------------------------------------

def sites_england():
    d = fetch_json(S.EA_SITES)
    out = []
    for it in d["result"]["items"]:
        sp = it.get("samplingPoint") or {}
        ll = valid(sp.get("lat"), sp.get("long"))
        if not ll:
            continue
        ca = it.get("latestComplianceAssessment") or {}
        out.append({
            "id": "E:" + str(it["eubwidNotation"]),
            "name": tidy_name(lit(it.get("name")) or str(it["eubwidNotation"])),
            "country": "England",
            "lat": ll[0], "lon": ll[1],
            "cls": _tidy_class(lit(ca.get("complianceClassification"))),
            "clsYear": ca.get("year") or year_from_uri(ca),
            "kind": _kind(type_from_uris(it.get("type"))),
            "district": lit(it.get("district")),
            "rainRisk": bool(it.get("waterQualityImpactedByHeavyRain")),
            "eaRegion": lit(it.get("regionalOrganization")),
            # The regulator's own page for this beach, so every page we publish
            # can point at the source rather than asking people to take our word.
            "url": "https://environment.data.gov.uk/bwq/profiles/profile.html?site=%s"
                   % it["eubwidNotation"],
        })
    return out


def sites_england_gone():
    """The English bathing waters that were de-designated, and are still swum at.

    THE SILENCE THIS BREAKS. "Designated bathing water" is a legal status, not a
    list of where people swim, and a beach can be taken off it. When that
    happens the Environment Agency's ordinary list endpoint stops returning it —
    but the record stays live in the linked data, with its coordinates, the year
    it went, and the EA's own reason in plain English. So these places simply
    vanish from every map built off the normal feed, including this one, while
    people carry on swimming there.

    Four of the fourteen carry PERMANENT ADVICE AGAINST BATHING, which is the
    strongest standing "do not swim here" the Environment Agency issues. The
    worst of them for this site is Ilfracombe Wildersmouth: it sits between
    Ilfracombe Tunnels and Ilfracombe Hele, both still designated and both
    already on our map, so a visitor sees a pin either side of the harbour and
    swims at the one in the middle that has no pin at all.

    The Republic of Ireland already does this properly — the EPA keeps
    de-classified beaches in the public beaches.ie feed as other monitored
    waters, which is why Merrion Strand is already in our 240. This mirrors that
    onto England's orphans.

    These are NEVER given a water-quality verdict. Nobody samples them any more,
    and that is the whole point: the page has to say that nobody is testing,
    rather than leave a gap that reads as nothing to worry about.
    """
    ids = _dedesignated_ids()
    if not ids:
        print("    de-designated: none returned")
        return []
    out = []
    for sid in ids:
        try:
            d = fetch_json("https://environment.data.gov.uk/doc/bathing-water/%s.json" % sid)
            it = d["result"]["primaryTopic"]
        except Exception as e:                      # noqa: BLE001
            print("    de-designated %s failed: %s" % (sid, str(e)[:60]))
            continue
        sp = it.get("samplingPoint") or {}
        ll = valid(sp.get("lat"), sp.get("long"))
        if not ll:
            continue
        reason = lit(it.get("dedesignationReasonText")) or ""
        year = year_from_uri({"year": it.get("yearDedesignated")}) \
            or str(lit(it.get("yearDedesignated")) or "")[-4:]
        out.append({
            "id": "E:" + str(it.get("eubwidNotation") or sid),
            "name": tidy_name(lit(it.get("name")) or sid),
            "country": "England",
            "lat": ll[0], "lon": ll[1],
            # No classification and no year: it is not classified any more, and
            # printing the last one it ever had would be presenting a grade from
            # years ago as if it still meant something.
            "cls": None, "clsYear": None,
            "kind": _kind(type_from_uris(it.get("type"))),
            "district": lit(it.get("district")),
            "rainRisk": False,
            "eaRegion": lit(it.get("regionalOrganization")),
            "gone": year or True,
            "goneWhy": reason,
            # The four with a standing regulatory instruction not to swim, which
            # is a different thing from merely being unmonitored and has to look
            # different on the page.
            "goneWarn": "permanent advice against bathing" in reason.lower(),
            "url": "https://environment.data.gov.uk/bwq/profiles/profile.html?site=%s"
                   % (it.get("eubwidNotation") or sid),
        })
    warn = sum(1 for x in out if x["goneWarn"])
    print("    de-designated %4d sites (%d with permanent advice against bathing)"
          % (len(out), warn))
    return out


def _dedesignated_ids():
    """Ask the EA's SPARQL endpoint, because the list endpoint drops these."""
    q = ("PREFIX bw: <http://environment.data.gov.uk/def/bathing-water/> "
         "SELECT ?bw WHERE { ?bw a bw:BathingWater ; bw:yearDedesignated ?y } ORDER BY ?bw")
    url = ("https://environment.data.gov.uk/sparql/bwq/query?"
           + urllib.parse.urlencode({"query": q, "output": "json"}))
    try:
        d = fetch_json(url)
    except Exception as e:                          # noqa: BLE001
        print("    de-designated lookup failed: %s" % str(e)[:80])
        return []
    return [b["bw"]["value"].rstrip("/").split("/")[-1]
            for b in d.get("results", {}).get("bindings", [])]


def sites_wales():
    d = fetch_json(S.NRW_SITES)
    out = []
    for it in d["result"]["items"]:
        sp = it.get("samplingPoint") or {}
        ll = valid(sp.get("lat"), sp.get("long"))
        if not ll:
            continue
        ca = it.get("latestComplianceAssessment") or {}
        out.append({
            "id": "W:" + str(it["eubwidNotation"]),
            "name": tidy_name(lit(it.get("name")) or str(it["eubwidNotation"])),
            "country": "Wales",
            "lat": ll[0], "lon": ll[1],
            "cls": _tidy_class(lit(ca.get("complianceClassification"))),
            "clsYear": ca.get("year") or year_from_uri(ca),
            "kind": _kind(type_from_uris(it.get("type"))),
            "district": lit(it.get("district")),
            "rainRisk": bool(it.get("waterQualityImpactedByHeavyRain")),
            "url": "https://environment.data.gov.uk/wales/bathing-waters/profiles/"
                   "profile.html?site=%s" % it["eubwidNotation"],
        })
    return out


def class_history(sites):
    """The last few years of classification for each English and Welsh site.

    A single grade says "Poor" and stops. Poor for the fourth year running and
    Poor after three years of Good are different situations, and the beach page
    could not tell them apart. The registers only ever hand over the latest
    assessment, so the history has to be asked for separately, one year at a
    time.

    England and Wales only. Scotland's feed carries a single current class,
    Northern Ireland has no annual classification at all, and the Republic's
    comes from a different body in a different shape. Rather than half-fill the
    trend and let it look complete, the page shows it only where there is a real
    series behind it.
    """
    hist = defaultdict(dict)
    this_year = int(time.strftime("%Y"))
    for back in range(1, S.CLASS_YEARS + 1):
        year = this_year - back
        for prefix, url in (("E:", S.EA_CLASS_YEAR), ("W:", S.NRW_CLASS_YEAR)):
            try:
                d = fetch_json(url.format(year=year), tries=2, timeout=90)
            except Exception as e:                  # noqa: BLE001
                print("    %s %d classifications unavailable: %s"
                      % (prefix.rstrip(":"), year, str(e)[:60]))
                continue
            for it in (d.get("result") or {}).get("items", []):
                bw = it.get("bwq_bathingWater") or {}
                eub = bw.get("eubwidNotation")
                cc = it.get("complianceClassification") or {}
                name = lit(cc.get("name"))
                if eub and name:
                    hist[prefix + str(eub)][year] = _tidy_class(name)

    got = 0
    for s in sites:
        rows = hist.get(s["id"])
        if not rows:
            continue
        # Newest first, and only where there is more than one year to compare.
        series = [[y, rows[y]] for y in sorted(rows, reverse=True)]
        if len(series) > 1:
            s["clsHist"] = series
            got += 1
    print("    %d sites with a classification history" % got)
    return got


def sites_scotland():
    d = fetch_json(S.SEPA_SITES)
    out = []
    for f in d.get("features", []):
        g = (f.get("geometry") or {}).get("coordinates") or []
        if len(g) < 2:
            continue
        ll = valid(g[1], g[0])
        if not ll:
            continue
        p = f.get("properties") or {}
        name = p.get("description") or "Unnamed"
        out.append({
            "id": "S:" + _slug(name),
            "name": tidy_name(name),
            "country": "Scotland",
            "lat": ll[0], "lon": ll[1],
            "cls": _tidy_class(p.get("class_description")),
            "clsYear": p.get("year"),
            "kind": "Coastal",
            "district": None,
            "rainRisk": False,
            "url": p.get("bw_url"),
        })
    return out


def sites_ni():
    d = fetch_json(S.DAERA_SITES)
    out = []
    for f in d.get("features", []):
        g = (f.get("geometry") or {}).get("coordinates") or []
        if len(g) < 2:
            continue
        ll = valid(g[1], g[0])
        if not ll:
            continue
        p = f.get("properties") or {}
        name = p.get("Site_name") or p.get("Bathing_Water_Site") or "Unnamed"
        out.append({
            "id": "N:" + str(p.get("Unique_Site_ID_Code") or _slug(name)),
            # DAERA shouts its site names in caps; the page needs them readable.
            "name": tidy_name(_title(name)),
            "country": "Northern Ireland",
            "lat": ll[0], "lon": ll[1],
            # NOTE: water_quality_indicator is the CURRENT per-sample reading, not
            # the annual classification, and its vocabulary differs (Satisfactory
            # vs Sufficient). It is carried on the live side, never as "cls".
            "cls": None,
            "clsYear": None,
            "kind": "Inland" if (p.get("Region") or "") == "Inland" else "Coastal",
            "district": p.get("Region"),
            "rainRisk": False,
            "url": p.get("Profile__URL") or None,
        })
    return out


def sites_roi():
    # StatusTypeName carried through. 87 of the 240 Republic records are flagged
    # "Non Identified" — the EPA samples them and publishes restriction notices,
    # but they are not identified bathing waters — and the same 87 are the ones
    # with no published latitude, which is why their coordinates come from an
    # Irish Grid reference. Calling all 941 sites "designated" counted those 87
    # as something their own regulator says they are not.
    d = fetch_json(S.ROI_SITES)
    out, dropped = [], 0
    for b in d.get("value", []):
        # EtrsY (latitude) is null on ~87 of 240 records and some EtrsX values are
        # rounded placeholders. A beach in roughly the wrong place is worse than a
        # beach that is missing, so anything without a real pair is dropped.
        ll = valid(b.get("EtrsY"), b.get("EtrsX"))
        if not ll and b.get("Easting") and b.get("Northing"):
            # 87 of 240 have no latitude but every one has an Irish Grid ref.
            # Verified against the 153 that publish both — see check_irish_grid.py.
            try:
                ll = valid(*irish_grid_to_wgs84(b["Easting"], b["Northing"]))
            except (TypeError, ValueError):
                ll = None
        if not ll:
            dropped += 1
            continue
        out.append({
            "id": "I:" + str(b.get("Code") or b.get("LocationId")),
            "name": tidy_name(b.get("Name") or "Unnamed"),
            "country": "Ireland",
            "desig": b.get("StatusTypeName") != "Non Identified",
            "lat": ll[0], "lon": ll[1],
            "cls": _tidy_class(b.get("AnnualClassificationName")),
            "clsYear": b.get("AnnualClassificationYear"),
            "kind": "Coastal",
            "district": b.get("CountyName"),
            "rainRisk": False,
            "url": b.get("ProfileUrl") or None,
        })
    if dropped:
        print("    ROI: dropped %d sites with no usable position at all" % dropped)
    return out


def _kind(t):
    if not t:
        return "Coastal"
    t = t.lower()
    if "river" in t:
        return "River"
    if "lake" in t:
        return "Lake"
    if "transitional" in t:
        return "Estuary"
    return "Coastal"


def _tidy_class(c):
    if not c:
        return None
    c = str(c).strip()
    return c if c.lower() not in ("unclassified", "not classified", "-", "") else None


def _title(s):
    return " ".join(w.capitalize() if w.isupper() and len(w) > 2 else w for w in str(s).split())


def tidy_name(s):
    """Fix the punctuation the registers are inconsistent about.

    Five English bathing waters arrive with a backtick where an apostrophe
    belongs: Norman`s Bay, Mother Ivey`s Bay, St Margaret`s Bay. Sixteen others
    in the same register use a real apostrophe, so this is the source being
    untidy rather than a name anyone chose. The slug is unaffected, because
    both characters slugify away identically, so no URL changes.
    """
    return str(s).replace("`", "\u2019").strip()


# Which part of the country a place is in, for the filters on the site.
#
# England's comes from the Environment Agency's own operational regions rather
# than lines drawn here — they publish one per bathing water, and an official
# answer beats an invented boundary through the middle of Dorset. "Anglian" is
# their word for the east coast and means nothing to a swimmer, so it is
# relabelled.
EA_REGION_LABEL = {
    "South West": "South West",
    "South East": "South East",
    "North East": "North East",
    "North West": "North West",
    "Anglian": "East",
    "Midlands": "Midlands",
}

# The Republic by province, because 240 sites under one heading is not a filter.
IRISH_PROVINCE = {
    "Carlow": "Leinster", "Dublin": "Leinster", "Kildare": "Leinster",
    "Kilkenny": "Leinster", "Laois": "Leinster", "Longford": "Leinster",
    "Louth": "Leinster", "Meath": "Leinster", "Offaly": "Leinster",
    "Westmeath": "Leinster", "Wexford": "Leinster", "Wicklow": "Leinster",
    "Clare": "Munster", "Cork": "Munster", "Kerry": "Munster",
    "Limerick": "Munster", "Tipperary": "Munster", "Waterford": "Munster",
    "Galway": "Connacht", "Leitrim": "Connacht", "Mayo": "Connacht",
    "Roscommon": "Connacht", "Sligo": "Connacht",
    "Cavan": "Ulster", "Donegal": "Ulster", "Monaghan": "Ulster",
}


def add_regions(sites):
    for s in sites:
        c = s["country"]
        if c == "England":
            s["region"] = EA_REGION_LABEL.get(s.pop("eaRegion", None) or "", "England")
        elif c == "Ireland":
            prov = IRISH_PROVINCE.get(s.get("district") or "")
            s["region"] = ("Ireland — " + prov) if prov else "Ireland"
        else:
            s["region"] = c
        s.pop("eaRegion", None)


def add_slugs(sites):
    """A stable, readable url for every beach.

    These become public page addresses, so they have to be unique and they must
    not churn: a slug that changes between builds silently breaks every link and
    every search result pointing at it. Collisions are resolved by appending the
    district, and only then by a number, so the common case stays clean.
    """
    used = {}
    for s in sites:
        base = _slug(s["name"]) or "beach"
        slug = base
        if slug in used:
            extra = _slug(s.get("district") or s["country"])
            slug = "%s-%s" % (base, extra) if extra else base
        n = 2
        while slug in used:
            slug = "%s-%d" % (base, n)
            n += 1
        used[slug] = s["id"]
        s["slug"] = slug


def _slug(s):
    """A clean ASCII url fragment.

    Accents are folded rather than kept: "Trá na mBan, An Spidéal" becomes
    tra-na-mban-an-spideal, which people can type, paste into a message and read
    back off a link. Runs of punctuation collapse to a single hyphen so names
    with commas do not produce doubled ones.
    """
    text = unicodedata.normalize("NFKD", str(s))
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Apostrophes vanish rather than becoming hyphens: "Anstey's Cove" should
    # read ansteys-cove, not anstey-s-cove.
    text = text.replace("'", "").replace("\u2019", "")
    out = []
    for ch in text.lower():
        if ch.isalnum() and ord(ch) < 128:
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")[:60].strip("-")


# ---------------------------------------------------------------------------
# The outfalls
# ---------------------------------------------------------------------------

COMPANY_CODES = {
    "Thames Water": "THA", "South West Water": "SWW", "Wessex Water": "WES",
    "United Utilities": "UNU", "Yorkshire Water": "YOR", "Northumbrian Water": "NOR",
    "Anglian Water": "ANG", "Severn Trent": "SEV", "Southern Water": "SOU",
    "Welsh Water": "DCW", "Scottish Water": "SCO",
}


def outfalls_english():
    """The eight companies on the Water UK common schema."""
    out = []
    for company, url in S.EDM_FEEDS.items():
        code = COMPANY_CODES[company]
        feats = arcgis_all(url, fields="Id,Latitude,Longitude,ReceivingWaterCourse",
                           expect=arcgis_count(url))
        n = 0
        for f in feats:
            oid = attr(f, "Id")
            ll = valid(attr(f, "Latitude"), attr(f, "Longitude"))
            if oid is None or not ll:
                continue
            out.append({
                "key": "%s:%s" % (code, oid),
                "co": company, "lat": ll[0], "lon": ll[1],
                "water": _watercourse(attr(f, "ReceivingWaterCourse")),
            })
            n += 1
        print("    %-20s %5d outfalls" % (company, n))
    return out


def outfalls_southern():
    feats = arcgis_all(S.SOUTHERN_OUTFALLS,
                       fields="OutfallSiteID,Outfall_Name,latitude,longitude,CALMS_Receiving_Water",
                       expect=arcgis_count(S.SOUTHERN_OUTFALLS))
    out = []
    for f in feats:
        oid = attr(f, "OutfallSiteID")
        ll = valid(attr(f, "latitude"), attr(f, "longitude"))
        if oid is None or not ll:
            continue
        out.append({
            "key": "SOU:%s" % oid, "co": "Southern Water",
            "lat": ll[0], "lon": ll[1],
            "water": _watercourse(attr(f, "CALMS_Receiving_Water")),
        })
    print("    %-20s %5d outfalls" % ("Southern Water", len(out)))
    return out


def outfalls_wales():
    feats = arcgis_all(S.DCWW_SPILLS,
                       fields="asset_name,discharge_x_location,discharge_y_location,Receiving_Water",
                       geometry=True, expect=arcgis_count(S.DCWW_SPILLS))
    out = []
    for f in feats:
        name = attr(f, "asset_name")
        g = f.get("geometry") or {}
        ll = valid(g.get("y"), g.get("x"))
        if not name or not ll:
            continue
        out.append({
            "key": "DCW:%s" % name, "co": "Welsh Water",
            "lat": ll[0], "lon": ll[1],
            "water": _watercourse(attr(f, "Receiving_Water")),
        })
    print("    %-20s %5d outfalls" % ("Welsh Water", len(out)))
    return out


def outfalls_scotland():
    # The payload is {"results": [...], "last_updated": ...} — one 2.9MB blob with
    # no filtering or paging of any kind (query params are silently ignored).
    d = fetch_json(S.SW_NRT, timeout=180)
    rows = d if isinstance(d, list) else d.get("results", [])
    out = []
    for r in rows:
        r = r.get("attributes", r) if isinstance(r, dict) else {}
        aid = r.get("ASSET_ID") or r.get("asset_id")
        lat = r.get("DISCHARGE_OVERFLOW_LOCATION_LATITUDE") or r.get("LATITUDE") or r.get("LAT")
        lon = r.get("DISCHARGE_OVERFLOW_LOCATION_LONGITUDE") or r.get("LONGITUDE") or r.get("LONG")
        ll = valid(lat, lon)                       # every value arrives as a string
        if not aid or not ll:
            continue
        out.append({
            "key": "SCO:%s" % aid, "co": "Scottish Water",
            "lat": ll[0], "lon": ll[1],
            "water": _watercourse(r.get("RECEIVING_WATER")),
        })
    print("    %-20s %5d outfalls" % ("Scottish Water", len(out)))
    return out


def _watercourse(w):
    """Severn Trent SHOUTS these and Wessex prefixes a bracketed code."""
    if not w:
        return None
    w = str(w).strip()
    if w.startswith("(") and ")" in w[:6]:
        w = w.split(")", 1)[1].strip()
    if w.isupper():
        w = " ".join(x.capitalize() for x in w.split())
    return w or None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match(sites, outfalls, radius=RADIUS_KM):
    """Bucket outfalls into ~5.5km cells so each beach only compares against its
    own neighbourhood instead of all 18,600."""
    cell = 0.05
    grid = defaultdict(list)
    for i, o in enumerate(outfalls):
        grid[(int(o["lat"] / cell), int(o["lon"] / cell))].append(i)

    # A degree of longitude shrinks with latitude; at 55N it is ~64km against
    # 111km for a degree of latitude, so the cell search has to widen east-west
    # or beaches in Scotland quietly lose their outfalls.
    nearby, hit = {}, 0
    for s in sites:
        cy, cx = int(s["lat"] / cell), int(s["lon"] / cell)
        span_y = int(radius / (111.0 * cell)) + 1
        span_x = int(radius / (max(111.0 * math.cos(math.radians(s["lat"])), 1.0) * cell)) + 1
        found = []
        for dy in range(-span_y, span_y + 1):
            for dx in range(-span_x, span_x + 1):
                for i in grid.get((cy + dy, cx + dx), ()):
                    o = outfalls[i]
                    d = haversine(s["lat"], s["lon"], o["lat"], o["lon"])
                    if d <= radius:
                        found.append([o["key"], round(d, 2)])
        found.sort(key=lambda x: x[1])
        if found:
            hit += 1
        nearby[s["id"]] = found[:60]               # 60 is far more than any beach needs
    return nearby, hit


def main():
    quick = "--quick" in sys.argv
    force = "--force" in sys.argv
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()

    print("Beach registers")
    sites = []
    for label, fn in (("England", sites_england), ("Wales", sites_wales),
                      ("Scotland", sites_scotland), ("N Ireland", sites_ni),
                      ("Ireland", sites_roi)):
        try:
            got = fn()
            sites.extend(got)
            print("    %-10s %4d sites" % (label, len(got)))
        except Exception as e:                      # noqa: BLE001
            print("    %-10s FAILED: %s" % (label, e))
    print("    total      %4d sites" % len(sites))

    sites.sort(key=lambda s: (s["country"], s["name"]))
    add_regions(sites)
    add_slugs(sites)
    places.add_nearby_towns(sites)
    if not quick:
        print("Classification history")
        try:
            class_history(sites)
        except Exception as e:                      # noqa: BLE001
            print("    FAILED: %s" % str(e)[:100])
    write(os.path.join(OUT, "sites.json"), {
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sites": sites,
    }, count=len(sites), force=force)

    # Deliberately its own file rather than extra rows in sites.json. These are
    # NOT bathing waters — every "941 bathing waters" count on the site would
    # become a lie, and worse, one of them could pick up a green verdict from
    # some consumer that had not heard of them. A separate file cannot do that.
    print("Bathing waters that were de-designated")
    try:
        gone = sites_england_gone()
        if gone:
            add_slugs(gone)
            places.add_nearby_towns(gone)
            write(os.path.join(OUT, "gone.json"), {
                "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "note": ("Bathing waters removed from the Environment Agency's "
                         "register. Nobody samples these any more. Four carry "
                         "permanent advice against bathing."),
                "gone": gone,
            }, count=len(gone), force=force)
    except Exception as e:                          # noqa: BLE001
        print("    FAILED: %s" % str(e)[:120])

    if quick:
        print("--quick: skipping outfalls")
        return

    print("Outfalls")
    outfalls, broken = [], []
    for fn in (outfalls_english, outfalls_southern, outfalls_wales, outfalls_scotland):
        try:
            outfalls.extend(fn())
        except Exception as e:                      # noqa: BLE001
            broken.append(fn.__name__)
            print("    FAILED %s: %s" % (fn.__name__, e))
    if broken and not force:
        print("    STOPPING: %s failed, so the outfall map would be incomplete. The old "
              "files are left in place. Run again, or --force." % ", ".join(broken))
        return
    print("    total               %5d outfalls" % len(outfalls))

    print("Matching outfalls to beaches (within %.0fkm)" % RADIUS_KM)
    nearby, hit = match(sites, outfalls)
    linked = {k for v in nearby.values() for k, _ in v}
    print("    %d of %d beaches have an outfall within %.0fkm" % (hit, len(sites), RADIUS_KM))
    print("    %d outfalls are near at least one beach (of %d)" % (len(linked), len(outfalls)))

    # Order matters: outfalls.json is the one whose shrink guard is meaningful,
    # so it is written first and nearby.json only follows if that passed. And
    # nearby is counted by LINKS, not by beaches — every beach keeps its key even
    # when its list is emptied, so counting keys would never notice the loss.
    links = sum(len(v) for v in nearby.values())
    wrote = write(os.path.join(OUT, "outfalls.json"),
                  {o["key"]: [o["lat"], o["lon"], o["co"], o["water"]]
                   for o in outfalls if o["key"] in linked},
                  count=len(linked), force=force)
    if not wrote:
        print("    nearby.json left alone too — the two must stay in step")
        return
    write(os.path.join(OUT, "nearby.json"), nearby, count=links, force=force)

    print("Done in %.0fs" % (time.time() - t0))


def write(path, obj, count=None, force=False):
    """Write a build artifact, refusing to shrink it without being told to.

    A half-failed rebuild is the dangerous case: if one company's feed 500s, the
    outfall file silently loses a thousand pipes, every beach near them loses its
    nearby list, and the live side then reports "nothing within 2km" — a green
    tick standing on missing data rather than on clean water. So a build that
    comes back materially smaller than the last one is refused.
    """
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    if count is not None and os.path.exists(path) and not force:
        try:
            old = json.load(io.open(path, encoding="utf-8"))
            if isinstance(old, dict) and "sites" in old:
                before = len(old["sites"])
            elif isinstance(old, dict) and old and isinstance(next(iter(old.values())), list) \
                    and os.path.basename(path) == "nearby.json":
                before = sum(len(v) for v in old.values())      # links, not beaches
            else:
                before = len(old)
        except Exception:                           # noqa: BLE001
            before = 0
        if before and count < before * 0.9:
            print("    REFUSED %-20s %d records, down from %d — run again, or pass "
                  "--force if the drop is real" % (os.path.basename(path), count, before))
            return False
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(body)
    print("    wrote %-22s %7.1f KB" % (os.path.basename(path), len(body.encode()) / 1024.0))
    return True


if __name__ == "__main__":
    main()
