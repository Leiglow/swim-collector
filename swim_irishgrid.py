"""Irish Grid (EPSG:29903) -> WGS84.

beaches.ie gives a latitude for only 153 of its 240 bathing waters, but an Irish
Grid reference for all 240. Without this, 87 Irish beaches — a tenth of the
country's coast — would simply be missing from the map.

Accuracy is not assumed. Run tools/check_irish_grid.py: it converts the 153
beaches that publish BOTH and compares. Median error 0m, 152 of 153 within 100m,
one 205m outlier (Youghal Claycastle) whose own two published positions disagree.
"""
import math

# Airy Modified 1849, the ellipsoid the Irish Grid is defined on
A, B_ = 6377340.189, 6356034.447
F0 = 1.000035
LAT0, LON0 = math.radians(53.5), math.radians(-8.0)
E0, N0 = 200000.0, 250000.0


def ig_to_ie65(east, north):
    e2 = (A * A - B_ * B_) / (A * A)
    n = (A - B_) / (A + B_)
    lat = LAT0
    m = 0.0
    for _ in range(20):
        lat = (north - N0 - m) / (A * F0) + lat
        dl, sl = lat - LAT0, lat + LAT0
        ma = (1 + n + 1.25 * n ** 2 + 1.25 * n ** 3) * dl
        mb = (3 * n + 3 * n ** 2 + 2.625 * n ** 3) * math.sin(dl) * math.cos(sl)
        mc = (1.875 * n ** 2 + 1.875 * n ** 3) * math.sin(2 * dl) * math.cos(2 * sl)
        md = (35.0 / 24.0) * n ** 3 * math.sin(3 * dl) * math.cos(3 * sl)
        m = B_ * F0 * (ma - mb + mc - md)
        if abs(north - N0 - m) < 1e-5:
            break
    sin_lat = math.sin(lat)
    nu = A * F0 / math.sqrt(1 - e2 * sin_lat ** 2)
    rho = A * F0 * (1 - e2) / (1 - e2 * sin_lat ** 2) ** 1.5
    eta2 = nu / rho - 1
    tl = math.tan(lat)
    tl2, tl4, tl6 = tl ** 2, tl ** 4, tl ** 6
    sec = 1.0 / math.cos(lat)
    vii = tl / (2 * rho * nu)
    viii = tl / (24 * rho * nu ** 3) * (5 + 3 * tl2 + eta2 - 9 * tl2 * eta2)
    ix = tl / (720 * rho * nu ** 5) * (61 + 90 * tl2 + 45 * tl4)
    x = sec / nu
    xi = sec / (6 * nu ** 3) * (nu / rho + 2 * tl2)
    xii = sec / (120 * nu ** 5) * (5 + 28 * tl2 + 24 * tl4)
    xiia = sec / (5040 * nu ** 7) * (61 + 662 * tl2 + 1320 * tl4 + 720 * tl6)
    de = east - E0
    latr = lat - vii * de ** 2 + viii * de ** 4 - ix * de ** 6
    lonr = LON0 + x * de - xi * de ** 3 + xii * de ** 5 - xiia * de ** 7
    return latr, lonr


def helmert(lat, lon, a, b, dx, dy, dz, rx, ry, rz, s, a2, b2):
    e2 = (a * a - b * b) / (a * a)
    nu = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    x = nu * math.cos(lat) * math.cos(lon)
    y = nu * math.cos(lat) * math.sin(lon)
    z = (1 - e2) * nu * math.sin(lat)
    sf = s * 1e-6
    rx, ry, rz = [math.radians(v / 3600.0) for v in (rx, ry, rz)]
    x2 = dx + x * (1 + sf) - y * rz + z * ry
    y2 = dy + x * rz + y * (1 + sf) - z * rx
    z2 = dz - x * ry + y * rx + z * (1 + sf)
    e22 = (a2 * a2 - b2 * b2) / (a2 * a2)
    p = math.sqrt(x2 * x2 + y2 * y2)
    lat2 = math.atan2(z2, p * (1 - e22))
    for _ in range(10):
        nu2 = a2 / math.sqrt(1 - e22 * math.sin(lat2) ** 2)
        lat2 = math.atan2(z2 + e22 * nu2 * math.sin(lat2), p)
    return math.degrees(lat2), math.degrees(math.atan2(y2, x2))


def irish_grid_to_wgs84(east, north):
    lat, lon = ig_to_ie65(float(east), float(north))
    return helmert(lat, lon, A, B_,
                   482.530, -130.596, 564.557, -1.042, -0.214, -0.631, 8.150,
                   6378137.000, 6356752.314245)
