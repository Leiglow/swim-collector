#!/usr/bin/env python3
"""Build the waterfall register from OpenStreetMap.

Waterfalls are not bathing waters. Nobody samples them, forecasts them or signs
them, so this deliberately carries NO water quality information — the pages that
use it answer "is it worth the walk today", never "can I swim".

Source: OpenStreetMap, waterway=waterfall, under the Open Database Licence. That
licence requires attribution and share-alike on the data itself, which is why the
extract written here stays a plain reusable file and every page credits OSM.

Run occasionally — waterfalls do not move:

    python3 tools/build_waterfalls.py
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
import urllib.parse
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("SWIM_OUT") or (
    os.path.join(ROOT, "swim") if os.path.isdir(os.path.join(ROOT, "swim")) else os.getcwd())

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Britain, Ireland and the isles around them.
BBOX = "49.8,-11.0,61.0,2.0"

QUERY = """[out:json][timeout:180];
(node["waterway"="waterfall"](%s);
 way["waterway"="waterfall"](%s););
out tags center;""" % (BBOX, BBOX)

CTX = ssl.create_default_context()
UA = "swim-collector/1.0 (personal outdoors tool; open data)"


def fetch(url, data=None, tries=3, timeout=200):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url, data=data.encode("utf-8") if data else None,
                headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return raw
        except Exception as e:                      # noqa: BLE001
            last = e
            if attempt < tries - 1:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(str(last)[:200])


def slugify(text):
    t = unicodedata.normalize("NFKD", str(text))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("'", "").replace("’", "")
    out = []
    for ch in t.lower():
        if ch.isalnum() and ord(ch) < 128:
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")[:60].strip("-")


def haversine(a, b, c, d):
    r = 6371.0088
    p1, p2 = math.radians(a), math.radians(c)
    x = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(d - b) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def load_reference():
    """The bathing water register, used to work out which country a point is in.

    No bounding box can separate Northern Ireland from the Republic — Donegal
    sits north of much of Ulster — so instead each waterfall takes the country of
    the nearest designated bathing water. There are 941 of those spread around
    both islands, including 33 in Northern Ireland and 240 in the Republic, which
    makes this reliable everywhere except within a few miles of the border.
    """
    path = os.path.join(OUT, "sites.json")
    if not os.path.exists(path):
        return []
    sites = json.load(io.open(path, encoding="utf-8"))["sites"]
    return [(s["lat"], s["lon"], s["country"]) for s in sites]


def country_of(lat, lon, reference):
    if not reference:
        return "United Kingdom"
    best, bestd = None, 1e9
    for rlat, rlon, country in reference:
        # Squared degrees is enough to rank candidates and far cheaper than a
        # great-circle distance across 941 sites for 2,557 waterfalls.
        d = (rlat - lat) ** 2 + ((rlon - lon) * 0.6) ** 2
        if d < bestd:
            best, bestd = country, d
    return best


def main():
    print("Asking OpenStreetMap for every waterfall in Britain and Ireland")
    body = None
    for host in MIRRORS:
        try:
            body = fetch(host, data="data=" + urllib.parse.quote(QUERY))
            print("    %s answered, %.0f KB" % (host.split("/")[2], len(body) / 1024.0))
            break
        except Exception as e:                      # noqa: BLE001
            print("    %s failed: %s" % (host.split("/")[2], str(e)[:70]))
    if not body:
        raise SystemExit("no Overpass mirror answered")

    els = json.loads(body.decode("utf-8", "replace")).get("elements", [])
    print("    %d waterfalls tagged" % len(els))
    reference = load_reference()
    print("    %d bathing waters loaded as the country reference" % len(reference))

    falls, used = [], {}
    unnamed = 0
    for e in els:
        tags = e.get("tags") or {}
        lat = e.get("lat") if e.get("lat") is not None else (e.get("center") or {}).get("lat")
        lon = e.get("lon") if e.get("lon") is not None else (e.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        name = (tags.get("name") or "").strip()
        rec = {
            "id": "F:%s%s" % (e.get("type", "n")[0], e.get("id")),
            "lat": round(float(lat), 5),
            "lon": round(float(lon), 5),
            "country": country_of(float(lat), float(lon), reference),
        }
        if name:
            rec["name"] = name
            base = slugify(name) or "waterfall"
            slug = base
            n = 2
            while slug in used:
                slug = "%s-%d" % (base, n)
                n += 1
            used[slug] = rec["id"]
            rec["slug"] = slug
        else:
            unnamed += 1
        for key, out_key in (("height", "height"), ("width", "width"),
                             ("wikidata", "wikidata"), ("wikipedia", "wikipedia"),
                             ("intermittent", "intermittent"), ("access", "access"),
                             ("tourism", "tourism"), ("name:cy", "nameCy"),
                             ("name:ga", "nameGa"), ("name:gd", "nameGd")):
            v = (tags.get(key) or "").strip()
            if v:
                rec[out_key] = v[:80]
        falls.append(rec)

    named = [f for f in falls if f.get("name")]
    print("    %d with a name, %d without" % (len(named), unnamed))

    # Nearest neighbours among the named ones, for the "other falls nearby" links.
    cell = 0.25
    grid = defaultdict(list)
    for i, f in enumerate(named):
        grid[(int(f["lat"] / cell), int(f["lon"] / cell))].append(i)
    for f in named:
        cy, cx = int(f["lat"] / cell), int(f["lon"] / cell)
        near = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for i in grid.get((cy + dy, cx + dx), ()):
                    o = named[i]
                    if o["id"] == f["id"]:
                        continue
                    km = haversine(f["lat"], f["lon"], o["lat"], o["lon"])
                    if km < 25:
                        near.append((round(km, 1), o["slug"], o["name"]))
        near.sort()
        f["near"] = [[s, n, k] for k, s, n in near[:6]]

    by_country = defaultdict(int)
    for f in falls:
        by_country[f["country"]] += 1
    print("    by country:", dict(by_country))

    payload = {
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "OpenStreetMap contributors, Open Database Licence",
        "falls": falls,
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    path = os.path.join(OUT, "waterfalls.json")
    io.open(path, "w", encoding="utf-8").write(body)
    print("    wrote waterfalls.json %.0f KB (%.0f KB gzipped)"
          % (len(body.encode()) / 1024.0, len(gzip.compress(body.encode())) / 1024.0))


if __name__ == "__main__":
    main()
