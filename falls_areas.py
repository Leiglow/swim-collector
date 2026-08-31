# Which county is a waterfall in?
#
# Waterfalls come from OpenStreetMap as bare points: an id, a name and a
# position, and nothing to say where in the country they are. Beaches get their
# district free from the bathing water registers, so the beach side of the site
# has always been filterable by area and the waterfall side has not.
#
# tools/counties.json holds 244 simplified county outlines — the ONS county and
# unitary authority boundaries for the UK, OpenStreetMap's admin_level 6
# relations for the Republic — and this works out which one a point falls in.
# The outlines are simplified to about 400 metres, which is invisible at county
# scale and keeps the file to a few hundred kilobytes.
import io, json, math, os

_CACHE = None


def _load():
    global _CACHE
    if _CACHE is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "counties.json")
        _CACHE = json.load(io.open(path, encoding="utf-8"))
    return _CACHE


def _inside(x, y, ring):
    """Ray casting. Odd number of crossings to the left means inside."""
    hit = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                hit = not hit
        j = i
    return hit


def district_of(lat, lon):
    """The county a point is in, and the country that county is in.

    Returns (county, country), or (None, None) for a point that is not in
    Britain, Ireland or the Isle of Man at all.

    Two things this fixes beyond simply filling in the county:

    Country. Until now a waterfall took the country of the nearest designated
    bathing water, which cannot separate the two sides of the Irish border and
    guessed wrong for fifteen of them — seven in the Scottish Borders filed
    under England, nine in Ulster on the wrong side. A county sits wholly in
    one country, so reading the country off the county is simply right.

    Reach. The search box asks OpenStreetMap for everything in a rectangle,
    and that rectangle clips the top of France: six waterfalls near Calais and
    in Normandy were being published as English, two of them under their French
    names. Anything more than eight kilometres from any county outline is not in
    the British Isles and comes back as nothing.
    """
    counties = _load()
    for name, c in counties.items():
        x0, y0, x1, y1 = c["bb"]
        if lon < x0 - 0.01 or lon > x1 + 0.01 or lat < y0 - 0.01 or lat > y1 + 0.01:
            continue
        for ring in c["r"]:
            if _inside(lon, lat, ring):
                return name, c["c"]

    # Simplifying the outlines pulls the coast in by a few hundred metres, and a
    # waterfall on a sea cliff can land fractionally outside every county. Eight
    # kilometres is comfortably wider than that error and far narrower than the
    # Channel: the nearest county to the Normandy ones is thirty-four km away.
    best, bestkm = (None, None), 8.0
    for name, c in counties.items():
        x0, y0, x1, y1 = c["bb"]
        if lon < x0 - 0.2 or lon > x1 + 0.2 or lat < y0 - 0.2 or lat > y1 + 0.2:
            continue
        for ring in c["r"]:
            for x, y in ring:
                km = math.hypot((x - lon) * 65.0, (y - lat) * 111.0)
                if km < bestkm:
                    best, bestkm = (name, c["c"]), km
    return best
