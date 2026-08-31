#!/usr/bin/env python3
"""The live half: read every feed, decide what to tell a swimmer, publish one
small snapshot.

Runs on GitHub Actions every 30 minutes. Deliberately NOT on Cloudflare: a free
Worker gets 10ms of CPU, and parsing 18,600 outfall records is far past that.

What comes out is one JSON file for all 941 beaches, which the site serves from
KV. The site this competes with ships 12.6MB per visit.

Four principles, in order. The first three were each violated by the first
version of this file, which is why they are written down:

  1. NEVER show green because a feed failed, a monitor is dead, or the season
     ended. Silence is not good news. Every beach carries a list of what was
     actually checked, and the page shows it.
  2. An official warning outranks anything we calculate. A legal prohibition or
     a "do not bathe" notice is the answer, not an input to a score.
  3. Last season's classification is NOT evidence about today's water. It never
     decides a verdict on its own.
  4. Say WHEN, and say whether a number was measured or modelled.

Usage:
    python3 tools/collect_swim.py                 write swim/live.json locally
    python3 tools/collect_swim.py --publish       also POST it to the site
"""
import csv
import gzip
import math
import io
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swim_sources as S

# Where the snapshot is written. This script runs from two places: inside the
# website repo (tools/collect_swim.py, writing into swim/) and inside its own
# small collector repo, where there is no swim/ directory and the working
# directory is the right answer.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("SWIM_OUT") or (
    os.path.join(ROOT, "swim") if os.path.isdir(os.path.join(ROOT, "swim")) else os.getcwd())
CTX = ssl.create_default_context()

# A spill this far from the beach counts against it. The register keeps matches
# out to 5km so the page can show "nearest outfall 4.1km" as context, and
# anything discharging between the two distances is still reported — just
# without escalating the verdict.
AFFECT_KM = 2.0
RECENT_HOURS = 48
HEAVY_RAIN_MM = 10.0            # over 24h, at a beach the EA flags as rain-affected
RAIN_MAX_AGE_MIN = 150          # rain is re-fetched at most this often; see rainfall()
# Waterfalls get their own, slower cadence. Open-Meteo counts LOCATIONS against a
# 10,000/day free allowance, and the 2,557 waterfalls add 859 grid cells to the
# 540 the beaches need. At the beach cadence that would be 13,400 calls a day —
# over the limit. Six-hourly keeps the total near 8,600, and costs nothing real:
# what a waterfall page reports is rain over the last 48 hours, which does not
# meaningfully change in six.
FALLS_RAIN_MAX_AGE_MIN = 360

# How much rain over 48 hours means what, for someone deciding whether a
# waterfall is worth the drive. Deliberately NOT a swimming judgement: the site
# says nothing about entering the water at a waterfall, because nobody samples,
# forecasts or signs them.
FLOW_BANDS = [
    (30.0, "spate", "In spate",
     "Very heavy rain has fallen here. The falls will be at their most dramatic "
     "and at their most dangerous — water levels can rise fast, rocks will be "
     "slick, and paths beside the water may be undercut."),
    (15.0, "strong", "Flowing strongly",
     "Plenty of rain in the last two days. Expect a full, loud waterfall, and "
     "wet rock underfoot on any path beside it."),
    (6.0, "good", "Flowing well",
     "Enough recent rain for a decent flow."),
    (2.0, "modest", "Modest flow",
     "A little recent rain. Worth a look, but not at its best."),
    (-1.0, "low", "Likely low",
     "Very little rain in the last two days, so expect a thin flow or, on a "
     "small stream, not much at all."),
]

NOW = datetime.now(timezone.utc)


def uk_offset(d):
    """Hours Britain and Ireland are ahead of UTC. BST runs from the last Sunday
    in March to the last Sunday in October.

    Two feeds publish naive local times with no marker (Welsh Water's discharge
    times, Southern's banner). Reading those as UTC shifts every summer spill by
    an hour, which at the boundary turns an ongoing discharge into a finished one.
    """
    # Compared naive-to-naive: this is called with the naive local timestamps
    # the publishers give us, before any timezone has been attached.
    bare = d.replace(tzinfo=None)

    def last_sunday(month):
        day = 31
        while True:
            try:
                c = datetime(bare.year, month, day, 1)
            except ValueError:
                day -= 1
                continue
            if c.weekday() == 6:
                return c
            day -= 1
    return 1 if last_sunday(3) <= bare < last_sunday(10) else 0


# Bathing seasons. Outside these dates the daily forecasts stop being published
# altogether, and a beach with no warning is not a beach that has been checked.
SEASONS = {
    "England": ((5, 15), (9, 30)),
    "Wales": ((5, 15), (9, 30)),
    "Scotland": ((6, 1), (9, 15)),
    "Northern Ireland": ((6, 1), (9, 15)),
    "Ireland": ((6, 1), (9, 15)),
}


def in_season(country, when=None):
    when = when or NOW
    s = SEASONS.get(country)
    if not s:
        return True
    (m1, d1), (m2, d2) = s
    start = datetime(when.year, m1, d1, tzinfo=timezone.utc)
    end = datetime(when.year, m2, d2, 23, 59, tzinfo=timezone.utc)
    return start <= when <= end


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------

class Feed:
    """One upstream source and whether it is actually working right now."""

    def __init__(self, name, covers=None, escalates=True):
        self.name = name
        self.covers = covers or []      # which countries go blind if this fails
        # Whether this feed failing actually changes a verdict. The page promises
        # that beaches relying on a broken feed are marked unchecked rather than
        # clear; that promise is only true for feeds that can escalate, so the
        # page needs to know which are which. Rainfall cannot: it is context.
        self.escalates = escalates
        self.ok = False
        self.partial = None
        self.error = None
        self.at = None                  # freshest timestamp inside the data
        self.count = 0
        self.spilling = 0
        self.offline = 0

    def as_dict(self):
        d = {"ok": self.ok, "count": self.count}
        if self.at:
            d["at"] = iso(self.at)
        if self.spilling:
            d["spilling"] = self.spilling
        if self.offline:
            d["offline"] = self.offline
        if self.partial:
            d["partial"] = self.partial
        if self.error:
            d["error"] = str(self.error)[:160]
        if self.covers:
            d["covers"] = self.covers
        d["escalates"] = self.escalates
        return d


# environment.data.gov.uk returns 403 to every request from GitHub's datacentre
# ranges — verified from a runner with a browser user agent, a bare curl and our
# own, all refused, while the same calls succeed from anywhere else. Losing it
# would cost England and Wales their daily pollution forecasts and every open
# pollution incident, so those calls are relayed through the site's own domain,
# which the API answers normally. Everything else goes direct.
PROXY_HOSTS = ("environment.data.gov.uk",)


# When a document comes from the relay's UK-side store rather than from the
# Environment Agency directly, the relay says how old it is. That age has to
# reach the page: a day-old warning presented as today's is exactly the failure
# this tool exists to avoid.
RELAY_AGE = {}


def via_proxy(url):
    """Returns (url_to_call, auth_token_or_None)."""
    base = os.environ.get("SWIM_FETCH_PROXY")
    token = os.environ.get("SWIM_INGEST_TOKEN")
    if not base or not token:
        return url, None
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return url, None
    if host not in PROXY_HOSTS:
        return url, None
    return base + "?u=" + urllib.parse.quote(url, safe=""), token


def fetch(url, tries=3, timeout=90, backoff=2):
    url, token = via_proxy(url)
    last = None
    for attempt in range(tries):
        try:
            headers = {
                "User-Agent": S.USER_AGENT,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            }
            if token:
                headers["Authorization"] = "Bearer " + token
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                age = r.headers.get("X-Data-Age-Seconds")
                if age is not None:
                    try:
                        RELAY_AGE[url] = (int(age), r.headers.get("X-Data-Fetched-At"))
                    except ValueError:
                        pass
                return raw
        except urllib.error.HTTPError as e:
            code = e.code
            # Carry the response body into the message. Without it a relayed
            # failure is just "502 Bad Gateway", which says nothing about which
            # of several possible causes it was.
            try:
                detail = e.read(400).decode("utf-8", "replace").strip()
            except Exception:                       # noqa: BLE001
                detail = ""
            last = RuntimeError("HTTP %s %s%s" % (code, e.reason,
                                                  " — " + detail if detail else ""))
            # 429 means we are asking too fast, not that the data is missing.
            if code == 429 and attempt < tries - 1:
                time.sleep(backoff * (attempt + 1) * 3)
                continue
            if attempt < tries - 1:
                time.sleep(backoff * (attempt + 1))
        except Exception as e:                      # noqa: BLE001
            last = e
            if attempt < tries - 1:
                time.sleep(backoff * (attempt + 1))
    raise RuntimeError(str(last)[:200])


def fetch_json(url, **kw):
    return json.loads(fetch(url, **kw).decode("utf-8", "replace"))


def arcgis_all(base, fields, where="1=1", page=1000, geometry=False, expect=None):
    """Page through a FeatureServer, and check we got everything it holds.

    ArcGIS truncates at maxRecordCount and flags exceededTransferLimit. Several
    of these services hold more than that, so a single call silently loses
    hundreds of outfalls — possibly the discharging one. `expect` reconciles
    against the service's own count, so a partial read is reported as a failure
    rather than as good news.
    """
    out, offset = [], 0
    while True:
        url = base + "?" + urllib.parse.urlencode({
            "where": where, "outFields": fields,
            "returnGeometry": "true" if geometry else "false",
            "outSR": "4326", "f": "json",
            "resultOffset": offset, "resultRecordCount": page,
        })
        d = fetch_json(url)
        if "error" in d:
            raise RuntimeError(json.dumps(d["error"])[:160])
        feats = d.get("features", [])
        out.extend(feats)
        if len(feats) < page and not d.get("exceededTransferLimit"):
            break
        if not feats:
            break
        offset += len(feats)
        if offset > 60000:
            break
    if expect is not None and len(out) < expect:
        raise RuntimeError("truncated: got %d of %d records" % (len(out), expect))
    return out


def arcgis_count(base, where="1=1"):
    try:
        d = fetch_json(base + "?" + urllib.parse.urlencode(
            {"where": where, "returnCountOnly": "true", "f": "json"}))
        return d.get("count")
    except Exception:                               # noqa: BLE001
        return None


def attr(rec, *names):
    a = rec.get("attributes", rec)
    lower = {k.lower(): v for k, v in a.items()}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def lit(v):
    """Unwrap a linked-data literal.

    environment.data.gov.uk wraps values: publishedAt arrives as
    {"_value": "2026-08-29T07:47:09", "_datatype": "dateTime"}. Handing that to
    a date parser silently yields None — which is how the guard that keeps the
    LATEST pollution forecast came to never run at all, leaving a withdrawn
    "advice against bathing" able to outrank the all-clear that replaced it.
    """
    if v is None or isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, dict):
        for k in ("_value", "name", "label"):
            if k in v:
                return lit(v[k])
        return None
    if isinstance(v, (list, tuple)):
        for item in v:
            got = lit(item)
            if got:
                return got
    return None


def epoch_ms(v):
    """ArcGIS dates are epoch milliseconds, as ints or strings."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    try:
        return datetime.fromtimestamp(v / 1000.0, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_iso(v, assume_local=False):
    v = lit(v)
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return epoch_ms(v)
    s = str(v).strip()
    if s.isdigit() and len(s) >= 12:                # epoch ms as a string
        return epoch_ms(s)
    s = s.replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"):
            try:
                d = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if d.tzinfo:
        return d
    # Naive. Some publishers mean UK local time by that, some mean UTC.
    if assume_local:
        return (d - timedelta(hours=uk_offset(d))).replace(tzinfo=timezone.utc)
    return d.replace(tzinfo=timezone.utc)


def iso(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hours_since(d):
    return (NOW - d).total_seconds() / 3600.0 if d else None


def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def norm(s):
    s = (s or "").lower()
    keep = [c for c in s if c.isalnum() or c == " "]
    return " ".join("".join(keep).split())


# ---------------------------------------------------------------------------
# live spill feeds
# ---------------------------------------------------------------------------

def state(now, end, start, recent=False, offline=False, note=None, window=None):
    """One outfall's condition, reduced to what a swimmer needs.

    `offline` matters as much as `now`: a monitor that is not reporting tells us
    nothing, and must never be counted as "not discharging".
    """
    if not now and end is not None:
        h = hours_since(end)
        if h is not None and 0 <= h <= RECENT_HOURS:
            recent = True
    return {"now": bool(now), "recent": bool(recent), "offline": bool(offline),
            "end": iso(end) if end else None, "start": iso(start) if start else None,
            "note": note, "window": window}


def spills_common(company, url, feed):
    """The eight English companies on the Water UK common schema.

    Status: 1 = discharging now, 0 = not discharging, -1 = offline.
    Read off the services' own coded-value domains, not assumed. Getting this
    backwards would paint every clean beach red and every spill green.
    """
    code = COMPANY_CODES[company]
    expect = arcgis_count(url)
    feats = arcgis_all(url, "Id,Status,StatusStart,LatestEventStart,LatestEventEnd,LastUpdated",
                       expect=expect)
    out, newest = {}, None
    for f in feats:
        oid = attr(f, "Id")
        if oid is None:
            continue
        key = "%s:%s" % (code, oid)
        try:
            status = int(attr(f, "Status"))
        except (TypeError, ValueError):
            # An unreadable status is not "fine" — treat it as a blind monitor.
            out[key] = state(False, None, None, offline=True)
            continue
        end = epoch_ms(attr(f, "LatestEventEnd"))
        start = epoch_ms(attr(f, "LatestEventStart")) or epoch_ms(attr(f, "StatusStart"))
        upd = epoch_ms(attr(f, "LastUpdated"))
        if upd and (newest is None or upd > newest):
            newest = upd
        out[key] = state(status == 1, end, start, offline=status == -1)
    feed.ok, feed.count, feed.at = True, len(out), newest
    feed.spilling = sum(1 for v in out.values() if v["now"])
    feed.offline = sum(1 for v in out.values() if v["offline"])
    stale_check(feed)
    return out


def stale_check(feed, hours=8):
    """Mark a feed unhealthy if NOTHING in it has been updated recently.

    Judged on the whole feed, never on individual rows: Northumbrian and
    Anglian only restamp a record when that outfall changes, so a perfectly
    healthy monitor can carry a six-week-old timestamp. It is the newest
    record in the feed that says whether the publisher is still alive.
    """
    if feed.at is None:
        return
    age = hours_since(feed.at)
    if age is not None and age > hours:
        feed.ok = False
        feed.error = "no record newer than %d hours" % int(age)


def spills_southern(feed):
    """Southern Water publishes its own shape. ReleaseStatus is a 1/2/3 traffic
    light, NOT a discharge state — the only field that says whether something is
    discharging now is the plain-English SpillMessage.

    Their lookback window is 72 hours, not the 48 the other companies use, so
    the wording that reaches the page has to say 72 for these.
    """
    expect = arcgis_count(S.SOUTHERN_OUTFALLS)
    feats = arcgis_all(S.SOUTHERN_OUTFALLS, "OutfallSiteID,ReleaseStatus,SpillMessage",
                       expect=expect)
    out = {}
    for f in feats:
        oid = attr(f, "OutfallSiteID")
        if oid is None:
            continue
        msg = (attr(f, "SpillMessage") or "").strip()
        low = msg.lower()
        now = low.startswith("there is an ongoing")
        recent = ("last 24 hours" in low) or ("last 72 hours" in low)
        # "under review" and "unverified" mean Southern itself does not yet know.
        blind = (("under review" in low) or ("unverified" in low) or not msg) and not now
        out["SOU:%s" % oid] = state(now, None, None, recent=recent, offline=blind,
                                    note=msg or None, window=72)
    feed.ok, feed.count = True, len(out)
    feed.spilling = sum(1 for v in out.values() if v["now"])
    feed.offline = sum(1 for v in out.values() if v["offline"])
    try:
        upd = arcgis_all(S.SOUTHERN_SITES.replace("/0/query", "/3/query"), "*", page=10)
        feed.at = parse_iso(attr(upd[0], "Last_Updated_Date")) if upd else None
    except Exception:                               # noqa: BLE001 - freshness is a nicety
        pass
    return out


def southern_beaches(feed, by_eubwid):
    """Southern's own bathing-site view, which applies a tidal model and carries
    the EA's site id.

    It is the only company feed that answers the swimmer's question rather than
    the engineer's, so where Southern says a release may have affected a beach,
    that is treated as a statement about the beach — not overridden by our own
    distance arithmetic.
    """
    feats = arcgis_all(S.SOUTHERN_SITES, "eubwid,Name,ReleaseStatus,SpillMessage")
    got = {}
    for f in feats:
        eub = str(attr(f, "eubwid") or "").strip().lower()
        sid = by_eubwid.get(eub)
        if not sid:
            continue
        msg = (attr(f, "SpillMessage") or "").strip()
        low = msg.lower()
        if not msg or "no recent" in low or "no releases" in low:
            continue
        # Southern's message says both what happened AND whether their tidal
        # model thinks it reached the beach. The two must be read separately:
        # "There is an ongoing release ... that WILL NOT IMPACT the bathing water
        # due to tidal conditions" contains the word "ongoing" but is an
        # all-clear for this beach. Matching on the tense alone told eleven
        # beaches not to swim on the strength of a message saying the opposite.
        impacts = "may have affected" in low or "may be affecting" in low
        cleared = "will not impact" in low or "did not impact" in low
        got[sid] = {"msg": msg,
                    "ongoing": impacts and "ongoing" in low,
                    "recent": impacts and not cleared,
                    "cleared": cleared}
    feed.ok, feed.count = True, len(got)
    return got


def spills_wales(feed):
    """Welsh Water. Status is descriptive text, and the feed carries DCWW's own
    outfall-to-beach mapping in Linked_Bathing_Water, which beats guessing by
    distance — it is their model of which beach an outfall actually affects.

    Their discharge times are naive UK local time, not UTC.
    """
    expect = arcgis_count(S.DCWW_SPILLS)
    feats = arcgis_all(S.DCWW_SPILLS,
                       "asset_name,status,start_date_time_discharge,stop_date_time_discharge,"
                       "Linked_Bathing_Water,EditDate", expect=expect)
    out, links, newest = {}, defaultdict(list), None
    for f in feats:
        name = attr(f, "asset_name")
        if not name:
            continue
        st = (attr(f, "status") or "").strip()
        low = st.lower()
        now = low.startswith("overflow operating")
        recent = "last 24 hours" in low
        blind = ("investigation" in low or not st) and not now
        start = parse_iso(attr(f, "start_date_time_discharge"), assume_local=True)
        stop = parse_iso(attr(f, "stop_date_time_discharge"), assume_local=True)
        upd = epoch_ms(attr(f, "EditDate"))
        if upd and (newest is None or upd > newest):
            newest = upd
        key = "DCW:%s" % name
        made = state(now, stop, start, recent=recent, offline=blind, note=st or None)
        # asset_name is not unique in this feed. Last-write-wins could drop a
        # discharging record in favour of a quiet namesake, so duplicates merge
        # to the worst case.
        prev = out.get(key)
        if prev:
            made["now"] = prev["now"] or made["now"]
            made["recent"] = prev["recent"] or made["recent"]
            made["offline"] = prev["offline"] and made["offline"]
        out[key] = made
        # Comma-separated string, not an array.
        for beach in (attr(f, "Linked_Bathing_Water") or "").split(","):
            beach = beach.strip()
            if beach:
                links[norm(beach)].append(key)
    feed.ok, feed.count, feed.at = True, len(out), newest
    feed.spilling = sum(1 for v in out.values() if v["now"])
    feed.offline = sum(1 for v in out.values() if v["offline"])
    stale_check(feed)
    return out, links


def spills_scotland(feed):
    """Scottish Water's official API — one 2.9MB blob, no filtering, and every
    value a string. Its last_updated is epoch milliseconds, not ISO, so parsing
    it as a date string loses Scotland's only freshness signal."""
    d = fetch_json(S.SW_NRT, timeout=180)
    rows = d if isinstance(d, list) else d.get("results", [])
    out = {}
    newest = parse_iso(d.get("last_updated")) if isinstance(d, dict) else None
    for r in rows:
        aid = r.get("ASSET_ID")
        if not aid:
            continue
        st = (r.get("OVERFLOW_STATUS_DESCRIPTION") or "")
        code = st.split("-")[0].strip().upper()
        seen = parse_iso(r.get("DEVICE_LAST_TRANSMITTED_DATETIME"))
        if seen and (newest is None or seen > newest):
            newest = seen
        out["SCO:%s" % aid] = state(
            code == S.SW_SPILLING,
            parse_iso(r.get("OVERFLOW_END_DATETIME")),
            parse_iso(r.get("OVERFLOW_START_DATETIME")),
            recent=code == "RO",
            # DA is "no data available"; anything unrecognised is also not proof
            # of a clean pipe.
            offline=code not in ("OF", "RO", "NO"),
            note=st or None,
        )
    feed.ok, feed.count, feed.at = True, len(out), newest
    feed.spilling = sum(1 for v in out.values() if v["now"])
    feed.offline = sum(1 for v in out.values() if v["offline"])
    stale_check(feed)
    return out


COMPANY_CODES = {
    "Thames Water": "THA", "South West Water": "SWW", "Wessex Water": "WES",
    "United Utilities": "UNU", "Yorkshire Water": "YOR", "Northumbrian Water": "NOR",
    "Anglian Water": "ANG", "Severn Trent": "SEV",
}


# ---------------------------------------------------------------------------
# official warnings — these outrank anything we work out ourselves
# ---------------------------------------------------------------------------

def note_relay_age(feed, url, stale_hours=20):
    """Record how old a relayed document was, and fail the feed if it is stale.

    Nothing is gained by pretending yesterday's forecast is today's — the page
    would rather say "can't say" than show a withdrawn warning.
    """
    got = RELAY_AGE.get(url)
    if not got:
        return
    seconds, fetched_at = got
    when = parse_iso(fetched_at) if fetched_at else None
    if when:
        feed.at = when
    hours = seconds / 3600.0
    if hours > stale_hours:
        feed.ok = False
        feed.error = ("the UK-side copy is %.0f hours old — it refreshes when the "
                      "site is viewed from the UK" % hours)
    elif hours > 1:
        feed.partial = "from a copy %.0f hours old" % hours


def prf(feed, url=None, prefix="E:"):
    """The daily pollution risk forecast for England, or Wales under its own
    prefix.

    Three traps, all able to tell someone the water is dirty when the warning has
    been lifted, or clean when it has not:

      * predictedOn is MANDATORY. Without it the endpoint returns the whole
        historical archive in arbitrary order.
      * A site can get SEVERAL forecasts in one morning and the risk level
        CHANGES between them — one real site went increased (07:47) then normal
        (08:41). Keep the one with the latest publishedAt.
      * Every value is a wrapped literal, so publishedAt has to go through lit()
        before any date parsing or the guard above silently never runs.
    """
    got = {}
    last_error = None
    for day_offset in (0, 1):
        date = (NOW - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        feed.partial = None if day_offset == 0 else "using yesterday's forecast"
        page_url = (url or S.EA_PRF).format(date=date)
        page_url_used = page_url
        items, guard = [], 0
        try:
            while page_url and guard < 10:
                d = fetch_json(page_url)
                res = d.get("result", {})
                items.extend(res.get("items", []))
                page_url = res.get("next")
                guard += 1
        except Exception as e:                          # noqa: BLE001
            # The fallback to yesterday only worked while today's document came
            # back EMPTY. Overnight, before the Environment Agency publishes at
            # about a quarter to eight, the relay has never stored today's copy
            # at all and answers 503 — which threw out of this function and took
            # the fallback with it. England and Wales then went to "can't say"
            # for the night: 358 bathing waters unchecked at half past one in the
            # morning, with yesterday's standing forecast sitting in the larder.
            last_error = e
            continue
        if not items:
            continue
        best = {}
        for it in items:
            site = it.get("stp_bathingWater")
            site = site.get("_about") if isinstance(site, dict) else site
            if not site:
                continue
            sid = prefix + str(site).rstrip("/").split("/")[-1]
            pub = parse_iso(it.get("publishedAt")) or parse_iso(it.get("predictedAt"))
            expires = parse_iso(it.get("expiresAt"))
            prev = best.get(sid)
            # Keep the latest PUBLISHED forecast. An undated record must never
            # displace a dated one, or the withdrawn warning wins by luck.
            if prev and (pub is None or (prev[0] and prev[0] >= pub)):
                continue
            level = it.get("riskLevel")
            level = level.get("_about") if isinstance(level, dict) else level
            best[sid] = (pub, {
                "level": str(level).rstrip("/").split("/")[-1] if level else None,
                "comment": lit(it.get("comment")),
                "at": iso(pub) if pub else None,
                "expires": iso(expires) if expires else None,
            })
        # A forecast past its own expiry is yesterday's news, not today's answer.
        got = {k: v[1] for k, v in best.items()
               if not v[1]["expires"] or v[1]["expires"] >= iso(NOW)}
        if got:
            feed.ok, feed.count = True, len(got)
            feed.at = max((parse_iso(v["at"]) for v in got.values() if v["at"]), default=None)
            note_relay_age(feed, page_url_used)
            break
    if not got and last_error is not None:
        # Neither day could be read. That IS a failed feed, and saying so is what
        # puts the affected beaches at "can't say" rather than quietly clear.
        raise last_error
    return got


def ea_incidents(feed):
    """Open pollution incidents and active suspensions, from the register CSV.

    These are separate from the daily forecast and more serious: a confirmed oil
    or sewage incident, or a bathing water formally suspended. The first version
    of this tool never read them, and eleven English beaches with an open
    incident were showing as fine to swim.

    Suspension rows go stale — one has sat there since 2023 with no end date — so
    anything over a year old, or whose expected end has passed, is ignored rather
    than presented as current.
    """
    csv_url = S.EA_SITES.replace("bathing-water.json", "bathing-water.csv")
    raw = fetch(csv_url, timeout=120)
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))
    got = {}
    for r in rows:
        eub = (r.get("eubwid notation") or "").strip()
        if not eub:
            continue
        sid = "E:" + eub
        inc_type = (r.get("latest open incident > incident type") or "").strip()
        inc_start = parse_iso(r.get("latest open incident > start of incident"))
        if inc_type and inc_start:
            # The column is "latest OPEN incident". Ageing one out would have the
            # page state there is no incident while the register still says there
            # is; the date travels with it instead, so the page can say how long.
            got.setdefault(sid, {})["incident"] = {"type": inc_type, "at": iso(inc_start)}
        s_desc = (r.get("latest active suspension > description") or "").strip()
        s_start = parse_iso(r.get("latest active suspension > start of suspension"))
        s_end = parse_iso(r.get("latest active suspension > expected end of suspension"))
        if s_desc and s_start:
            ended = s_end is not None and s_end < NOW
            if (NOW - s_start).days <= 365 and not ended:
                got.setdefault(sid, {})["suspension"] = {"why": s_desc, "at": iso(s_start)}
    feed.ok, feed.count = True, len(got)
    note_relay_age(feed, csv_url)
    return got


# The only forecast wordings SEPA uses to say the water is expected to be fine.
# Matched as a closed set on purpose: anything outside it — "Awaiting Prediction"
# before the morning run, or some future wording — must not be read as an
# all-clear. See the verdict function for what happens to the rest.
SEPA_CLEAR = {"good", "excellent", "no pollution risk", "normal"}


def predictions_scotland(feed, by_country_name):
    """SEPA's daily prediction, covering 30 of Scotland's 90 beaches. The other
    60 have an annual classification and nothing else, which the page must say
    rather than implying today's water has been assessed."""
    d = fetch_json(S.SEPA_PREDICTIONS)
    rows = d if isinstance(d, list) else d.get("items", [])
    got = {}
    for r in rows:
        sid = by_country_name.get(("Scotland", norm(r.get("bathing_water"))))
        if not sid:
            continue
        when = parse_iso(r.get("last_updated"))
        got[sid] = {
            "forecast": (r.get("current_forecast") or "").strip() or None,
            "at": iso(when) if when else None,
            "reason": (r.get("override_reason_text") or "").strip() or None,
        }
    feed.ok, feed.count = True, len(got)
    return got


def restrictions_roi(feed):
    """Statutory bathing restrictions in the Republic. These are legal notices,
    not model output, and are the strongest signal anywhere in this system.

    Ireland has no storm overflow monitoring at all — no EDM programme exists —
    so for Irish beaches this and the sample results are the whole picture."""
    d = fetch_json(S.ROI_RESTRICTIONS)
    rows = d if isinstance(d, list) else (d.get("value") or d.get("items") or d.get("data") or [])
    got = {}
    for r in rows:
        code = r.get("Code") or r.get("LocationId")
        if not code:
            continue
        if r.get("HasRestrictionInPlace") is False:
            continue
        got["I:%s" % code] = {
            "type": (r.get("RestrictionTypeName") or "").strip() or None,
            "group": (r.get("RestrictionGroupTypeName") or "").strip() or None,
            "detail": (r.get("IncidentDescription") or "").strip()[:300] or None,
            "from": r.get("StartDate"),
            "notice": r.get("WarningNoticeUrl"),
        }
    feed.ok, feed.count = True, len(got)
    return got


def quality_ni(feed, by_country_name):
    """Northern Ireland's live per-sample indicator — the only live NI signal.

    Its vocabulary is Excellent/Good/Satisfactory/NoBathing, which is NOT the
    annual classification vocabulary. Sampling is weekly at best, so the age of
    the reading travels with it and the page states it.
    """
    d = fetch_json(S.DAERA_SITES)
    got, newest = {}, None
    for f in d.get("features", []):
        p = f.get("properties") or {}
        code = p.get("Unique_Site_ID_Code")
        sid = ("N:%s" % code) if code else by_country_name.get(
            ("Northern Ireland", norm(p.get("Site_name"))))
        if not sid:
            continue
        when = epoch_ms(p.get("Sampling_datetime")) or parse_iso(p.get("Sampling_datetime"))
        if when and (newest is None or when > newest):
            newest = when
        got[sid] = {
            "indicator": (p.get("water_quality_indicator") or "").strip() or None,
            "at": iso(when) if when else None,
        }
    feed.ok, feed.count, feed.at = True, len(got), newest
    return got


# ---------------------------------------------------------------------------
# rain
# ---------------------------------------------------------------------------

def flow_for(rain, intermittent):
    """Turn recent rainfall into what a visitor would want to know.

    The safety note gets STRONGER as the flow gets better, which is the opposite
    of how the beach side of this site reads. That inversion is deliberate and is
    the whole reason waterfalls are not run through the bathing water logic: rain
    makes a beach dirtier, but it makes a waterfall both more impressive and more
    lethal. A coroner's Prevention of Future Deaths report was issued in February
    2026 over three deaths in Waterfall Country, and the documented mechanism at
    UK waterfalls is usually slipping on wet rock, not swimming.
    """
    if not rain:
        return None
    mm = rain.get("h48", 0)
    for threshold, code, word, note in FLOW_BANDS:
        if mm >= threshold:
            out = {"v": code, "word": word, "note": note, "mm48": mm,
                   "mm24": rain.get("h24", 0), "next24": rain.get("next24", 0)}
            if intermittent and code in ("low", "modest"):
                out["note"] = ("This one is recorded as intermittent — it only runs after "
                               "rain, and there has been little. " + note)
            return out
    return None


def rainfall(sites, feed, previous=None, max_age_min=None):
    """Rain in the last 24 and 48 hours at every beach.

    Rain is the honest proxy for a spill nobody has reported yet — and for the
    beaches with no monitored outfall nearby, it is the only forward signal.

    Two things learned the hard way:

      * Open-Meteo counts LOCATIONS, not requests, against its rate limit. 941
        beaches in quick succession earns a 429 partway through, and because the
        beach list is sorted by country that silently wiped out every beach in
        Wales, Scotland and Ireland while the feed still reported itself healthy.
      * Their model grid is about 11km, so beaches within 11km of each other get
        an identical answer anyway. Collapsing to a 0.1 degree grid cuts 941
        lookups to about 540 and loses nothing real.

    Rain totals over 24 hours barely move in half an hour, so a recent result is
    carried forward instead of re-fetched. That keeps us inside the daily
    allowance with room to spare, and the age of the figure is published.
    """
    limit = max_age_min or RAIN_MAX_AGE_MIN
    if previous and previous.get("at") and previous.get("rain"):
        when = parse_iso(previous["at"])
        age = hours_since(when)
        if age is not None and 0 <= age * 60 < limit:
            # Carrying forward must not launder a partial set into a healthy one:
            # apply the same coverage bar as a fresh fetch.
            enough = len(previous["rain"]) >= len(sites) * 0.95
            feed.ok, feed.count, feed.at = enough, len(previous["rain"]), when
            feed.partial = "carried forward, %d minutes old%s" % (
                int(age * 60), "" if enough else " (%d of %d beaches)" % (
                    len(previous["rain"]), len(sites)))
            return previous["rain"], when

    cells = {}
    for s in sites:
        cells.setdefault((round(s["lat"] / 0.1), round(s["lon"] / 0.1)), []).append(s)
    keys = list(cells.keys())
    batches = [keys[i:i + S.OPEN_METEO_BATCH] for i in range(0, len(keys), S.OPEN_METEO_BATCH)]

    got, failed = {}, 0
    for n, batch in enumerate(batches):
        pts = [cells[k][0] for k in batch]
        url = S.OPEN_METEO.format(
            lats=",".join("%.4f" % p["lat"] for p in pts),
            lons=",".join("%.4f" % p["lon"] for p in pts))
        try:
            d = fetch_json(url, timeout=60, tries=4)
        except Exception as e:                      # noqa: BLE001
            feed.error = e
            failed += len(batch)
            continue
        blocks = d if isinstance(d, list) else [d]
        for key, blk in zip(batch, blocks):
            h = (blk or {}).get("hourly") or {}
            times, precip = h.get("time") or [], h.get("precipitation") or []
            if not times:
                continue
            stamp = NOW.strftime("%Y-%m-%dT%H:00")
            idx = next((i for i, t in enumerate(times) if t >= stamp), len(times) - 1)

            def total(hours, i=idx, p=precip):
                lo = max(0, i - hours)
                return round(sum(x or 0 for x in p[lo:i]), 1)

            value = {"h24": total(24), "h48": total(48),
                     "next24": round(sum(x or 0 for x in precip[idx:idx + 24]), 1)}
            for s in cells[key]:
                got[s["id"]] = value
        if n < len(batches) - 1:
            # Open-Meteo's limit is 600 LOCATIONS a minute, not 600 requests, so
            # a batch of 100 must be followed by roughly ten seconds. Sending
            # them 1.2s apart was asking for 5,000 a minute and quietly losing
            # whole batches to 429s — which, before the coverage check below, had
            # wiped out every beach in Wales, Scotland and Ireland in one run.
            time.sleep(len(batch) / 9.0)

    feed.count = len(got)
    feed.at = NOW
    # Partial coverage is a failure of this feed, not a quiet gap.
    feed.ok = failed == 0 and len(got) >= len(sites) * 0.95
    if failed:
        feed.partial = "%d of %d locations" % (len(got), len(sites))
    return got, NOW


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------

AVOID_WORDS = ("advice against bathing", "do not swim", "prohibited",
               "swimming not advised", "no bathing")

# "unknown" sits ABOVE "ok": not knowing is a worse answer than knowing it is
# clear, and must be able to displace a green tick.
# Five levels, and the split between the top two is the point of them. "avoid"
# is somebody official saying do not go in this water today — a legal
# prohibition, an open pollution incident, a storm overflow discharging within
# 2km right now. "advised" is the daily pollution risk forecast: a model's view
# of today, published as advice against bathing, and routine after heavy rain.
#
# They used to share a level, which put a forecast under the same words as a
# legal ban and made two thirds of the red on this site predictive rather than
# actual.
# Running this by hand writes the snapshot next to the site so it can be looked
# at. The names start with a dot and are gitignored: the undotted names are real,
# committed files that say the old build-time snapshots are gone, and a local run
# must never quietly put a live-looking snapshot back at a public address.
# How recent a published snapshot has to be for a run to stand down. The
# workflow asks every few minutes because GitHub delivers only a fraction of
# what it is asked for; this is what stops the ones that do arrive from
# repeating work that is already done.
FRESH_ENOUGH_MINUTES = 20

LOCAL_SNAPSHOT = ".snapshot-local.json"
LOCAL_FALLS = ".falls-local.json"


ORDER = {"ok": 0, "unknown": 1, "caution": 2, "advised": 3, "avoid": 4}

AUTHORITY = {"England": "Environment Agency", "Wales": "Natural Resources Wales",
             "Scotland": "SEPA", "Northern Ireland": "DAERA", "Ireland": "EPA Ireland"}

FORECAST_FEED = {"England": "EA pollution risk forecast",
                 "Wales": "NRW pollution risk forecast",
                 "Scotland": "SEPA daily prediction",
                 "Northern Ireland": "NI sample quality",
                 "Ireland": "Ireland restrictions"}


def _outfall_row(ctx, key, dist, code):
    """One nearby outfall, as the page needs it: what it discharges into, how far
    away, its state, and where it is.

    The position is carried so a beach page can show these on a map — seeing
    which pipe is discharging and where it sits relative to the water is the
    thing a list of names cannot convey.
    """
    pos = ctx["outfall_pos"].get(key) or []
    row = [ctx["outfall_name"].get(key) or ctx["outfall_co"].get(key, "Outfall"), dist, code]
    if len(pos) == 2:
        row.extend([round(pos[0], 5), round(pos[1], 5)])
    return row


def verdict(site, ctx):
    """Decide what to tell someone standing on this beach, and why.

    Returns the level, the reasons, and — just as important — a record of what
    was actually checked and what could not be. A green tick that cannot say
    what it looked at is worthless.

    An official warning is the answer on its own; it is not averaged with
    anything. Last season's classification never decides a verdict.
    """
    why, checked, gaps = [], [], []
    level = "ok"
    sid, country = site["id"], site["country"]

    def raise_to(new):
        return new if ORDER[new] > ORDER[level] else level

    # ---- 1. official warnings ----------------------------------------------
    p = ctx["prf"].get(sid)
    if p:
        checked.append("Today's official pollution forecast")
        comment = (p.get("comment") or "").lower()
        if any(w in comment for w in AVOID_WORDS):
            # The daily forecast, not an incident: the Environment Agency and NRW
            # predict today's water from rainfall and tides and post advice
            # against bathing when the model says so. Serious, and the reason
            # this site exists — but it is a forecast, and it sits a step below
            # a prohibition or sewage actually going in the water.
            level = raise_to("advised")
            why.append({"t": "advised", "s": AUTHORITY.get(country, "The regulator"),
                        "text": p.get("comment"), "at": p.get("at")})
        elif p.get("level") == "increased":
            level = raise_to("caution")
            why.append({"t": "forecast", "s": AUTHORITY.get(country, "The regulator"),
                        "text": p.get("comment") or "Increased pollution risk forecast today",
                        "at": p.get("at")})
        elif p.get("level") == "normal":
            why.append({"t": "clear", "s": AUTHORITY.get(country, "The regulator"),
                        "text": p.get("comment") or "No pollution warnings in force",
                        "at": p.get("at")})
        else:
            # No risk level at all means the forecast could not be read, which is
            # not the same as a forecast that says everything is fine.
            level = raise_to("unknown")
            gaps.append("Today's forecast for this beach could not be read")

    inc = ctx["incidents"].get(sid) or {}
    if inc.get("incident"):
        level = raise_to("avoid")
        why.append({"t": "warning", "s": AUTHORITY.get(country, "The regulator"),
                    "text": "Open pollution incident — " + inc["incident"]["type"],
                    "at": inc["incident"]["at"]})
    if inc.get("suspension"):
        level = raise_to("avoid")
        why.append({"t": "warning", "s": AUTHORITY.get(country, "The regulator"),
                    "text": "This bathing water is suspended (%s)" % inc["suspension"]["why"],
                    "at": inc["suspension"]["at"]})

    if country == "Ireland" and ctx["feeds"]["Ireland restrictions"].ok:
        checked.append("Today's local authority bathing restrictions")
    r = ctx["roi"].get(sid)
    if r:
        text = r.get("type") or "Bathing restriction in force"
        if r.get("group") and r["group"].lower() not in text.lower():
            text += " — " + r["group"].lower()
        level = raise_to("avoid") if any(w in (r.get("type") or "").lower()
                                         for w in AVOID_WORDS) else raise_to("caution")
        why.append({"t": "warning", "s": "Local authority notice", "text": text,
                    "detail": r.get("detail"), "at": r.get("from"), "url": r.get("notice")})

    sc = ctx["sepa"].get(sid)
    if sc and sc.get("forecast"):
        f = sc["forecast"].lower()
        if "pollution incident" in f:
            checked.append("SEPA's prediction for today")
            level = raise_to("avoid")
            why.append({"t": "warning", "s": "SEPA", "text": "Pollution incident reported",
                        "at": sc.get("at")})
        elif f == "poor":
            checked.append("SEPA's prediction for today")
            level = raise_to("caution")
            why.append({"t": "forecast", "s": "SEPA",
                        "text": "Today's prediction: poor water quality", "at": sc.get("at")})
        elif f in SEPA_CLEAR:
            checked.append("SEPA's prediction for today")
            why.append({"t": "clear", "s": "SEPA",
                        "text": "Today's prediction: " + f, "at": sc.get("at")})
        else:
            # SEPA carries the beach but has not said anything about it today.
            # "Awaiting Prediction" sits in this field for every Scottish beach
            # until their morning run lands, and for all of them again after
            # the season — it is the ABSENCE of a prediction, and printing it
            # as a reason once turned 19 beaches green on the strength of SEPA
            # not having spoken. An unrecognised wording lands here too: a
            # string this code has never seen is not evidence that the water
            # is fine.
            gaps.append("SEPA has not issued today's prediction for this beach yet")
    elif country == "Scotland" and ctx["feeds"]["SEPA daily prediction"].ok:
        # Only claim SEPA does not cover this beach when we actually reached
        # SEPA. If their feed is down, the gate below reports that instead.
        gaps.append("SEPA does not publish a daily prediction for this beach — only "
                    "%d of Scotland's 90 bathing waters get one" % len(ctx["sepa"]))

    ni = ctx["ni"].get(sid)
    if ni and ni.get("indicator"):
        age = hours_since(parse_iso(ni.get("at"))) if ni.get("at") else None
        if (ni["indicator"]).lower() == "nobathing":
            level = raise_to("avoid")
            why.append({"t": "warning", "s": "DAERA", "text": "No bathing advised",
                        "at": ni.get("at")})
        elif age is not None and age <= 24 * 8:
            checked.append("Most recent bacterial sample")
        if age is not None and age > 24 * 8:
            gaps.append("The most recent water sample here is %d days old" % int(age / 24))

    sw = ctx["southern"].get(sid)
    if sw:
        # Southern Water's own tidal model, about this specific beach. Their view
        # can flag a beach our distance arithmetic would not — and can also clear
        # one, which is reported as context rather than as a warning.
        if sw["ongoing"]:
            level = raise_to("avoid")
        elif sw["recent"]:
            level = raise_to("caution")
        why.append({"t": "spill" if sw["ongoing"] else
                         "spill-recent" if sw["recent"] else "clear",
                    "s": "Southern Water", "text": sw["msg"]})

    # ---- 2. spills ----------------------------------------------------------
    near = ctx["nearby"].get(sid) or []
    now_list, recent_list, blind, further = [], [], [], []
    unknown_co, resolved = set(), 0
    # The outfalls close enough to matter, carried into the snapshot so the page
    # can show what was actually looked at without downloading the national
    # outfall dataset — and so the detail table shows a real status rather than
    # the company's name.
    detail = []
    for key, dist in near:
        st = ctx["spills"].get(key)
        if dist > AFFECT_KM:
            if st and st["now"]:
                further.append((key, dist, st))
            continue
        if st is None:
            unknown_co.add(ctx["outfall_co"].get(key, "a water company"))
            continue
        if st["offline"]:
            blind.append((key, dist, st))
            # A release the company has logged but not yet verified is both
            # unreadable AND a reported discharge. Counting it only as a dead
            # monitor would lose the discharge entirely.
            if st["recent"]:
                recent_list.append((key, dist, st))
            detail.append(_outfall_row(ctx, key, dist, "off"))
            continue
        resolved += 1
        if st["now"]:
            now_list.append((key, dist, st))
        elif st["recent"]:
            recent_list.append((key, dist, st))
        detail.append(_outfall_row(ctx, key, dist,
                                   "now" if st["now"] else "recent" if st["recent"] else "clear"))

    if resolved:
        checked.append("%d monitored storm overflow%s within %.0fkm"
                       % (resolved, "" if resolved == 1 else "s", AFFECT_KM))

    if now_list:
        level = raise_to("avoid")
        nearest = min(now_list, key=lambda x: x[1])
        why.append({"t": "spill", "s": ctx["outfall_co"].get(nearest[0], "Water company"),
                    "text": "%d storm overflow%s discharging within %.0fkm right now"
                            % (len(now_list), "" if len(now_list) == 1 else "s", AFFECT_KM),
                    "km": nearest[1], "at": nearest[2].get("start")})
    elif recent_list:
        level = raise_to("caution")
        nearest = min(recent_list, key=lambda x: x[1])
        window = nearest[2].get("window") or RECENT_HOURS
        why.append({"t": "spill-recent", "s": ctx["outfall_co"].get(nearest[0], "Water company"),
                    "text": "%d storm overflow%s discharged within %.0fkm in the last %d hours"
                            % (len(recent_list), "" if len(recent_list) == 1 else "s",
                               AFFECT_KM, window),
                    "km": nearest[1], "at": nearest[2].get("end")})

    # A discharge just outside the radius does not escalate the verdict, but
    # staying silent about it would make "no warnings" mean more than it should.
    if further and not now_list:
        nearest = min(further, key=lambda x: x[1])
        why.append({"t": "spill-far", "s": ctx["outfall_co"].get(nearest[0], "Water company"),
                    "text": "%d storm overflow%s discharging further off, the nearest %.1fkm away"
                            % (len(further), "" if len(further) == 1 else "s", nearest[1]),
                    "km": nearest[1]})

    # A dead monitor is not a clean pipe. This is the difference between "we
    # looked and it was off" and "we looked and it was fine".
    if blind:
        level = raise_to("unknown")
        nearest = min(blind, key=lambda x: x[1])
        gaps.append("%d nearby overflow monitor%s not reporting (nearest %.1fkm away)"
                    % (len(blind), "" if len(blind) == 1 else "s", nearest[1]))
    if unknown_co:
        level = raise_to("unknown")
        gaps.append("%s's live feed is down, so overflows near here could not be checked"
                    % ", ".join(sorted(unknown_co)))

    if country in ("Ireland", "Northern Ireland"):
        gaps.append("Nowhere in Ireland monitors storm overflows, so sewage discharges "
                    "cannot be checked here at all")

    # ---- 3. rain ------------------------------------------------------------
    rain = ctx["rain"].get(sid) or {}
    if rain:
        # Deliberately NOT added to `checked`. Rain is a weather model, not a
        # look at the water, and counting it let a green tick be earned by an
        # 11km grid square having stayed dry — which is how 58 beaches with
        # nothing else known about them came to read "No warnings today".
        if rain.get("h24", 0) >= HEAVY_RAIN_MM:
            if site.get("rainRisk") or now_list or recent_list:
                level = raise_to("caution")
            why.append({"t": "rain", "s": "Open-Meteo — modelled, not measured",
                        "text": "%.1fmm of rain here in the last 24 hours" % rain["h24"]})
    elif not ctx["feeds"]["Rainfall"].ok:
        gaps.append("Rainfall could not be checked for this beach")

    # ---- 4. the standing rating --------------------------------------------
    # Never used to justify a green verdict — only ever to add a warning.
    if (site.get("cls") or "").lower().startswith("poor"):
        level = raise_to("caution")
        why.append({"t": "class", "s": "Annual classification",
                    "text": "Rated Poor for %s" % (site.get("clsYear") or "the last season")})

    # ---- 5. season and coverage --------------------------------------------
    if not in_season(country):
        gaps.append("Out of bathing season — daily forecasts and sampling stop until "
                    "the summer, so nothing is being checked today")
        level = raise_to("unknown")
    else:
        # Ireland is included: the restrictions feed is the ONLY live signal for
        # 240 Irish beaches, so its failure has to be disclosed exactly like a
        # failed forecast elsewhere, not quietly ignored.
        feed = ctx["feeds"].get(FORECAST_FEED.get(country, ""))
        if not (feed and feed.ok):
            gaps.append("Today's official information could not be fetched from %s"
                        % AUTHORITY.get(country, "the regulator"))
            level = raise_to("unknown")

    # A green verdict has to be earned by something actually checked today.
    if level == "ok" and not checked:
        level = "unknown"

    detail.sort(key=lambda x: x[1])
    return {"level": level, "why": why, "checked": checked, "gaps": gaps,
            "now": len(now_list), "recent": len(recent_list),
            "blind": len(blind), "near": near, "detail": detail}


# ---------------------------------------------------------------------------

def collect_waterfalls(feeds):
    """The waterfall side: rainfall only, and a flow reading built from it.

    Kept in its own snapshot rather than bolted onto the bathing water one. A
    beach page has no use for 2,557 waterfalls and a waterfall page has no use
    for 941 beaches, so each fetches only what it needs.
    """
    try:
        falls = load_static("waterfalls.json")["falls"]
    except Exception as e:                          # noqa: BLE001
        print("    no waterfall register available: %s" % str(e)[:90])
        return None

    f = feeds["Waterfall rainfall"] = Feed("Waterfall rainfall", escalates=False)
    try:
        rain, rain_at = rainfall(falls, f, previous_falls(), FALLS_RAIN_MAX_AGE_MIN)
    except Exception as e:                          # noqa: BLE001
        f.error = e
        print("    Waterfall rainfall FAILED %s" % str(e)[:120])
        return None

    out = {}
    for w in falls:
        flow = flow_for(rain.get(w["id"]), w.get("intermittent") == "yes")
        if flow:
            out[w["id"]] = flow
    print("    %-32s %4d waterfalls%s" % ("Waterfall rainfall", len(out),
          " (" + f.partial + ")" if f.partial else ""))
    counts = defaultdict(int)
    for v in out.values():
        counts[v["v"]] += 1
    print("    flow:", dict(counts))
    return {
        "at": iso(NOW),
        "rainAt": iso(rain_at) if rain_at else None,
        "counts": dict(counts),
        "falls": out,
    }


def previous_falls():
    url = os.environ.get("SWIM_INGEST_URL", "").replace("/ingest", "/data")
    if not url:
        return None
    try:
        d = fetch_json(url + ("&" if "?" in url else "?") + "falls=1", tries=1, timeout=20)
        return {"at": d.get("rainAt") or d.get("at"),
                "rain": {k: {"h24": v["mm24"], "h48": v["mm48"], "next24": v["next24"]}
                         for k, v in (d.get("falls") or {}).items()
                         if isinstance(v, dict) and "mm48" in v}}
    except Exception:                               # noqa: BLE001
        return None


def previous_snapshot():
    """The last published snapshot, used only to carry rainfall forward.

    Nothing else is ever reused: a stale spill status presented as live is
    exactly the failure this tool exists to avoid.
    """
    url = os.environ.get("SWIM_INGEST_URL", "").replace("/ingest", "/data")
    if not url:
        return None
    try:
        d = fetch_json(url, tries=1, timeout=20)
        return {"at": d.get("rainAt") or d.get("at"),
                "rain": {k: {"h24": v["rain"][0], "h48": v["rain"][1], "next24": v["rain"][2]}
                         for k, v in (d.get("sites") or {}).items()
                         if isinstance(v, dict) and isinstance(v.get("rain"), list)
                         and len(v["rain"]) == 3}}
    except Exception:                               # noqa: BLE001
        return None


def load_static(name):
    """Read a build artifact from disk, or from the live site.

    The collector runs from its own small public repo so it can use unlimited
    free Actions minutes, while the beach register lives with the website. Rather
    than keep two copies in step, it just reads the published one — there is one
    source of truth, and a register rebuild reaches the collector the moment it
    is deployed.
    """
    base = os.environ.get("SWIM_DATA_BASE")
    local = os.path.join(OUT, name)
    if not base:
        if os.path.exists(local):
            return json.load(io.open(local, encoding="utf-8"))
        # No default URL here: this file is published in a public repo, so the
        # address it posts to comes from the environment, never from the source.
        raise RuntimeError(
            "%s not found locally and SWIM_DATA_BASE is not set — point it at the "
            "directory the register is published from, or run "
            "build_swim_register.py first" % name)
    return fetch_json(base.rstrip("/") + "/" + name, timeout=60)


def already_fresh(minutes):
    """Has somebody already published a snapshot recently enough?

    GitHub drops most scheduled events, so the workflow asks for a run every few
    minutes and accepts that only some arrive. That only works if the runs that
    are not needed are cheap: this checks the published snapshot first and lets
    the job finish in seconds rather than spending ninety on work already done.

    Anything it cannot determine — no address, no answer, an unreadable time —
    means carry on and collect. A missed run is worse than a wasted one.
    """
    base = os.environ.get("SWIM_DATA_BASE")
    if not base or "--force" in sys.argv:
        return False
    try:
        d = fetch_json(base.rstrip("/") + "/data", timeout=30)
        at = parse_iso(d.get("at"))
        if not at:
            return False
        age = (NOW - at).total_seconds() / 60.0
        if age < minutes:
            print("published snapshot is %.0f minutes old — nothing to do" % age)
            return True
        print("published snapshot is %.0f minutes old — collecting" % age)
    except Exception as e:                              # noqa: BLE001
        print("could not read the published snapshot (%s) — collecting" %
              str(e)[:80])
    return False


def main():
    t0 = time.time()
    if already_fresh(FRESH_ENOUGH_MINUTES):
        return
    sites = load_static("sites.json")["sites"]
    nearby = load_static("nearby.json")
    outfalls = load_static("outfalls.json")
    outfall_co = {k: v[2] for k, v in outfalls.items()}
    outfall_name = {k: v[3] for k, v in outfalls.items() if v[3]}
    outfall_pos = {k: [v[0], v[1]] for k, v in outfalls.items()}
    # Keyed by country as well as name: "Sandycove" and "Silver Strand" exist in
    # more than one country, and a bare name map hands one country's reading to
    # the other's beach.
    by_country_name = {(s["country"], norm(s["name"])): s["id"] for s in sites}
    by_eubwid = {s["id"].split(":", 1)[1].lower(): s["id"] for s in sites}

    feeds, spills = {}, {}

    def run(name, fn, *a, **kw):
        f = feeds[name] = Feed(name, kw.get("covers"), kw.get("escalates", True))
        try:
            return fn(f, *a)
        except Exception as e:                      # noqa: BLE001 - a dead feed must not kill the run
            f.error = e
            print("    %-26s FAILED %s" % (name, str(e)[:400]))
            return None

    print("Storm overflows")
    for company, url in S.EDM_FEEDS.items():
        got = run(company, lambda f, c=company, u=url: spills_common(c, u, f))
        if got:
            spills.update(got)
            print("    %-26s %5d outfalls, %d discharging, %d offline"
                  % (company, feeds[company].count, feeds[company].spilling,
                     feeds[company].offline))

    got = run("Southern Water", spills_southern)
    if got:
        spills.update(got)
        print("    %-26s %5d outfalls, %d discharging, %d unverified"
              % ("Southern Water", feeds["Southern Water"].count,
                 feeds["Southern Water"].spilling, feeds["Southern Water"].offline))

    wales = run("Welsh Water", spills_wales)
    dcww_links = {}
    if wales:
        got, dcww_links = wales
        spills.update(got)
        print("    %-26s %5d outfalls, %d discharging"
              % ("Welsh Water", feeds["Welsh Water"].count, feeds["Welsh Water"].spilling))

    got = run("Scottish Water", spills_scotland)
    if got:
        spills.update(got)
        print("    %-26s %5d outfalls, %d discharging, %d no data"
              % ("Scottish Water", feeds["Scottish Water"].count,
                 feeds["Scottish Water"].spilling, feeds["Scottish Water"].offline))

    # Welsh Water publishes its own outfall-to-beach mapping, which is a better
    # answer than our distance guess. Added, then re-sorted so "nearest" stays true.
    linked = 0
    coords = {s["id"]: (s["lat"], s["lon"]) for s in sites}
    for beach_name, keys in (dcww_links or {}).items():
        sid = by_country_name.get(("Wales", beach_name))
        if not sid or sid not in coords:
            continue
        have = {k for k, _ in nearby.get(sid, [])}
        for k in keys:
            if k in have:
                continue
            o = outfalls.get(k)
            if not o:
                continue
            # The REAL distance, not a placeholder. Filing these at a nominal
            # 0.5km would have shown a user "nearest outfall 0.5km" for a pipe
            # up to 8.6km up the coast, and pulled distant spills inside the 2km
            # radius. Welsh Water's own judgement that it affects this beach is
            # respected by including it at all; the distance stays honest.
            nearby.setdefault(sid, []).append([k, round(haversine(coords[sid][0], coords[sid][1],
                                                                 o[0], o[1]), 2)])
            linked += 1
        nearby[sid].sort(key=lambda x: x[1])
    if linked:
        print("    Welsh Water's own beach mapping added %d links" % linked)

    print("Official warnings")
    warnings = run("EA pollution risk forecast", prf, covers=["England"]) or {}
    warnings.update(run("NRW pollution risk forecast",
                        lambda f: prf(f, S.NRW_PRF, "W:"), covers=["Wales"]) or {})
    incidents = run("EA incidents and suspensions", ea_incidents, covers=["England"]) or {}
    sepa = run("SEPA daily prediction", predictions_scotland, by_country_name,
               covers=["Scotland"]) or {}
    roi = run("Ireland restrictions", restrictions_roi, covers=["Ireland"]) or {}
    ni = run("NI sample quality", quality_ni, by_country_name,
             covers=["Northern Ireland"]) or {}
    southern = run("Southern Water beaches", southern_beaches, by_eubwid) or {}
    for k in ("EA pollution risk forecast", "NRW pollution risk forecast",
              "EA incidents and suspensions", "SEPA daily prediction",
              "Ireland restrictions", "NI sample quality", "Southern Water beaches"):
        if feeds[k].ok:
            print("    %-32s %4d" % (k, feeds[k].count))

    print("Rain")
    rain_res = run("Rainfall", lambda f: rainfall(sites, f, previous_snapshot()),
                   escalates=False)
    rain, rain_at = rain_res if rain_res else ({}, None)
    print("    %-32s %4d beaches%s"
          % ("Rainfall", len(rain),
             " (" + feeds["Rainfall"].partial + ")" if feeds["Rainfall"].partial else ""))

    ctx = {"prf": warnings, "incidents": incidents, "sepa": sepa, "roi": roi, "ni": ni,
           "southern": southern, "rain": rain, "spills": spills, "nearby": nearby,
           "outfall_co": outfall_co, "outfall_name": outfall_name,
           "outfall_pos": outfall_pos, "feeds": feeds}

    print("Verdicts")
    out, counts = {}, defaultdict(int)
    for s in sites:
        v = verdict(s, ctx)
        counts[v["level"]] += 1
        rec = {"v": v["level"], "why": v["why"]}
        for k, val in (("ck", v["checked"]), ("gaps", v["gaps"]), ("now", v["now"]),
                       ("recent", v["recent"]), ("blind", v["blind"])):
            if val:
                rec[k] = val
        r = rain.get(s["id"])
        if r:
            rec["rain"] = [r["h24"], r["h48"], r["next24"]]
        if v["near"]:
            rec["outfalls"] = len(v["near"])
            rec["nearest"] = v["near"][0][1]
        if v["detail"]:
            rec["of"] = [[r[0], round(r[1], 1)] + list(r[2:]) for r in v["detail"]]
        n = ni.get(s["id"])
        if n and n.get("indicator"):
            rec["ni"] = n["indicator"]
            rec["niAt"] = n.get("at")
        out[s["id"]] = rec
    for k in ("avoid", "advised", "caution", "unknown", "ok"):
        print("    %-9s %4d" % (k, counts[k]))

    snapshot = {
        "at": iso(NOW),
        "rainAt": iso(rain_at) if rain_at else None,
        "season": {c: in_season(c) for c in SEASONS},
        "counts": dict(counts),
        "feeds": {k: v.as_dict() for k, v in feeds.items()},
        "sites": out,
    }
    body = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False)
    io.open(os.path.join(OUT, LOCAL_SNAPSHOT), "w", encoding="utf-8").write(body)

    print("Waterfalls")
    falls_snapshot = collect_waterfalls(feeds)
    falls_body = None
    if falls_snapshot:
        falls_body = json.dumps(falls_snapshot, separators=(",", ":"), ensure_ascii=False)
        io.open(os.path.join(OUT, LOCAL_FALLS), "w", encoding="utf-8").write(falls_body)
        print("    wrote falls.json %.0f KB (%.0f KB gzipped)"
              % (len(falls_body.encode()) / 1024.0,
                 len(gzip.compress(falls_body.encode())) / 1024.0))
    print("wrote live.json %.0f KB (%.0f KB gzipped) in %.0fs"
          % (len(body.encode()) / 1024.0, len(gzip.compress(body.encode())) / 1024.0,
             time.time() - t0))

    if "--publish" in sys.argv:
        publish(body)
        if falls_body:
            publish(falls_body, kind="falls")


def publish(body, kind=None):
    url = os.environ.get("SWIM_INGEST_URL")
    token = os.environ.get("SWIM_INGEST_TOKEN")
    if not url or not token:
        print("publish: SWIM_INGEST_URL / SWIM_INGEST_TOKEN not set — skipped")
        return
    if kind:
        url += ("&" if "?" in url else "?") + "kind=" + kind
    # Sent uncompressed on purpose: a Worker hands request.text() the raw bytes,
    # so a gzipped body arrives as mojibake and is rejected on every single run.
    last = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
            "User-Agent": S.USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                raw = r.read(600).decode("utf-8", "replace")
            print("published:", r.status, raw)
            try:
                res = json.loads(raw)
            except ValueError:
                raise SystemExit("publish returned something that is not JSON: " + raw[:200])
            if res.get("ok") is not True:
                raise SystemExit("publish rejected by the site: " + raw[:200])
            # A history that has quietly stopped recording should turn the run red,
            # not sit in a log nobody reads.
            if res.get("historyNote") not in (None, "written"):
                raise SystemExit("history not recorded: " + str(res.get("historyNote")))
            return
        except SystemExit:
            raise
        except Exception as e:                      # noqa: BLE001
            last = e
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
    raise SystemExit("publish failed after 3 attempts: %s" % str(last)[:200])


if __name__ == "__main__":
    main()
