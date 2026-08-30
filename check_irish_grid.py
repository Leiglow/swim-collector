#!/usr/bin/env python3
"""Prove the Irish Grid conversion against beaches that publish both positions.

Run this if swim_irishgrid.py is ever touched. A silent regression here would
put Irish beaches in the wrong place — including, plausibly, in the sea.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_swim_register as B
from swim_irishgrid import irish_grid_to_wgs84

errs = []
for b in B.fetch_json(B.S.ROI_SITES)["value"]:
    ll = B.valid(b.get("EtrsY"), b.get("EtrsX"))
    if not ll or not b.get("Easting"):
        continue
    lat, lon = irish_grid_to_wgs84(b["Easting"], b["Northing"])
    errs.append((B.haversine(ll[0], ll[1], lat, lon) * 1000, b["Name"]))

errs.sort()
n = len(errs)
ok = sum(1 for e, _ in errs if e < 100)
print("checked %d beaches that publish both a grid ref and a lat/lon" % n)
print("median %.0fm | 90th %.0fm | worst %.0fm (%s)" % (errs[n // 2][0], errs[int(n * .9)][0], errs[-1][0], errs[-1][1]))
print("within 100m: %d/%d" % (ok, n))
sys.exit(0 if ok >= n - 1 else 1)
