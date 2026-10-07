"""GFA-style map for the left third: NAV CANADA Graphic Area Forecast symbology on our 4 inks.

  areas      cloud BKN/OVC        red scalloped border                     (GFA: brown scallops)
             continuous precip    solid border, dithered stipple           (GFA: green, stippled)
             showery/intermittent dashed border, 45° hatching              (GFA: green, hatched)
             fog / vis <= 6 SM    dashed border, light yellow             (GFA: orange, dashed)
             thunderstorm         red TS tag                               (GFA: red TS symbol)
  labels     GFA notation in a box at each cloud area's pole of inaccessibility, e.g.
             "BKN 120/30" (tops / base, hundreds of ft ASL) - "OCNL 3-6SM -SHRA" - "CIGS 15 AGL"
  synoptic   isobars every 2 hPa (GFA uses 4; marked in the notes), WPC analysed fronts and
             pressure centres: drawn if on the map, otherwise an edge tag with distance and motion
  notes      valid time, HGT ASL UNLS NOTED, freezing level

Data
  live   ECCC HRDPS 2.5 km via MSC GeoMet WCS (no key, no rate limit): precipitation rate,
         precipitation character (WEonG), total cloud, fog visibility, thunderstorm probability,
         sea-level pressure. GFS (Open-Meteo, one point per labelled area) for cloud bases / tops
         and the freezing level. WPC coded surface bulletins (analysis + 12 h prog) for fronts.
  demo   Open-Meteo historical-forecast grid (no GeoMet archive), same drawing code.

This is an automated imitation, not a forecaster's GFA: the qualifiers, bases and tops are
heuristics from model fields. Desktop only (numpy, scipy, Pillow, contourpy, shapely, tifffile).
"""
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import contourpy
import numpy as np
import tifffile
from PIL import Image, ImageDraw
from scipy.ndimage import map_coordinates
from shapely.geometry import Point, Polygon
from shapely.ops import polylabel

from . import halftone as ht
from . import weather as wx

PK, PW, PY, PR = wx.PK, wx.PW, wx.PY, wx.PR
H, W = wx.MAP_H, wx.MAP_W
GEOMET_CACHE = os.path.join(wx.CACHE, "geomet")

RAIN_MM = 0.1              # mm/h: inside a precipitation area
CLOUD_BKN = 62.5           # %: 5/8, organised cloud gets a scalloped border
FOG_M = 9656               # m: 6 SM, obstruction to vision
TS_PCT = 30                # %: thunderstorm tag
MIN_AREA = 600             # px²: smaller areas aren't drawn (about 2 x 2 HRDPS cells)
CHAR_SHOWERS, CHAR_INTERMITTENT, CHAR_CONTINUOUS = 1, 2, 3   # WEonG precipitation character (legend order)


# ================================================================== data: live (HRDPS via GeoMet)

def _bbox():
    lat, lon = wx.px_to_ll(np.array([0, W, 0, W]), np.array([0, 0, H, H]))
    return lat.min() - .15, lat.max() + .15, lon.min() - .2, lon.max() + .2


def geomet_layer(layer, valid):
    """One HRDPS coverage over the map area at a valid time -> (array, lon0, lat0, dlon, dlat), cached."""
    os.makedirs(GEOMET_CACHE, exist_ok=True)
    path = os.path.join(GEOMET_CACHE, f"{layer}_{valid:%Y%m%dT%H}.tif")
    if not os.path.exists(path):
        la0, la1, lo0, lo1 = _bbox()
        q = (f"service=WCS&version=2.0.1&request=GetCoverage&coverageId={layer}&subsettingCrs=EPSG:4326"
             f"&outputCrs=EPSG:4326&subset=lat({la0:.3f},{la1:.3f})&subset=lon({lo0:.3f},{lo1:.3f})"
             f"&format=image/tiff&time={valid:%Y-%m-%dT%H:00:00Z}")
        data = urllib.request.urlopen("https://geo.weather.gc.ca/geomet?" + q, timeout=60).read()
        if not data.startswith((b"II", b"MM")):
            raise RuntimeError(f"GeoMet {layer}: {data[:200]!r}")
        open(path, "wb").write(data)
        for old in os.listdir(GEOMET_CACHE):            # keep two days: the cache may live in RAM
            f = os.path.join(GEOMET_CACHE, old)
            if os.path.getmtime(f) < time.time() - 2 * 86400:
                os.remove(f)
    with tifffile.TiffFile(path) as t:
        a = t.asarray().astype(float)
        sx, sy, _ = t.pages[0].tags["ModelPixelScaleTag"].value
        tie = t.pages[0].tags["ModelTiepointTag"].value
    return a, tie[3], tie[4], sx, sy


def _to_map(layer, valid, order=1):
    """Resample a lat/lon coverage onto the map's pixels (pixel-is-area: centres at +0.5)."""
    a, lon0, lat0, dlon, dlat = geomet_layer(layer, valid)
    yy, xx = np.mgrid[0:H, 0:W]
    lat, lon = wx.px_to_ll(xx + .5, yy + .5)
    return map_coordinates(a, [(lat0 - lat) / dlat - .5, (lon - lon0) / dlon - .5], order=order, mode="nearest")


def geomet_fields(valid):
    """All map fields for one valid time (UTC, on the hour)."""
    return dict(
        rate=np.clip(_to_map("HRDPS.CONTINENTAL_RT", valid) * 3600, 0, None),              # kg/(m² s) -> mm/h
        char=np.rint(_to_map("HRDPS-WEonG_2.5km_PrecipCharacter", valid, order=0)).astype(int),
        cloud=np.clip(_to_map("HRDPS.CONTINENTAL_NT", valid), 0, 100),
        fog=_to_map("HRDPS-WEonG_2.5km_LiquidFogVisibility", valid),
        ts=_to_map("HRDPS-WEonG_2.5km_Thunderstorm-Prob", valid),
        mslp=_to_map("HRDPS.CONTINENTAL_PN-SLP", valid) / 100,
        source="HRDPS")


# ================================================================== data: demo (Open-Meteo archive grid)

def openmeteo_fields(path, base_url, hour, **params):
    """Same fields from an Open-Meteo grid (GEM), for dates GeoMet no longer serves."""
    if not os.path.exists(path):
        lat, lon = wx.grid_points()
        q = dict(latitude=",".join(f"{v:.4f}" for v in lat.ravel()), longitude=",".join(f"{v:.4f}" for v in lon.ravel()),
                 hourly="precipitation,showers,cloud_cover,pressure_msl", models=wx.MODEL, timezone="UTC", **params)
        json.dump(json.load(urllib.request.urlopen(base_url + "?" + urllib.parse.urlencode(q), timeout=120)), open(path, "w"))
    data = json.load(open(path))

    def grid(var):
        return np.nan_to_num(np.array([d["hourly"][var][hour] for d in data], float)).reshape(wx.GRID_NY, wx.GRID_NX)

    rate, showers = grid("precipitation"), grid("showers")
    char_grid = np.where(rate < RAIN_MM, 0, np.where(showers > .5 * rate, CHAR_SHOWERS, CHAR_CONTINUOUS))
    return dict(rate=np.clip(wx.upsample(rate), 0, None),
                char=np.rint(map_coordinates(char_grid.astype(float), np.mgrid[0:H, 0:W] * [[[(wx.GRID_NY - 1) / (H - 1)]], [[(wx.GRID_NX - 1) / (W - 1)]]], order=0)).astype(int),
                cloud=np.clip(wx.upsample(grid("cloud_cover")), 0, 100),
                fog=np.full((H, W), 99999.), ts=np.zeros((H, W)),
                mslp=wx.upsample(grid("pressure_msl")), source="GEM")


# ================================================================== data: GFS column (bases, tops, freezing level)

def gfs_column(lat, lon, valid, base_url="https://api.open-meteo.com/v1/forecast", **params):
    """GFS cloud layers at one point as [(amount, base, top)] in hundreds of ft ASL, plus freezing
    level (hundreds of ft ASL) and ground elevation (ft)."""
    hourly = [f"cloud_cover_{l}hPa" for l in wx.LEVELS] + [f"geopotential_height_{l}hPa" for l in wx.LEVELS] + ["freezing_level_height"]
    q = dict(latitude=round(lat, 3), longitude=round(lon, 3), models="gfs_seamless", timezone="UTC", hourly=",".join(hourly), **params)
    if "start_date" not in params:
        q["forecast_hours"] = 24
    try:
        d = json.load(urllib.request.urlopen(base_url + "?" + urllib.parse.urlencode(q), timeout=60))
    except Exception:
        return [], None, 0
    h = d["hourly"]
    want = valid.strftime("%Y-%m-%dT%H:00")
    i = h["time"].index(want) if want in h["time"] else 0
    layers, cur = [], None
    for lvl in wx.LEVELS:
        cc, z = h[f"cloud_cover_{lvl}hPa"][i], h[f"geopotential_height_{lvl}hPa"][i]
        if cc is None or z is None:
            continue
        ft = z * 3.281 / 100
        if cc >= 10:
            cur = [max(cc, cur[0]), cur[1], ft] if cur else [cc, ft, ft]
        elif cur:
            layers.append(cur)
            cur = None
    if cur:
        layers.append(cur)
    fz = h["freezing_level_height"][i]
    return ([(wx.cover_code(c), round(b), round(t)) for c, b, t in layers], None if fz is None else round(fz * 3.281 / 100),
            d.get("elevation", 0) * 3.281)


# ================================================================== data: WPC fronts and pressure centres

WPC_ANALYSIS = "https://tgftp.nws.noaa.gov/data/raw/as/asus01.kwbc.cod.sus.txt"
WPC_PROG = "https://tgftp.nws.noaa.gov/data/raw/fs/fsus02.kwbc.cod.srp.txt"
FRONT_NAMES = {"WARM": "WRMFNT", "COLD": "CDFNT", "STNRY": "STNRY FNT", "OCFNT": "OCLDFNT", "TROF": "TROF"}


def _pos(tok):
    """WPC low-res position: 2 digits °N, then °W (2 or 3 digits)."""
    return int(tok[:2]), -int(tok[2:])


def parse_coded(text, section=0):
    """WPC coded surface bulletin -> dict(highs, lows: [(hPa, lat, lon)], fronts: [(kind, [(lat, lon)])]).
    `section` picks the n-th VALID block (0 = analysis / 12 h prog)."""
    blocks = re.split(r"\n(?=(?:\d+HR PROG )?VALID )", text)
    block = [b for b in blocks if re.match(r"(?:\d+HR PROG )?VALID ", b)][section]
    records, cur = [], None
    for line in block.splitlines()[1:]:
        toks = line.split()
        if not toks:
            continue
        if toks[0] in ("HIGHS", "LOWS") or toks[0] in FRONT_NAMES:
            cur = [toks[0], toks[1:]]
            records.append(cur)
        elif cur and toks[0].isdigit():
            cur[1].extend(toks)                          # wrapped line
        elif toks[0].startswith("$$"):
            break
    out = dict(highs=[], lows=[], fronts=[])
    for kind, toks in records:
        if kind in ("HIGHS", "LOWS"):
            pairs = [t for t in toks if t.isdigit()]
            out[kind.lower()] += [(int(p), *_pos(q)) for p, q in zip(pairs[::2], pairs[1::2])]
        else:
            pts = [_pos(t) for t in toks if t.isdigit()]
            if len(pts) >= 2:
                out["fronts"].append((kind, pts))
    return out


def fetch_wpc():
    path = os.path.join(wx.CACHE, "wpc.json")
    if os.path.exists(path) and "--refresh" not in __import__("sys").argv:
        return json.load(open(path))
    try:
        ana = parse_coded(urllib.request.urlopen(WPC_ANALYSIS, timeout=60).read().decode("latin-1"))
        prog = parse_coded(urllib.request.urlopen(WPC_PROG, timeout=60).read().decode("latin-1"))
    except Exception:
        return None
    json.dump(dict(analysis=ana, prog=prog), open(path, "w"))
    return dict(analysis=ana, prog=prog)


def _nm(a, b):
    """Great-circle distance in NM and initial bearing (deg true) from a to b, both (lat, lon)."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    d = 2 * math.asin(math.sqrt(math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))
    brg = math.degrees(math.atan2(math.sin(lo2 - lo1) * math.cos(la2),
                                  math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)))
    return d * 3440.065, brg % 360


def _compass(b):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((b + 22.5) // 45) % 8]


def _nearest_point(pts, ref):
    """Closest vertex of a polyline (densified to ~0.25°) to ref."""
    dense = []
    for (a, b), (c, e) in zip(pts, pts[1:]):
        n = max(1, int(max(abs(c - a), abs(e - b)) / .25))
        dense += [(a + (c - a) * k / n, b + (e - b) * k / n) for k in range(n)]
    dense.append(pts[-1])
    return min(dense, key=lambda p: _nm(ref, p)[0])


def synoptic(wpc, ref=wx.ORIGIN, radius_nm=700):
    """Nearest front of each kind and nearest H / L, with motion from the 12 h prog."""
    if not wpc:
        return []
    ana, prog = wpc["analysis"], wpc["prog"]
    items = []

    def motion(kind, here, candidates):
        best = min(candidates, key=lambda p: _nm(here, p)[0], default=None)
        if best is None:
            return None
        dist, brg = _nm(here, best)
        if dist > 300:                                  # no plausible match in the prog
            return None
        spd = dist / 12
        return "STNR" if spd < 5 else f"MOVG {_compass(brg)} {spd:.0f}KT"

    for kind in ("COLD", "WARM", "OCFNT", "STNRY", "TROF"):
        cands = [(_nearest_point(pts, ref), pts) for k, pts in ana["fronts"] if k == kind]
        if not cands:
            continue
        p, pts = min(cands, key=lambda c: _nm(ref, c[0])[0])
        dist, brg = _nm(ref, p)
        if dist > radius_nm:
            continue
        prog_pts = [_nearest_point(q, p) for k, q in prog["fronts"] if k == kind]
        items.append(dict(kind=kind, label=FRONT_NAMES[kind], dist=dist, brg=brg, at=p, pts=pts,
                          motion=motion(kind, p, prog_pts)))
    for kind, key in (("L", "lows"), ("H", "highs")):
        if not ana[key]:
            continue
        hpa, la, lo = min(ana[key], key=lambda c: _nm(ref, (c[1], c[2]))[0])
        dist, brg = _nm(ref, (la, lo))
        if dist > radius_nm * 1.5:
            continue
        items.append(dict(kind=kind, label=f"{kind} {hpa}", dist=dist, brg=brg, at=(la, lo), pts=None,
                          motion=motion(kind, (la, lo), [(a, b) for _, a, b in prog[key]])))
    return items


# ================================================================== geometry

def areas(field, level, upper=1e12, min_area=MIN_AREA):
    """Filled contour of field >= level -> shapely Polygons in map pixels, smoothed and filtered."""
    gen = contourpy.contour_generator(z=field, fill_type="OuterOffset")
    pts_list, offs_list = gen.filled(level, upper)
    out = []
    for pts, offs in zip(pts_list, offs_list):
        rings = [pts[offs[k]:offs[k + 1]] for k in range(len(offs) - 1)]
        holes = [r for r in rings[1:] if len(r) >= 4 and Polygon(r).area >= min_area]
        poly = Polygon(rings[0], holes).buffer(0).simplify(1.2)
        for p in getattr(poly, "geoms", [poly]):
            if p.area >= min_area:
                out.append(p)
    return out


def _mask(poly):
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    d.polygon(list(poly.exterior.coords), fill=255)
    for hole in poly.interiors:
        d.polygon(list(hole.coords), fill=0)
    return np.asarray(m) > 127


def _resample(coords, step):
    """Points every `step` px along a closed ring."""
    c = np.asarray(coords)
    seg = np.hypot(*np.diff(c, axis=0).T)
    total = seg.sum()
    if total < step * 3:
        return c
    t = np.linspace(0, total, max(4, int(total / step)) + 1)
    cum = np.concatenate([[0], np.cumsum(seg)])
    return np.stack([np.interp(t, cum, c[:, 0]), np.interp(t, cum, c[:, 1])], 1)


def _on_edge(p):
    return p[0] <= 1.5 or p[1] <= 1.5 or p[0] >= W - 2.5 or p[1] >= H - 2.5


def scallops(d, poly, colour=PR, step=11, width=2):
    """GFA cloud border: semicircles bulging outward along the ring (not along the map edge)."""
    for ring in [poly.exterior] + list(poly.interiors):
        pts = _resample(ring.coords, step)
        for a, b in zip(pts, pts[1:]):
            if _on_edge(a) and _on_edge(b):
                continue
            mid = (a + b) / 2
            ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
            r = np.hypot(*(b - a)) / 2
            normal = np.array([math.cos(math.radians(ang - 90)), math.sin(math.radians(ang - 90))])
            outward = ang - 90 if not poly.contains(Point(*(mid + normal * 2))) else ang + 90
            d.arc((mid[0] - r, mid[1] - r, mid[0] + r, mid[1] + r), outward - 90, outward + 90, fill=colour, width=width)


def outline(d, poly, colour=PK, width=2, dash=None):
    """Solid or dashed border, skipping the stretches that run along the map edge."""
    for ring in [poly.exterior] + list(poly.interiors):
        pts = _resample(ring.coords, 3)
        run = 0.0
        for a, b in zip(pts, pts[1:]):
            seg = float(np.hypot(*(b - a)))
            on = True if dash is None else (run % sum(dash)) < dash[0]
            run += seg
            if on and not (_on_edge(a) and _on_edge(b)):
                d.line((tuple(a), tuple(b)), fill=colour, width=width)


def hatch(img, poly, step=7, colour=PK):
    """45° hatching (showery / intermittent precipitation)."""
    m = _mask(poly)
    yy, xx = np.mgrid[0:H, 0:W]
    lines = ((xx + yy) % step) == 0
    a = np.asarray(img).copy()
    a[m & lines] = colour
    img.paste(Image.fromarray(a[:H, :W]), (0, 0))


def stipple(img, poly, frac=.16, ink=ht.BLACK):
    """Ordered-dither stipple (continuous precipitation) or a light tint (fog)."""
    m = _mask(poly)
    f = np.zeros((H, W, 4))
    f[..., ht.WHITE] = 1
    f[m, ink], f[m, ht.WHITE] = frac, 1 - frac
    idx = ht.render_stacked(f, ht.bayer_screen(H, W, 8))
    a = np.asarray(img).copy()
    dots = m & (idx == ink)
    a[dots] = ht.PURE[ink]
    img.paste(Image.fromarray(a[:H, :W]), (0, 0))


# ================================================================== placement

class Occupied:
    """Rectangles already used on the map, so text boxes can avoid each other."""

    def __init__(self):
        self.rects = []

    def free(self, r, pad=2):
        return (r[0] >= 2 and r[1] >= 2 and r[2] <= W - 2 and r[3] <= H - 2 and
                not any(r[0] < q[2] + pad and r[2] > q[0] - pad and r[1] < q[3] + pad and r[3] > q[1] - pad for q in self.rects))

    def take(self, r):
        self.rects.append(tuple(r))
        return r

    def place(self, size, candidates):
        """First candidate centre where a box of `size` fits; None if nowhere."""
        w, h = size
        for cx, cy in candidates:
            r = (int(cx - w / 2), int(cy - h / 2), int(cx - w / 2) + w, int(cy - h / 2) + h)
            if self.free(r):
                return self.take(r)
        return None


def spiral(cx, cy, step=8, rings=14):
    yield cx, cy
    for k in range(1, rings + 1):
        for a in range(0, 360, 30):
            yield cx + math.cos(math.radians(a)) * k * step, cy + math.sin(math.radians(a)) * k * step


# ================================================================== GFA text

def _vis_wx(rate, showery):
    """Visibility range and weather from a precipitation rate (mm/h), GFA style."""
    kind = "SHRA" if showery else "RA"
    if rate < 2.5:
        return "P6SM", f"-{kind}"
    if rate < 7.6:
        return "3-6SM", kind
    return "1-3SM", f"+{kind}"


def _qualifier(frac, showery):
    if showery:
        return "ISOL" if frac <= .25 else "OCNL" if frac <= .5 else "FRQ"
    return "LCA" if frac <= .25 else "PTCHY" if frac <= .5 else "XTNSV"


def area_text(fields, poly, column):
    """GFA lines for one cloud area: cloud (amount + tops/base), then vis & weather, then CIGS."""
    m = _mask(poly)
    cloud = fields["cloud"][m].mean()
    layers, _, elev_ft = column
    amt = "OVC" if cloud >= 87.5 else "BKN"
    lines = []
    sig = [(c, b, t) for c, b, t in layers if c in ("BKN", "OVC")] or layers
    if sig:
        base, top = min(b for _, b, _ in sig), max(t for _, _, t in sig)
        lines.append([amt + (" LYRS" if len(sig) > 1 else ""), ("frac", top, max(base, 1))])
    else:
        lines.append([amt])
    wet = m & (fields["rate"] >= RAIN_MM)
    if wet.sum() > 0.05 * m.sum():
        showery = np.mean(np.isin(fields["char"][wet], (CHAR_SHOWERS, CHAR_INTERMITTENT))) >= .5
        vis, wxs = _vis_wx(np.percentile(fields["rate"][wet], 90), showery)
        lines.append([f"{_qualifier(wet.sum() / m.sum(), showery)} {vis} {wxs}"])
    else:
        lines.append(["P6SM"])
    if sig:
        agl = (min(b for _, b, _ in sig) * 100 - elev_ft) / 100
        if agl < 30:
            lines.append([f"CIGS {max(1, round(agl))} AGL"])
    return lines


def text_size(d, lines, fs=13):
    big, small = wx.condensed(fs, True), wx.condensed(fs - 3, True)
    w = 0
    for line in lines:
        lw = sum(max(d.textlength(str(t[1]), font=small), d.textlength(str(t[2]), font=small)) + 6 if isinstance(t, tuple)
                 else d.textlength(t, font=big) + 4 for t in line)
        w = max(w, lw)
    return int(w + 8), int(len(lines) * (fs + 3) + 6 + (4 if any(isinstance(t, tuple) for t in lines[0]) else 0))


def text_block(img, d, center, lines, fs=13, rect=None):
    """Boxed GFA text; ('frac', top, base) tokens render as a stacked fraction."""
    big, small = wx.condensed(fs, True), wx.condensed(fs - 3, True)
    lh = fs + 3
    widths = []
    for line in lines:
        w = 0
        for tok in line:
            if isinstance(tok, tuple):
                w += max(d.textlength(str(tok[1]), font=small), d.textlength(str(tok[2]), font=small)) + 6
            else:
                w += d.textlength(tok, font=big) + 4
        widths.append(w)
    bw, bh = max(widths) + 8, len(lines) * lh + 6 + (4 if any(isinstance(t, tuple) for t in lines[0]) else 0)
    if rect:
        x0, y0 = rect[0], rect[1]
    else:
        x0 = int(min(max(center[0] - bw / 2, 3), W - bw - 3))
        y0 = int(min(max(center[1] - bh / 2, 3), H - bh - 22))
    d.rectangle((x0, y0, x0 + bw, y0 + bh), fill=PW, outline=PK)
    y = y0 + 3 + (2 if any(isinstance(t, tuple) for t in lines[0]) else 0)
    for line, w in zip(lines, widths):
        x = x0 + (bw - w) / 2
        for tok in line:
            if isinstance(tok, tuple):
                fw = max(d.textlength(str(tok[1]), font=small), d.textlength(str(tok[2]), font=small))
                cx = x + 2 + fw / 2
                d.text((cx, y - 2), str(tok[1]), font=small, fill=PK, anchor="ma")
                d.line((x + 1, y + lh / 2, x + fw + 3, y + lh / 2), fill=PK)
                d.text((cx, y + lh / 2 + 1), str(tok[2]), font=small, fill=PK, anchor="ma")
                x += fw + 6
            else:
                d.text((x, y), tok, font=big, fill=PK)
                x += d.textlength(tok, font=big) + 4
        y += lh
    return (x0, y0, x0 + bw, y0 + bh)


# ================================================================== synoptic drawing

def draw_front(d, pts_px, kind, step=26):
    """Front line with pips on one side (WPC doesn't say which; we use the left of the line order)."""
    colour = PR if kind == "WARM" else PK
    if kind == "TROF":
        outline_pts = pts_px
        for k, (a, b) in enumerate(zip(outline_pts, outline_pts[1:])):
            if k % 2 == 0:
                d.line((a, b), fill=PK, width=2)
        return
    d.line(pts_px, fill=colour, width=3)
    pts = _resample(pts_px + [pts_px[-1]], step)
    for k, (a, b) in enumerate(zip(pts, pts[1:])):
        mid, v = (a + b) / 2, (b - a)
        n = np.hypot(*v) or 1
        u, nrm = v / n, np.array([v[1], -v[0]]) / n
        c = PR if (kind == "WARM" or (kind in ("STNRY", "OCFNT") and k % 2)) else PK
        side = nrm if not (kind == "STNRY" and k % 2) else -nrm
        if kind == "WARM" or (kind in ("STNRY", "OCFNT") and k % 2):
            d.pieslice((mid[0] - 6, mid[1] - 6, mid[0] + 6, mid[1] + 6),
                       math.degrees(math.atan2(side[1], side[0])) - 90, math.degrees(math.atan2(side[1], side[0])) + 90, fill=c)
        else:
            d.polygon([tuple(mid - u * 6), tuple(mid + u * 6), tuple(mid + side * 9)], fill=c)


def edge_tag(img, d, item, occ):
    """Off-map feature: a tag against the map edge in the feature's direction, holding a glyph
    (pip or H / L), an arrow pointing at the feature, distance and motion."""
    b = math.radians(item["brg"])
    vx, vy = math.sin(b), -math.cos(b)
    txt = f"{item['label']} {item['dist']:.0f}NM {_compass(item['brg'])}" + (f" {item['motion']}" if item["motion"] else "")
    f = wx.condensed(13, True)
    tw, th = int(d.textlength(txt, font=f)) + 40, 18
    t = min((W / 2) / abs(vx) if abs(vx) > 1e-6 else 1e9, (H / 2) / abs(vy) if abs(vy) > 1e-6 else 1e9)
    ex, ey = W / 2 + vx * t, H / 2 + vy * t                    # where the bearing leaves the map
    ex, ey = min(max(ex, tw / 2 + 3), W - tw / 2 - 3), min(max(ey, th / 2 + 3), H - th / 2 - 3)
    r = occ.place((tw, th), spiral(ex, ey, step=9, rings=30))  # as close to that edge point as space allows
    if not r:
        return
    x0, y0 = r[0], r[1]
    d.rectangle((x0, y0, r[2], r[3]), fill=PW, outline=PK)
    gx, gy = x0 + 10, y0 + 9
    if item["kind"] == "WARM":
        d.pieslice((gx - 6, gy - 4, gx + 6, gy + 8), 180, 360, fill=PR)
    elif item["kind"] in ("COLD", "OCFNT", "STNRY"):
        d.polygon([(gx - 6, gy + 5), (gx + 6, gy + 5), (gx, gy - 5)], fill=PK)
    elif item["kind"] == "TROF":
        d.line((gx - 6, gy, gx - 1, gy), fill=PK, width=2)
        d.line((gx + 2, gy, gx + 6, gy), fill=PK, width=2)
    else:
        d.text((gx, gy), item["kind"], font=wx.serif(14), fill=PR if item["kind"] == "L" else PK, anchor="mm")
    ax, ay = x0 + 26, y0 + 9                                   # arrow toward the feature
    d.line((ax - vx * 6, ay - vy * 6, ax + vx * 5, ay + vy * 5), fill=PK, width=2)
    px_, py_ = -vy, vx
    tip = (ax + vx * 7, ay + vy * 7)
    d.polygon([tip, (tip[0] - vx * 5 + px_ * 3, tip[1] - vy * 5 + py_ * 3), (tip[0] - vx * 5 - px_ * 3, tip[1] - vy * 5 - py_ * 3)], fill=PK)
    d.text((x0 + 36, y0 + 9), txt, font=f, fill=PK, anchor="lm")


def isobars(img, d, mslp, step=2):
    lo, hi = math.ceil(np.nanmin(mslp) / step) * step, math.floor(np.nanmax(mslp) / step) * step
    gen = contourpy.contour_generator(z=mslp, line_type="Separate")
    for level in range(int(lo), int(hi) + 1, step):
        for line in gen.lines(level):
            if len(line) < 8:
                continue
            d.line([tuple(p) for p in line], fill=PK, width=1)
            p = line[len(line) // 2]
            wx.boxed_text(d, (float(p[0]), float(p[1])), str(level), 10, "mm", bold=False, pad=1)


# ================================================================== the map

def draw_gfa_map(img, fields, valid, wpc=None, column_fn=None, fzlvl=None):
    """Left third, GFA style. column_fn(lat, lon) -> gfs_column(...) result for area labels."""
    land, rings = wx.land_mask()
    m = Image.new("RGB", (W, H), PW)
    d = ImageDraw.Draw(m)
    d.fontmode = "1"
    for pts in rings:                                          # geography: thin, so the weather leads
        d.line(pts + [pts[0]], fill=PK, width=1)

    precip = areas(np.where(fields["rate"] >= RAIN_MM, fields["rate"], 0), RAIN_MM)
    for poly in precip:
        msk = _mask(poly)
        showery = np.mean(np.isin(fields["char"][msk & (fields["rate"] >= RAIN_MM)], (CHAR_SHOWERS, CHAR_INTERMITTENT))) >= .5
        if showery:
            hatch(m, poly)
        else:
            stipple(m, poly)
    d = ImageDraw.Draw(m)
    d.fontmode = "1"
    for poly in precip:
        msk = _mask(poly)
        showery = np.mean(np.isin(fields["char"][msk & (fields["rate"] >= RAIN_MM)], (CHAR_SHOWERS, CHAR_INTERMITTENT))) >= .5
        outline(d, poly, PK, 2, dash=(7, 5) if showery else None)
    fog = areas(np.where(fields["fog"] <= FOG_M, FOG_M + 1 - fields["fog"], 0), 1)
    for poly in fog:
        stipple(m, poly, .35, ht.YELLOW)
    d = ImageDraw.Draw(m)
    d.fontmode = "1"
    for poly in fog:
        outline(d, poly, PK, 2, dash=(4, 4))
    if "mslp" in fields:
        isobars(m, d, fields["mslp"])
    clouds = areas(fields["cloud"], CLOUD_BKN)
    for poly in clouds:
        scallops(d, poly)

    occ = Occupied()
    title = f"GFA-STYLE  VALID {valid:%d/%H}Z"
    notes = f"HGT ASL UNLS NOTED · ISOBARS 2HPA · {fields['source']}" + (f" · FZLVL {fzlvl}" if fzlvl else "")
    for txt, size, xy, anchor in ((title, 14, (W - 6, 6), "ra"), (notes, 10, (W - 6, H - 5), "rb"), ("updated 00:00", 10, (6, H - 5), "lb")):
        l, t_, r_, b_ = d.textbbox(xy, txt, font=wx.font(size, size == 14), anchor=anchor)
        occ.take((l - 5, t_ - 5, r_ + 5, b_ + 5))
    town_px = []
    for name, lat, lon, side, dot in wx.TOWNS:
        x, y = (float(v) for v in wx.ll_to_px(lat, lon))
        if dot:
            anchor, tx = ("lm", x + 9) if side == "r" else ("rm", x - 9)
            l, t_, r_, b_ = d.textbbox((tx, y), name, font=wx.font(14), anchor=anchor)
            occ.take((min(l, x - 5) - 3, t_ - 3, max(r_, x + 5) + 3, b_ + 3))
            town_px.append((name, x, y, tx, anchor))

    # synoptic: on-map fronts drawn, everything else as edge tags
    tags = []
    for item in synoptic(wpc):
        if item["pts"]:
            px = [tuple(float(v) for v in wx.ll_to_px(la, lo)) for la, lo in item["pts"]]
            if any(0 <= x < W and 0 <= y < H for x, y in px):
                draw_front(d, px, item["kind"])
                continue
        tags.append(item)
    fronts = sorted((t for t in tags if t["kind"] not in ("H", "L")), key=lambda i: i["dist"])
    centres = {t["kind"]: t for t in tags if t["kind"] in ("H", "L")}
    chosen = fronts[:1] + [centres[k] for k in ("L", "H") if k in centres] + fronts[1:2]
    for item in chosen[:4]:                                     # nearest front, low, high, then a second front
        edge_tag(m, d, item, occ)

    for name, x, y, tx, anchor in town_px:                     # towns, as stickers
        wx.sticker_text(m, (tx, y), name, 14, anchor, grow=2)
        d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=PK, outline=PW, width=2)

    for poly in sorted(clouds, key=lambda p: -p.area)[:3]:      # GFA text, one box per (big) cloud area
        pt = polylabel(poly, tolerance=3)
        lat, lon = (float(v) for v in wx.px_to_ll(pt.x, pt.y))
        col = column_fn(lat, lon) if column_fn else ([], None, 0)
        lines = area_text(fields, poly, col)
        r = occ.place(text_size(d, lines), (c for c in spiral(pt.x, pt.y) if poly.contains(Point(*c))))
        if r:
            text_block(m, d, None, lines, rect=r)
    for item in np.argwhere(fields["ts"] >= TS_PCT)[:1]:        # thunderstorm tag at the first hit
        y, x = item
        wx.boxed_text(d, (float(x), float(y)), "TS", 13, "mm", fg=PW, bg=PR, pad=3)

    wx.boxed_text(d, (W - 6, 6), title, 14, "ra", fg=PY, bg=PK, pad=4)
    wx.boxed_text(d, (W - 6, H - 5), notes, 10, "rb", bold=False, pad=2)
    img.paste(m, (0, 0))
