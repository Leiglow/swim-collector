"""Towns near enough to a place to be worth searching for.

places.json is GeoNames populated places in Britain and Ireland with a
population of 5,000 or more, trimmed to those within 30km of at least one
bathing water. CC BY 4.0. It never leaves this repository — the names it
produces are baked into the registers at build time, so a browser fetches four
extra words rather than a gazetteer, and nobody's location is sent anywhere.

WHY THIS EXISTS. tools/towns.json already gave every place its NEAREST town,
which sounds like the same thing and is not: the nearest town to Ainsdale is
Ainsdale, and the nearest town to Bovisand is not Plymouth. Searching
"Plymouth" returned 3 of the 24 bathing waters within 25km of it, and Truro,
Exeter, Edinburgh, Newcastle and Liverpool each returned nothing at all. What
people type is the city they are driving from, which is rarely the nearest
named thing to the beach.
"""
import io
import json
import math
import os
import unicodedata

PLACES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "places.json")
NEAR_TOWN_KM = 30.0        # a distance somebody would actually drive for a swim
NEAR_TOWN_MAX = 4          # enough to catch the city and its neighbours

_cache = None


def _load():
    global _cache
    if _cache is None:
        try:
            with io.open(PLACES, encoding="utf-8") as fh:
                rows = json.load(fh)
        except Exception as e:                      # noqa: BLE001
            print("    places.json unreadable (%s) - search will not know about towns" % e)
            rows = []
        grid = {}
        for name, lat, lon, pop in rows:
            grid.setdefault((round(lat), round(lon)), []).append((name, lat, lon, pop))
        _cache = grid
    return _cache


def _km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _key(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def towns_for(lat, lon, own_name=""):
    """The towns worth searching for near one point, biggest first.

    Ranked by population rather than distance: with four slots, somebody typing
    a place name means the city far more often than the hamlet beside it. A town
    already inside the place's own name is skipped - it is findable, and it
    would waste a slot.
    """
    grid = _load()
    if lat is None or lon is None or not grid:
        return []
    cand = []
    for dla in (-1, 0, 1):
        for dlo in (-1, 0, 1):
            for name, tla, tlo, pop in grid.get((round(lat) + dla, round(lon) + dlo), []):
                d = _km(lat, lon, tla, tlo)
                if d <= NEAR_TOWN_KM:
                    cand.append((-pop, d, name))
    cand.sort()
    own = _key(own_name)
    seen, picked = set(), []
    for _pop, _d, name in cand:
        k = _key(name)
        if not k or k in seen or k in own:
            continue
        seen.add(k)
        picked.append(name)
        if len(picked) >= NEAR_TOWN_MAX:
            break
    return picked


def add_nearby_towns(rows, field="near"):
    """Write the nearby town names onto each row that has coordinates."""
    filled = 0
    for r in rows:
        names = towns_for(r.get("lat"), r.get("lon"), r.get("name") or "")
        if names:
            r[field] = " ".join(names)
            filled += 1
    print("    named nearby towns on %d of %d" % (filled, len(rows)))
    return filled
