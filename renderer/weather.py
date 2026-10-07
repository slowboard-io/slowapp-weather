"""Weather dashboard prototype: a rotated map of the east side of Vancouver Island
(Qualicum Beach to Victoria, across to Boundary Bay) with next-hour cloud cover (grey)
and rain (Windy-style yellow -> orange -> red -> maroon), Bayer-dithered, plus date,
Victoria conditions and upcoming events on the right.

  python3 -m renderer.weather [--refresh] [--events FILE] [--feeds-out DIR]
      -> out/weather/live.*   (live Open-Meteo, GEM model; METAR from aviationweather.gov)
         out/weather/storm.*  (archived storm, 2025-12-10 02-03h)

--feeds-out DIR also writes the live cards as contract-2 feeds (docs/SLOWBOARD.md §8.6), one per
third: DIR/feeds/<left|middle|right>/deck.json and cards/<id>.png (2-bit PNGs in the four inks).

Each output is .bin (packed for the panel), .png (exact panel pixels), _sim.png (estimated
inks, slightly blurred) and _zoom.png (4x crop of the map). API responses are cached in
cache/weather/; --refresh fetches them again. Events come from --events, else the synced
calendars (/var/lib/epaper/calendar/agenda.json, from tools/calsync.py), else config/events.json,
else the made-up renderer/assets/events.example.json.
Needs numpy, scipy and Pillow (requirements-desktop.txt).
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from scipy.ndimage import map_coordinates

from epd.epd10in85g import HEIGHT, WIDTH
from epd.layout import BODY_H, ORDER, PANELS
from epd.pack import pack

from . import cards as decks
from . import events as cal
from . import halftone as ht
from .painter import Painter

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ASSETS = os.path.join(HERE, "assets")
FONTS = os.path.join(ASSETS, "fonts")
COAST = os.path.join(ASSETS, "geo", "coast.geojson")   # made by tools/make_coast.py
RUN = os.environ.get("EPAPER_RUN")                    # on the board, /run/epaper: renders and cache live in RAM
CACHE = os.path.join(RUN, "cache") if RUN else os.path.join(REPO, "cache", "weather")
OUT = os.path.join(RUN, "out") if RUN else os.path.join(REPO, "out", "weather")
K, W, Y, R = ht.BLACK, ht.WHITE, ht.YELLOW, ht.RED


def _zone():
    """The local zone, from $TZ (slowbhub and the pane kit set it from the device's prefs), else
    America/Vancouver. Data is fetched in it, so data times and feed validity agree."""
    name = os.environ.get("TZ", "").lstrip(":")
    try:
        return name, ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return "America/Vancouver", ZoneInfo("America/Vancouver")


ZONE_NAME, ZONE = _zone()
PK, PW, PY, PR = (0, 0, 0), (255, 255, 255), (255, 255, 0), (255, 0, 0)

# ------------------------------------------------------------------ projection
# The panel is split into thirds whose edges suit partial refresh (window x0 % 4 == 0, x1 % 4 == 3):
# left 0-451 sits wholly on the master IC (x < 680), right 908-1359 wholly on the slave, so either
# side can be refreshed on its own; the middle 452-907 straddles both.
THIRDS = ((0, 451), (452, 907), (908, 1359))

# Flat local projection centred on Victoria (km east / km north), optionally rotated
# counter-clockwise by ROT_DEG. The map fills the left third, fitted around FIT (+ MARGIN_KM).
ROT_DEG = 0.0
ORIGIN = (48.4284, -123.3656)
FIT = [(49.3477, -124.4432), (49.1659, -123.9401), (48.4284, -123.3656), (48.43, -123.30), (48.3740, -123.7262),
       (49.0059, -123.0817), (49.045, -122.975), (49.2827, -123.1207)]  # Qualicum..Victoria, Boundary Bay, Vancouver
MARGIN_KM = 8.0
MARGIN_V_KM = 6.0     # top and bottom: a little less, so the shorter body keeps the map's width-fitted scale
KX = 111.32 * np.cos(np.radians(ORIGIN[0]))
KY = 111.32
COS, SIN = np.cos(np.radians(ROT_DEG)), np.sin(np.radians(ROT_DEG))
_fx = np.array([(lo - ORIGIN[1]) * KX for _, lo in FIT])
_fy = np.array([(la - ORIGIN[0]) * KY for la, _ in FIT])
_fu, _fv = _fx * COS - _fy * SIN, _fx * SIN + _fy * COS
MAP_W = THIRDS[0][1] + 1
MAP_H = BODY_H                                        # the peek strip sits under the map
KM_PX = max((np.ptp(_fv) + 2 * MARGIN_V_KM) / MAP_H, (np.ptp(_fu) + 2 * MARGIN_KM) / MAP_W)
U0 = (_fu.min() + _fu.max()) / 2 - MAP_W * KM_PX / 2
U1 = U0 + MAP_W * KM_PX
V1 = (_fv.min() + _fv.max()) / 2 + MAP_H * KM_PX / 2
V0 = V1 - MAP_H * KM_PX


def ll_to_px(lat, lon):
    x = (np.asarray(lon, float) - ORIGIN[1]) * KX
    y = (np.asarray(lat, float) - ORIGIN[0]) * KY
    u, v = x * COS - y * SIN, x * SIN + y * COS
    return (u - U0) / KM_PX, (V1 - v) / KM_PX


def px_to_ll(px, py):
    u, v = U0 + np.asarray(px, float) * KM_PX, V1 - np.asarray(py, float) * KM_PX
    x, y = u * COS + v * SIN, -u * SIN + v * COS
    return ORIGIN[0] + y / KY, ORIGIN[1] + x / KX


# name, lat, lon, label side ("r"/"l"), dot?
TOWNS = [("Qualicum Beach", 49.3477, -124.4432, "r", True), ("Nanaimo", 49.1659, -123.9401, "r", True),
         ("Duncan", 48.7787, -123.7079, "l", True), ("Salt Spring", 48.8547, -123.5030, "r", True),
         ("Sidney", 48.6506, -123.3986, "r", True), ("Victoria", 48.4284, -123.3656, "l", True),
         ("Sooke", 48.3740, -123.7262, "l", True), ("Vancouver", 49.2827, -123.1207, "l", True),
         ("Tsawwassen", 49.0059, -123.0817, "l", True)]

# ------------------------------------------------------------------ style
WATER_ROW, WATER_DOT = 6, 2   # sea texture: a dotted line every 6th row, a dot every 2nd pixel
CLOUD_K = 0.28        # extra grey at 100 % cloud cover
# rain colour ramp, mm/h -> ink fractions (K, W, Y, R), interpolated on log(mm/h)
RAIN_STOPS = [(0.1, (0, .65, .35, 0)), (0.5, (0, .25, .75, 0)), (1.5, (0, 0, 1, 0)),
              (3.0, (0, 0, .5, .5)), (6.0, (0, 0, 0, 1)), (12.0, (.3, 0, 0, .7))]
LEGEND = [0.3, 1, 2, 4, 8, 12]
MODEL = "gem_seamless"               # Environment Canada GEM (HRDPS 2.5 km for the first 48 h)
GRID_NX, GRID_NY = 18, 18             # forecast sample points across the map (~7 km apart)


def font(size, bold=True):
    return ImageFont.truetype(os.path.join(FONTS, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), size)


def condensed(size, bold=False):
    return ImageFont.truetype(os.path.join(FONTS, "DejaVuSansCondensed-Bold.ttf" if bold else "DejaVuSansCondensed.ttf"), size)


SERIF = os.path.join(FONTS, "AlfaSlabOne-Regular.ttf")   # Alfa Slab One (OFL)
_serif_cache = {}


def serif(size, wght=None):
    """Alfa Slab One for headings: one very heavy weight, so it stays solid in 1-bit."""
    if size not in _serif_cache:
        _serif_cache[size] = ImageFont.truetype(SERIF, size)
    return _serif_cache[size]


# ------------------------------------------------------------------ data

def grid_points():
    gx, gy = np.meshgrid(np.linspace(0, MAP_W - 1, GRID_NX), np.linspace(0, MAP_H - 1, GRID_NY))
    return px_to_ll(gx, gy)


def fetch_grid(path, base_url, **params):
    """Open-Meteo hourly cloud cover + precipitation at every grid point (one request), cached."""
    if os.path.exists(path) and "--refresh" not in sys.argv:
        return json.load(open(path))
    lat, lon = grid_points()
    q = dict(latitude=",".join(f"{v:.4f}" for v in lat.ravel()), longitude=",".join(f"{v:.4f}" for v in lon.ravel()),
             hourly="cloud_cover,precipitation", timezone=ZONE_NAME, models=MODEL, **params)
    data = json.load(urllib.request.urlopen(base_url + "?" + urllib.parse.urlencode(q), timeout=120))
    json.dump(data, open(path, "w"))
    return data


def fetch_victoria(path, base_url="https://api.open-meteo.com/v1/forecast", **params):
    """Victoria hourly (for 'now' and the rain callouts) + daily (for the 5-day strip)."""
    if os.path.exists(path) and "--refresh" not in sys.argv:
        return json.load(open(path))
    q = dict(latitude=ORIGIN[0], longitude=ORIGIN[1], timezone=ZONE_NAME,
             hourly="temperature_2m,weather_code,precipitation,precipitation_probability,wind_speed_10m,cloud_cover", models=MODEL,
             daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,sunrise,sunset",
             **params)
    data = json.load(urllib.request.urlopen(base_url + "?" + urllib.parse.urlencode(q), timeout=60))
    json.dump(data, open(path, "w"))
    return data


def fetch_aqi(path, **params):
    """US AQI at Victoria, current or (with start/end dates) hourly history."""
    if os.path.exists(path) and "--refresh" not in sys.argv:
        return json.load(open(path))
    q = dict(latitude=ORIGIN[0], longitude=ORIGIN[1], timezone=ZONE_NAME, **params)
    data = json.load(urllib.request.urlopen("https://air-quality-api.open-meteo.com/v1/air-quality?" + urllib.parse.urlencode(q), timeout=60))
    json.dump(data, open(path, "w"))
    return data


def now_conditions(vic, now):
    h = vic["hourly"]
    i = h["time"].index(now.strftime("%Y-%m-%dT%H:00"))
    return dict(temperature_2m=h["temperature_2m"][i], weather_code=h["weather_code"][i],
                wind_speed_10m=h["wind_speed_10m"][i]), i


AQI_BAD = 101          # US AQI: only show from "unhealthy for sensitive groups" up
RAIN_MM = 0.1


def aqi_label(a):
    return "poor" if a <= 150 else "unhealthy" if a <= 200 else "very unhealthy" if a <= 300 else "hazardous"


def callouts(vic, now, aqi):
    """Only the lines that matter right now: rain start or stop within 24 h, and bad air."""
    h = vic["hourly"]
    _, i = now_conditions(vic, now)
    # precipitation at T is the total for (T-1h, T]; index i+1 is the hour we're in
    nxt = list(zip(h["time"][i + 1:i + 26], h["precipitation"][i + 1:i + 26], h["precipitation_probability"][i + 1:i + 26]))
    out = []

    def when(t):
        dt = datetime.fromisoformat(t)
        start = dt.replace(hour=(dt.hour - 1) % 24) if dt.hour else dt.replace(hour=23, day=dt.day - 1)
        return start.strftime("%H:%M") + ("" if start.date() == now.date() else start.strftime(" %a"))

    if nxt and (nxt[0][1] or 0) >= RAIN_MM:
        stop = next((t for t, mm, _ in nxt[1:] if (mm or 0) < RAIN_MM), None)
        out.append((f"Rain stops ~{when(stop)}" if stop else "Rain for 24 h+", PR))
    else:
        start = next((t for t, mm, pp in nxt if (mm or 0) >= RAIN_MM or (pp or 0) >= 60), None)
        cutoff = datetime.combine(now.date() + timedelta(days=1), datetime.min.time()).replace(hour=6)
        if start and datetime.fromisoformat(start) - timedelta(hours=1) < cutoff:   # today or overnight only
            out.append((f"Rain from ~{when(start)}", PR))
    if aqi is not None and aqi >= AQI_BAD:
        out.append((f"AQI {aqi} {aqi_label(aqi)}", PR))
    return out


# airports: METAR where the field reports one, otherwise Open-Meteo model wind at the field
AIRPORTS = [("YYJ", "CYYJ", 48.6469, -123.4258), ("YCD", "CYCD", 49.0550, -123.8700), ("YVR", "CYVR", 49.1939, -123.1844),
            ("CAT4", None, 49.3372, -124.3939), ("ZBB", None, 49.0742, -123.0075)]
# true headings from OurAirports runways.csv (public domain); main runways thick, secondary thin
RUNWAYS = {"YYJ": [(105.0, True, 0), (151.9, False, 0)],           # 09/27, 14/32
           "YCD": [(181.0, True, 0)],                               # 16/34
           "YVR": [(100.0, True, -5), (100.3, True, 5)],            # 08L/26R, 08R/26L
           "CAT4": [(126.8, True, 0)],                              # 11/29
           "ZBB": [(90.0, True, 0), (144.0, False, 0)]}             # 07/25, 13/31


def flight_category(ceil, visib):
    """FAA-style categories, used only if the METAR feed doesn't supply one."""
    v = 10.0 if visib in (None, "10+") else float(str(visib).rstrip("+"))
    c = 99999 if ceil is None else ceil
    if c < 500 or v < 1:
        return "LIFR"
    if c < 1000 or v < 3:
        return "IFR"
    if c <= 3000 or v <= 5:
        return "MVFR"
    return "VFR"


# Fields without a METAR: an ECCC automatic station at the field gives real wind / temperature /
# visibility (SWOB, MSC GeoMet), and GFS gives the sky (cloud cover per pressure level -> layers).
FIELD_STATIONS = {"CAT4": "QUALICUM BEACH AIRPORT", "ZBB": "Vancouver Boundary Bay"}
LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300]
KMH_KT = 1 / 1.852


def swob_latest(name, lat, lon):
    """The named ECCC station's latest top-of-the-hour observation near (lat, lon), or None.
    Like a METAR it changes once an hour; the minutely reports in between would flap the display."""
    since = (datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = dict(bbox=f"{lon - .03},{lat - .03},{lon + .03},{lat + .03}", datetime=f"{since}/..", limit=200, f="json")
    try:
        fs = json.load(urllib.request.urlopen("https://api.weather.gc.ca/collections/swob-realtime/items?" + urllib.parse.urlencode(q), timeout=60))["features"]
    except Exception:
        return None
    fs = [f["properties"] for f in fs if f["properties"].get("stn_nam-value") == name]
    hourly = [p for p in fs if p["date_tm-value"][14:16] == "00"]
    return max(hourly or fs, key=lambda p: p["date_tm-value"]) if fs else None


def cover_code(pct):
    return None if pct < 10 else "FEW" if pct < 30 else "SCT" if pct < 55 else "BKN" if pct < 90 else "OVC"


WX_CODES = {45: "FG", 48: "FG", 51: "-DZ", 53: "DZ", 55: "+DZ", 61: "-RA", 63: "RA", 65: "+RA",
            71: "-SN", 73: "SN", 75: "+SN", 80: "-SHRA", 81: "SHRA", 82: "+SHRA", 95: "TS"}


def model_sky(lat, lon, elev_m):
    """GFS now: cloud layers (contiguous cloudy pressure levels, base = lowest level's height AGL),
    visibility in SM, and a present-weather string."""
    hourly = [f"cloud_cover_{l}hPa" for l in LEVELS] + [f"geopotential_height_{l}hPa" for l in LEVELS] + ["visibility", "weather_code"]
    q = dict(latitude=lat, longitude=lon, models="gfs_seamless", forecast_hours=1, timezone="UTC", hourly=",".join(hourly))
    h = json.load(urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q), timeout=60))["hourly"]
    layers, cur = [], None
    for l in LEVELS:
        cc, z = h[f"cloud_cover_{l}hPa"][0], h[f"geopotential_height_{l}hPa"][0]
        if cc is None or z is None or z < elev_m:
            continue
        code = cover_code(cc)
        if code and cur is None:
            cur = [cc, int(round((z - elev_m) * 3.281 / 100)) * 100]
        elif code and cur:
            cur[0] = max(cur[0], cc)
        elif not code and cur:
            layers.append((cover_code(cur[0]), max(cur[1], 100)))
            cur = None
    if cur:
        layers.append((cover_code(cur[0]), max(cur[1], 100)))
    vis = h["visibility"][0]
    return layers, (None if vis is None else min(15, round(vis / 1609.34))), WX_CODES.get(h["weather_code"][0], "")


def fetch_wind(path):
    if os.path.exists(path) and "--refresh" not in sys.argv:
        return json.load(open(path))
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    try:                                                # modelled fields keep their sky for the hour, like a METAR
        prev = {w["id"]: w for w in json.load(open(path)) if w.get("modelled") and w.get("hour") == hour}
    except (OSError, ValueError):
        prev = {}
    ids = ",".join(icao for _, icao, _, _ in AIRPORTS if icao)
    metars = {m["icaoId"]: m for m in json.load(urllib.request.urlopen(
        f"https://aviationweather.gov/api/data/metar?ids={ids}&format=json", timeout=60))}
    out = []
    for name, icao, lat, lon in AIRPORTS:
        m = metars.get(icao) if icao else None
        if m:
            wdir = m.get("wdir")
            ceil = min((c["base"] for c in m.get("clouds") or [] if c.get("cover") in ("BKN", "OVC", "OVX", "VV")
                        and c.get("base") is not None), default=None)
            layers = [(c.get("cover"), c.get("base")) for c in m.get("clouds") or [] if c.get("cover")]
            out.append(dict(id=name, dir=None if wdir in (None, "VRB") else int(wdir), spd=m.get("wspd") or 0,
                            gst=m.get("wgst"), ceil=ceil, cat=m.get("fltCat") or flight_category(ceil, m.get("visib")),
                            layers=layers, wx=m.get("wxString") or "", vis=m.get("visib"), raw=m.get("rawOb") or "",
                            src="obs " + datetime.fromisoformat(m["reportTime"].replace("Z", "+00:00"))
                            .astimezone(ZONE).strftime("%H:%M")))
        elif name in FIELD_STATIONS and (st := swob_latest(FIELD_STATIONS[name], lat, lon)):
            spd = st.get("avg_wnd_spd_10m_pst2mts") or st.get("avg_wnd_spd_10m_pst10mts")
            wdir = st.get("avg_wnd_dir_10m_pst2mts") or st.get("avg_wnd_dir_10m_pst10mts")
            peak = st.get("max_wnd_spd_10m_pst10mts")
            spd_kt = round((spd or 0) * KMH_KT)
            gst = round(peak * KMH_KT) if peak and peak * KMH_KT >= spd_kt + 10 else None   # METAR rule: peak >= mean + 10 kt
            if name in prev:
                layers, mvis, mwx = [tuple(l) for l in prev[name]["layers"]], prev[name]["mvis"], prev[name]["wx"]
            else:
                layers, mvis, mwx = model_sky(lat, lon, st.get("stn_elev") or 0)
            svis = st.get("avg_vis_pst10mts") or st.get("vis")
            vis = round(svis / 1.609) if svis else mvis
            ceil = min((b for c, b in layers if c in ("BKN", "OVC")), default=None)
            out.append(dict(id=name, dir=None if spd_kt == 0 else (int(wdir) if wdir is not None else None), spd=spd_kt,
                            gst=gst, ceil=ceil, cat=flight_category(ceil, vis), layers=layers, wx=mwx, vis=vis,
                            src="stn+GFS", modelled=True, hour=hour, mvis=mvis))
        else:
            q = dict(latitude=lat, longitude=lon, current="wind_speed_10m,wind_direction_10m,wind_gusts_10m",
                     wind_speed_unit="kn", timezone=ZONE_NAME)
            c = json.load(urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q), timeout=60))["current"]
            gst = round(c["wind_gusts_10m"])
            out.append(dict(id=name, dir=round(c["wind_direction_10m"]), spd=round(c["wind_speed_10m"]),
                            gst=gst if gst >= round(c["wind_speed_10m"]) + 5 else None, ceil=None, cat=None,
                            layers=None, wx="", vis=None, src="model"))
    json.dump(out, open(path, "w"))
    return out


def grid_fields(data, precip_i, cloud_is):
    pr = np.array([x["hourly"]["precipitation"][precip_i] for x in data], float)
    cc = np.array([[x["hourly"]["cloud_cover"][i] for i in cloud_is] for x in data], float).mean(1)
    return np.nan_to_num(pr).reshape(GRID_NY, GRID_NX), np.nan_to_num(cc).reshape(GRID_NY, GRID_NX)


def upsample(field):
    """Grid spanning the map box -> map pixels, cubic."""
    yy, xx = np.mgrid[0:MAP_H, 0:MAP_W].astype(float)
    return map_coordinates(field, [yy / (MAP_H - 1) * (GRID_NY - 1), xx / (MAP_W - 1) * (GRID_NX - 1)],
                           order=3, mode="nearest")


# ------------------------------------------------------------------ map

def land_mask():
    """Land polygons from coast.geojson, already clipped to just outside the map box."""
    img = Image.new("L", (MAP_W, MAP_H), 0)
    d = ImageDraw.Draw(img)
    rings = []
    for ft in json.load(open(COAST))["features"]:
        g = ft["geometry"]
        for poly in g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]:
            for k, ring in enumerate(poly):
                ring = np.asarray(ring)
                px, py = ll_to_px(ring[:, 1], ring[:, 0])
                pts = list(zip(px.tolist(), py.tolist()))
                d.polygon(pts, fill=255 if k == 0 else 0)
                rings.append(pts)
    return np.asarray(img) > 127, rings


def rain_fractions(mm):
    logs = np.log([s for s, _ in RAIN_STOPS])
    fr = np.array([f for _, f in RAIN_STOPS], float)
    lm = np.log(np.clip(mm, RAIN_STOPS[0][0], RAIN_STOPS[-1][0]))
    return np.stack([np.interp(lm, logs, fr[:, i]) for i in range(4)], -1)


def map_fractions(pr, cc, land):
    f = np.zeros((MAP_H, MAP_W, 4))
    cloud = np.clip(upsample(cc) / 100, 0, 1)
    k = CLOUD_K * cloud                                           # grey dots mean cloud, on land or sea
    f[..., K], f[..., W] = k, 1 - k
    rain = np.clip(upsample(pr), 0, None)
    a = np.clip((rain - 0.05) / 0.1, 0, 1)[..., None]            # fade in around 0.1 mm/h
    return f * (1 - a) + rain_fractions(rain) * a, a[..., 0]


def sticker_text(img, xy, s, size, anchor, bold=True, grow=3):
    """Label that looks cut out like a sticker: the text's own shape, dilated, in white with a black rim."""
    from scipy.ndimage import binary_dilation
    fnt = font(size, bold)
    probe = ImageDraw.Draw(img)
    l, t, r, b = (int(v) for v in probe.textbbox(xy, s, font=fnt, anchor=anchor))
    pad = grow + 3
    box = (l - pad, t - pad, r + pad, b + pad)
    m = Image.new("L", (box[2] - box[0], box[3] - box[1]), 0)
    md = ImageDraw.Draw(m)
    md.fontmode = "1"
    md.text((xy[0] - box[0], xy[1] - box[1]), s, font=fnt, fill=255, anchor=anchor)
    glyph = np.asarray(m) > 127
    yy, xx = np.mgrid[-grow:grow + 1, -grow:grow + 1]
    body = binary_dilation(glyph, structure=(xx ** 2 + yy ** 2) <= grow ** 2)
    rim = binary_dilation(body, iterations=1) & ~body
    tile = np.asarray(img.crop(box)).copy()
    tile[rim] = PK
    tile[body] = PW
    tile[glyph] = PK
    img.paste(Image.fromarray(tile), box[:2])


def boxed_text(d, xy, s, size, anchor, fg=PK, bg=PW, pad=3, bold=True):
    l, t, r, b = d.textbbox(xy, s, font=font(size, bold), anchor=anchor)
    d.rectangle((l - pad, t - pad + 1, r + pad, b + pad - 1), fill=bg)
    d.text(xy, s, font=font(size, bold), fill=fg, anchor=anchor)


def north_arrow(d, cx, cy, length=34):
    # direction of north in pixel space: rotate (0, 1) km, flip y for pixels
    nx, ny = -SIN, -COS
    tip = (cx + nx * length / 2, cy + ny * length / 2)
    tail = (cx - nx * length / 2, cy - ny * length / 2)
    px, py = -ny, nx                                   # perpendicular, for the arrowhead
    d.ellipse((cx - 26, cy - 26, cx + 26, cy + 26), fill=PW, outline=PK, width=2)
    d.line((tail, tip), fill=PK, width=3)
    d.polygon([tip, (tip[0] - nx * 12 + px * 7, tip[1] - ny * 12 + py * 7),
               (tip[0] - nx * 12 - px * 7, tip[1] - ny * 12 - py * 7)], fill=PK)
    d.text((cx - nx * 13 + 2, cy - ny * 13 + 2), "N", font=font(14), fill=PK, anchor="mm")


def draw_map(img, pr, cc, title):
    land, rings = land_mask()
    f, rain_a = map_fractions(pr, cc, land)
    idx = ht.render_stacked(f, ht.bayer_screen(MAP_H, MAP_W, 8))
    # sea texture: sparse horizontal dotted lines (engraved-chart style) on white sea pixels,
    # kept off rain so the rain colours stay clean. It reads as water, not as grey.
    yy, xx = np.mgrid[0:MAP_H, 0:MAP_W]
    water = ~land & (rain_a == 0) & (idx == W) & (yy % WATER_ROW == 3) & (xx % WATER_DOT == 0)
    idx[water] = K
    m = Image.fromarray(ht.to_rgb(idx))
    d = ImageDraw.Draw(m)
    d.fontmode = "1"
    for width, colour in ((6, PW), (3, PK)):      # white halo, then the black coast
        for pts in rings:
            d.line(pts + [pts[0]], fill=colour, width=width, joint="curve")
    for name, lat, lon, side, dot in TOWNS:
        x, y = (float(v) for v in ll_to_px(lat, lon))
        if dot:
            anchor, tx = ("lm", x + 11) if side == "r" else ("rm", x - 11)
            sticker_text(m, (tx, y), name, 18, anchor)
            d.ellipse((x - 5, y - 5, x + 5, y + 5), fill=PK, outline=PW, width=2)
        else:
            sticker_text(m, (x, y), name, 15, "mm" if side == "c" else ("lm" if side == "r" else "rm"), bold=False)
    if ROT_DEG:
        north_arrow(d, 34, 34)
    boxed_text(d, (MAP_W - 10, 10), title, 18, "ra", fg=PY, bg=PK, pad=5)
    img.paste(m, (0, 0))


def draw_legend(img, d, x1, y):
    """Rain + cloud key, right-aligned to x1, swatches starting at y."""
    cw = 34
    x = x1 - (cw * len(LEGEND) + 16 + 2 * 38)
    d.text((x - 10, y + 9), "rain mm/h", font=font(15, False), fill=PK, anchor="rm")
    sw = rain_fractions(np.array(LEGEND, float))
    for i, mm in enumerate(LEGEND):
        cidx = ht.render_stacked(np.broadcast_to(sw[i], (18, cw - 4, 4)), ht.bayer_screen(18, cw - 4, 8))
        img.paste(Image.fromarray(ht.to_rgb(cidx)), (x + i * cw, y))
        d.rectangle((x + i * cw - 1, y - 1, x + i * cw + cw - 4, y + 18), outline=PK)
        d.text((x + i * cw + (cw - 4) // 2, y + 21), f"{mm:g}", font=font(12, False), fill=PK, anchor="mt")
    cx = x + len(LEGEND) * cw + 16
    d.text((cx + 37, y - 4), "cloud", font=font(12, False), fill=PK, anchor="mb")
    for j, cover in enumerate((0.5, 1.0)):
        k = CLOUD_K * cover
        cl = np.broadcast_to(np.array([k, 1 - k, 0, 0]), (18, 34, 4))
        img.paste(Image.fromarray(ht.to_rgb(ht.render_stacked(cl, ht.bayer_screen(18, 34, 8)))), (cx + j * 38, y))
        d.rectangle((cx + j * 38 - 1, y - 1, cx + j * 38 + 34, y + 18), outline=PK)
        d.text((cx + j * 38 + 17, y + 21), f"{cover:.0%}", font=font(12, False), fill=PK, anchor="mt")


# ------------------------------------------------------------------ right side

def weather_icon(p, code, cx, cy, size):
    if code in (0, 1):
        p.sun(cx, cy, size // 3)
    elif code == 2:
        p.partly(cx - size // 2, cy - size // 2, size, size)
    elif code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99):
        p.rain(cx - size // 2, cy - size // 2, size, size)
    else:
        p.cloud(cx - size // 2, cy - size // 3, size, size * 2 // 3)


CONDITIONS = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Fog",
              51: "Drizzle", 53: "Drizzle", 55: "Drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
              71: "Snow", 73: "Snow", 75: "Heavy snow", 80: "Showers", 81: "Showers", 82: "Downpours", 95: "Thunder"}


EVENT_ROWS, EVENT_ROW_H = 7, 28   # agenda rows above the airports (8 x 29 before the peek strip)
WIND_RED_KT = 10        # above this (or any gust) the wind figure goes red
XWIND_KT = 4            # crosswind on the best runway above this gets a yellow tag


def crosswind(w):
    """Crosswind (kt) on the runway that minimises it; variable wind counts as all crosswind."""
    rws = RUNWAYS.get(w["id"], [])
    if not w["spd"] or not rws:
        return 0
    if w["dir"] is None:
        return w["spd"]
    return min(abs(w["spd"] * np.sin(np.radians(w["dir"] - hdg))) for hdg, _, _ in rws)


GREY = np.array([.45, .55, 0, 0])       # ink fractions K W Y R: the moon's shadow grey, used for all rain


def day_kind(vic, date):
    """Icon for a day from its daytime hours (09-17), not Open-Meteo's worst-of-24-h daily code."""
    h = vic["hourly"]
    idx = [i for i, t in enumerate(h["time"]) if t.startswith(date) and 9 <= int(t[11:13]) <= 17]
    cloud = np.mean([h["cloud_cover"][i] or 0 for i in idx])
    rain = sum(h["precipitation"][i] or 0 for i in idx)
    pop = max(h["precipitation_probability"][i] or 0 for i in idx)
    if rain >= 1.0 or (pop >= 60 and rain >= 0.3):
        return "rain"
    return "sun" if cloud < 25 else "partly" if cloud < 65 else "cloud"


def forecast_icon(img, kind, pop, cx, cy, size):
    p = Painter(img)
    d = p.d
    if kind == "sun" and pop < 30:
        p.sun(cx, cy, size // 3)
        return
    w, h = size, int(size * .66)
    x, y = cx - w // 2, cy - h // 2 - (6 if kind == "rain" else 0)
    if kind == "partly":
        p.sun(int(x + w * .72), int(y + h * .1), int(w * .15))
        x, y, w, h = x - 2, y + 6, int(w * .9), int(h * .9)
    parts = [(x, y + h * .45, x + w * .45, y + h), (x + w * .2, y + h * .1, x + w * .7, y + h * .8),
             (x + w * .5, y + h * .35, x + w, y + h), (x + w * .2, y + h * .6, x + w * .85, y + h)]
    t = max(2, w // 22)
    for e in parts:
        d.ellipse(e, fill=PK)
    # inner area: white above the rain-chance level, the moon's dithered grey below it
    x0, y0, x1, y1 = int(x) - 1, int(y) - 1, int(x + w) + 2, int(y + h) + 2
    m = Image.new("L", (x1 - x0, y1 - y0), 0)
    md = ImageDraw.Draw(m)
    for a, b, c, e in parts:
        md.ellipse((a + t - x0, b + t - y0, c - t - x0, e - t - y0), fill=255)
    inner = np.asarray(m) > 127
    rows = np.where(inner.any(1))[0]
    level = rows.min() + (1 - pop / 100) * (rows.max() - rows.min() + 1)
    yy = np.mgrid[0:inner.shape[0], 0:inner.shape[1]][0]
    f = np.zeros(inner.shape + (4,))
    f[..., ht.WHITE] = 1
    f[inner & (yy >= level)] = GREY
    idx = ht.render_stacked(f, ht.bayer_screen(*inner.shape, 8))
    tile = np.asarray(img.crop((x0, y0, x1, y1))).copy()
    tile[inner] = ht.to_rgb(idx)[inner]
    img.paste(Image.fromarray(tile), (x0, y0))
    if kind == "rain":                                  # three drops under the cloud
        for k, dx in enumerate((-.28, 0, .28)):
            grey_drop(img, d, cx + dx * size, y + h + 9 + (4 if k == 1 else 0), r=4)


def draw_forecast(p, d, img, x0, x1, y, days):
    """Five days from tomorrow: day name, icon (cloud filled grey by rain chance), high / low, rain total."""
    cw = (x1 - x0) / len(days)
    for i, day in enumerate(days):
        cx = int(x0 + cw * i + cw / 2)
        if i:
            d.line((x0 + cw * i, y + 4, x0 + cw * i, y + 134), fill=PK, width=1)
        d.text((cx, y + 12), day["name"], font=serif(22), fill=PK, anchor="mm")
        forecast_icon(img, day["kind"], day["pop"], cx, y + 48, 46)
        d.text((cx, y + 85), f"{day['hi']}°", font=font(23), fill=PK, anchor="mm")
        d.text((cx, y + 106), f"{day['lo']}°", font=font(17, False), fill=PK, anchor="mm")
        if day["mm"] >= 0.5:                            # rain total, only on wet days
            txt = f"{day['mm']:.0f} mm" if day["mm"] >= 1 else "<1 mm"
            tw = d.textlength(txt, font=font(14))
            grey_drop(img, d, cx - tw / 2 - 6, y + 126)
            d.text((cx + 5, y + 125), txt, font=font(14), fill=PK, anchor="mm")


def sky_lines(w, budget=3):
    """What fits in `budget` lines: cloud layers up to and including the ceiling (anything above
    it doesn't matter), highest first, then visibility and weather on the bottom line. That line
    always gets a row when there's weather or visibility under 10 SM, and otherwise fills a spare
    row. With more layers than rows, the ceiling and the lowest layer stay."""
    vis, wx = w.get("vis"), w.get("wx") or ""
    try:
        low_vis = vis is not None and float(str(vis).rstrip("+")) < 10
    except ValueError:
        low_vis = False
    layers = sorted([(c, b) for c, b in (w.get("layers") or []) if c], key=lambda cb: cb[1] if cb[1] is not None else 0)
    ceiling = next(((c, b) for c, b in layers if c in ("BKN", "OVC", "OVX", "VV") and b is not None), None)
    if ceiling:
        layers = [cb for cb in layers if cb[1] is not None and cb[1] <= ceiling[1]]
    must = bool(wx) or low_vis
    room = budget - (1 if must else 0)
    keep = []
    for cand in ([ceiling] if ceiling else []) + layers[:1] + layers:
        if cand and cand not in keep and len(keep) < room:
            keep.append(cand)
    keep.sort(key=lambda cb: -(cb[1] or 0))
    keep = keep or [("CLR", None)]
    show_vis = must or (vis is not None and len(keep) < budget)
    bottom = " ".join(t for t in ((f"{vis}SM" if show_vis and vis is not None else ""), wx) if t)
    return keep, bottom, ceiling


def draw_layers(d, cx, y, w, step=17, width=82):
    """METAR-style sky in three lines. Each layer reads like the METAR (BKN190) with the trailing
    00 in a smaller size, top-aligned with the digits. The ceiling layer is bold."""
    if w["src"] == "model":
        d.text((cx, y + 8), "no METAR", font=condensed(16), fill=PK, anchor="mm")
        return
    layers, bottom, ceiling = sky_lines(w)
    k = 0
    for cover, base in layers:
        bold = (cover, base) == ceiling
        big, small = condensed(16, bold), condensed(11, bold)
        main = cover if base is None else f"{cover}{base // 100:03d}"
        tail = "" if base is None else "00"
        total = d.textlength(main, font=big) + d.textlength(tail, font=small)
        x, base_y = cx - total / 2, y + k * step + 13
        d.text((x, base_y), main, font=big, fill=PK, anchor="ls")
        if tail:                                       # align the small 00's top with the digits' top
            top_big = big.getbbox("0", anchor="ls")[1]
            top_small = small.getbbox("0", anchor="ls")[1]
            d.text((x + d.textlength(main, font=big), base_y + top_big - top_small), tail, font=small, fill=PK, anchor="ls")
        k += 1
    if bottom:
        obscured = re.search(r"FG|BR|HZ|FU|DU|SA", bottom)
        size = 16
        while size > 11 and d.textlength(bottom, font=condensed(size, True)) > width:   # shrink to fit the column
            size -= 1
        d.text((cx, y + k * step + 13), bottom, font=condensed(size, True), fill=PR if obscured else PK, anchor="ms")


def draw_wind(d, cx, cy, r, w, hy=None, img=None):
    """One airport: dial with a downwind arrow, speed and gust in knots."""
    strong = (w["spd"] or 0) > WIND_RED_KT or bool(w["gst"])
    colour = PK
    # heading coloured by flight category: yellow = marginal, red = IFR / LIFR
    cat = w.get("cat")
    bg, fg = {"MVFR": (PY, PK), "IFR": (PR, PW), "LIFR": (PR, PW)}.get(cat, (None, PK))
    hy = cy - r - 40 if hy is None else hy
    if bg and w.get("modelled") and img is not None:                     # modelled: the category colour dithered at 50 %, black text
        l, t, rr, b = (int(v) for v in d.textbbox((cx, hy), w["id"], font=serif(22), anchor="mm"))
        box = (l - 6, t - 4, rr + 6, b + 4)
        ink = ht.YELLOW if bg == PY else ht.RED
        f = np.zeros((box[3] - box[1] + 1, box[2] - box[0] + 1, 4))
        f[..., ink], f[..., ht.WHITE] = .5, .5
        tile = ht.to_rgb(ht.render_stacked(f, ht.bayer_screen(*f.shape[:2], 4)))
        m = Image.new("L", (f.shape[1], f.shape[0]), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, f.shape[1] - 1, f.shape[0] - 1), 5, fill=255)
        img.paste(Image.fromarray(tile), box[:2], m)
        d.rounded_rectangle(box, 5, outline=PK)
        bg, fg = None, PK
    if bg:
        l, t, rr, b = d.textbbox((cx, hy), w["id"], font=serif(22), anchor="mm")
        d.rounded_rectangle((l - 6, t - 4, rr + 6, b + 4), 5, fill=bg, outline=PK)
    d.text((cx, hy), w["id"], font=serif(22), fill=fg, anchor="mm")
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=PW, outline=PK, width=2)
    for hdg, main, off in RUNWAYS.get(w["id"], []):              # runways, true heading, behind the arrow
        s_, c_ = np.sin(np.radians(hdg)), -np.cos(np.radians(hdg))
        ox, oy = -c_ * off, s_ * off
        L = r - 4
        d.line((cx + ox - s_ * L, cy + oy - c_ * L, cx + ox + s_ * L, cy + oy + c_ * L), fill=PR, width=7 if main else 3)
    for b in (0, 90, 180, 270):                                   # N E S W ticks
        s, c = np.sin(np.radians(b)), -np.cos(np.radians(b))
        d.line((cx + s * (r - 6), cy + c * (r - 6), cx + s * r, cy + c * r), fill=PK, width=2)
    d.text((cx, cy - r + 9), "N", font=font(10), fill=PK, anchor="mm")
    if not w["spd"]:
        pass                                            # calm: said in the speed line instead
    elif w["dir"] is None:
        boxed_text(d, (cx, cy + 1), "VRB", 14, "mm", pad=2)
    else:
        b = np.radians(w["dir"] + 180)                             # arrow points downwind
        ux, uy = np.sin(b), -np.cos(b)
        tip = (cx + ux * (r - 9), cy + uy * (r - 9))
        tail = (cx - ux * (r - 9), cy - uy * (r - 9))
        px, py = -uy, ux
        d.line((tail, (tip[0] - ux * 8, tip[1] - uy * 8)), fill=colour, width=4)
        d.polygon([tip, (tip[0] - ux * 14 + px * 8, tip[1] - uy * 14 + py * 8),
                   (tip[0] - ux * 14 - px * 8, tip[1] - uy * 14 - py * 8)], fill=colour)
    xw = crosswind(w)
    if round(xw) > XWIND_KT:                            # yellow crosswind tag on the dial rim
        tag = f"X{round(xw)}"
        l, t, rr, b = d.textbbox((cx, cy + r), tag, font=font(12), anchor="mm")
        d.rounded_rectangle((l - 4, t - 3, rr + 4, b + 3), 4, fill=PY, outline=PK)
        d.text((cx, cy + r), tag, font=font(12), fill=PK, anchor="mm")
    spd = "calm" if not w["spd"] else f"{w['spd']}" + (f"G{w['gst']}" if w["gst"] else "") + " kt"
    d.text((cx, cy + r + 17), spd, font=font(17), fill=PR if strong else PK, anchor="mm")
    d.text((cx, cy + r + 33), w["src"], font=font(11, False), fill=PK, anchor="mm")


def grey_drop(img, d, x, cy, r=3):
    """Raindrop filled with the moon's dithered grey (as are the forecast clouds' rain fill), black rim."""
    m = Image.new("L", (2 * r + 3, 2 * r + 7), 0)
    md = ImageDraw.Draw(m)
    ox, oy = r + 1, r + 5
    md.polygon([(ox, oy - r - 4), (ox - r, oy - 1), (ox + r, oy - 1)], fill=255)
    md.ellipse((ox - r, oy - r, ox + r, oy + r), fill=255)
    inner = np.asarray(m) > 127
    f = np.zeros(inner.shape + (4,))
    f[..., ht.WHITE] = 1
    f[inner] = GREY
    tile = ht.to_rgb(ht.render_stacked(f, ht.bayer_screen(*inner.shape, 8)))
    x0, y0 = int(x - ox), int(cy - oy)
    img.paste(Image.fromarray(tile), (x0, y0), Image.fromarray((inner * 255).astype(np.uint8)))
    d.polygon([(x0 + ox, y0 + oy - r - 4), (x0 + ox - r, y0 + oy - 1), (x0 + ox + r, y0 + oy - 1)], outline=PK)
    d.arc((x0 + ox - r, y0 + oy - r, x0 + ox + r, y0 + oy + r), -30, 210, fill=PK)


def draw_sparkline(img, d, x0, x1, y0, y1, vic, now):
    """Next 24 h: temperature as a line, rain as 1-3 grey drops above it."""
    h = vic["hourly"]
    _, i = now_conditions(vic, now)
    times = h["time"][i:i + 25]
    temps = np.array([v if v is not None else np.nan for v in h["temperature_2m"][i:i + 25]], float)
    rain = np.array([v or 0 for v in h["precipitation"][i + 1:i + 26]], float)   # (T-1, T] -> the hour starting at T-1
    lo, hi = np.nanmin(temps), np.nanmax(temps)
    px0, px1, py0, py1 = x0 + 34, x1 - 2, y0 + 3, y1 - 12
    py0 += 14 if np.nanmax(np.array(h["precipitation"][i + 1:i + 26], float)) >= RAIN_MM else 0   # headroom for drops
    n = len(times)
    xs = px0 + np.arange(n) * (px1 - px0) / (n - 1)
    bw = max(2, int((px1 - px0) / (n - 1)) - 2)
    span = max(hi - lo, 1)
    ys = py1 - (temps - lo) / span * (py1 - py0)
    d.line(list(zip(xs.tolist(), ys.tolist())), fill=PK, width=2, joint="curve")
    for k, mm in enumerate(rain[:n - 1]):                  # rain: 1-3 drops stacked above the line at that hour
        if mm < RAIN_MM:
            continue
        drops = 1 if mm < 1 else 2 if mm < 4 else 3
        x, top = (xs[k] + xs[k + 1]) / 2, min(ys[k], ys[k + 1]) - 4
        for j in range(drops):
            grey_drop(img, d, x, max(top - j * 7, y0 + 4))
    d.line((px0, py1, px1, py1), fill=PK, width=1)
    d.text((x0 + 28, py0 + 2), f"{round(hi)}°", font=font(12), fill=PK, anchor="rm")
    d.text((x0 + 28, py1 - 2), f"{round(lo)}°", font=font(12, False), fill=PK, anchor="rm")
    for k, t in enumerate(times):                          # hour ticks every 6 h
        if int(t[11:13]) % 6 == 0:
            d.line((xs[k], py1, xs[k], py1 + 3), fill=PK, width=1)
            d.text((xs[k], y1 - 4), t[11:13], font=font(10, False), fill=PK, anchor="mm")


SYNODIC = 29.530588853
NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
MOON_NAMES = ["New moon", "Waxing crescent", "First quarter", "Waxing gibbous",
              "Full moon", "Waning gibbous", "Last quarter", "Waning crescent"]


def moon_phase(t):
    """0 = new, 0.5 = full. Mean synodic month from a known new moon: within about a day."""
    days = (t.astimezone(timezone.utc) - NEW_MOON).total_seconds() / 86400
    return (days / SYNODIC) % 1


def draw_moon(img, cx, cy, r, phase):
    """Lit side yellow, shadow side dithered grey, black rim; northern-hemisphere view."""
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1] / r
    inside = xx ** 2 + yy ** 2 <= 1
    c = np.cos(2 * np.pi * phase)
    half = np.sqrt(np.clip(1 - yy ** 2, 0, 1))
    lit = (xx > c * half) if phase < 0.5 else (xx < -c * half)
    f = np.zeros(xx.shape + (4,))
    f[..., W] = 1
    f[inside & lit] = (0, 0, 1, 0)
    f[inside & ~lit] = GREY
    idx = ht.render_stacked(f, ht.bayer_screen(*xx.shape, 8))
    tile = Image.fromarray(ht.to_rgb(idx))
    mask = Image.fromarray((inside * 255).astype(np.uint8))
    img.paste(tile, (cx - r, cy - r), mask)
    ImageDraw.Draw(img).ellipse((cx - r, cy - r, cx + r, cy + r), outline=PK, width=2)


def draw_sun_moon(img, d, x, cy, daily, now):
    rise, sset = daily["sunrise"][0][-5:], daily["sunset"][0][-5:]
    mins = (datetime.fromisoformat(daily["sunset"][0]) - datetime.fromisoformat(daily["sunrise"][0])).seconds // 60
    Painter(img).sun(x + 12, cy, 8)
    d.text((x + 34, cy - 9), f"{rise} – {sset}", font=font(17), fill=PK, anchor="lm")
    d.text((x + 34, cy + 11), f"{mins // 60} h {mins % 60:02d} m daylight", font=font(14, False), fill=PK, anchor="lm")
    phase = moon_phase(now.replace(tzinfo=ZONE))
    lit = (1 - np.cos(2 * np.pi * phase)) / 2
    mx = x + 222
    draw_moon(img, mx, cy, 16, phase)
    d.text((mx + 26, cy - 9), MOON_NAMES[int((phase * 8) + 0.5) % 8], font=font(17), fill=PK, anchor="lm")
    d.text((mx + 26, cy + 11), f"{lit:.0%} lit", font=font(14, False), fill=PK, anchor="lm")


def ellipsize(d, text, fnt, width):
    if d.textlength(text, font=fnt) <= width:
        return text
    while text and d.textlength(text + "…", font=fnt) > width:
        text = text[:-1]
    return text.rstrip(" -:") + "…"


def build(pr, cc, map_title, vic, wind, now, aqi, evs, events_now, map_drawer=None):
    """Every panel's home card, drawn side by side on one WIDTH x BODY_H canvas (the peek strips and
    the dividers are added by renderer.cards when the frame is composed)."""
    img = Image.new("RGB", (WIDTH, BODY_H), PW)
    if map_drawer:                              # GFA-style map (renderer/gfa.py)
        map_drawer(img)
    else:                                       # radar-style rain overlay
        draw_map(img, pr, cc, map_title)
    p = Painter(img)
    d = p.d
    (_, lx1), (mx0, mx1), (rx0, rx1) = THIRDS
    mx0, mx1 = mx0 + 16, mx1 - 14                  # middle: date, conditions, next 24 h, five days
    rx0, rx1 = rx0 + 12, rx1 - 14                  # right: upcoming events, airports
    banners, rows = cal.classify(evs, events_now)

    d.text((mx0, 2), now.strftime("%A"), font=serif(38), fill=PR)
    d.text((mx0, 48), now.strftime("%-d %B %Y"), font=font(20), fill=PK)
    if banners and now.date() == events_now.date():
        tag = ellipsize(d, banners[0], font(13), mx1 - mx0 - 14)
        l, t, r, b = d.textbbox((mx0 + 8, 85), tag, font=font(13), anchor="lm")
        d.rounded_rectangle((mx0, t - 5, r + 8, b + 5), 7, fill=PY, outline=PK)
        d.text((mx0 + 8, 85), tag, font=font(13), fill=PK, anchor="lm")
    cur, _ = now_conditions(vic, now)
    daily = vic["daily"]
    weather_icon(p, cur["weather_code"], mx0 + 32, 130, 52)
    d.text((mx0 + 68, 130), f"{round(cur['temperature_2m'])}°", font=font(58), fill=PK, anchor="lm")
    tx = mx0 + 206
    d.text((tx, 104), CONDITIONS.get(cur["weather_code"], "—"), font=serif(22), fill=PK)
    d.text((tx, 134), f"H {round(daily['temperature_2m_max'][0])}°   L {round(daily['temperature_2m_min'][0])}°", font=font(19, False), fill=PK)
    notes = callouts(vic, now, aqi)
    if notes:                                   # only when something's worth saying
        d.text((mx0, 172), ellipsize(d, "  ".join(t for t, _ in notes), font(16), mx1 - mx0), font=font(16), fill=PR, anchor="lm")
    draw_sparkline(img, d, mx0, mx1, 184, 232, vic, now)
    d.line((mx0, 238, mx1, 238), fill=PK, width=2)
    days = []
    for i in range(1, 6):
        dt = datetime.fromisoformat(daily["time"][i])
        days.append(dict(name=dt.strftime("%a"), kind=day_kind(vic, daily["time"][i]),
                         hi=round(daily["temperature_2m_max"][i]), lo=round(daily["temperature_2m_min"][i]),
                         pop=daily["precipitation_probability_max"][i] or 0, mm=daily["precipitation_sum"][i] or 0))
    draw_forecast(p, d, img, mx0, mx1, 242, days)
    d.line((mx0, 380, mx1, 380), fill=PK, width=2)
    draw_sun_moon(img, d, mx0, 409, daily, now)

    # right: the classifier's picks (highlights in red), then the airports
    d.text((rx0, 2), "Upcoming", font=serif(30), fill=PK)
    today = events_now.date()
    for i, (e, score, why, hi) in enumerate(rows[:EVENT_ROWS]):
        y = 45 + i * EVENT_ROW_H
        colour = PR if hi else PK
        if hi:
            d.rectangle((rx0, y, rx0 + 5, y + 21), fill=PR)
        ed = datetime.fromisoformat(e["date"]).date()
        dayname = "Today" if ed == today else ("Tmrw" if (ed - today).days == 1 else ed.strftime("%a %-d"))
        d.text((rx0 + 13, y + 11), dayname, font=serif(18), fill=colour, anchor="lm")
        d.text((rx0 + 88, y + 11), e["start"] or "all day", font=condensed(18, True), fill=colour, anchor="lm")
        title = cal.display_title(e["title"])
        tf = condensed(19, True)
        d.text((rx0 + 146, y + 11), ellipsize(d, title, tf, rx1 - rx0 - 146), font=tf, fill=colour, anchor="lm")
    rule = 45 + EVENT_ROWS * EVENT_ROW_H + 2
    d.line((rx0, rule, rx1, rule), fill=PK, width=2)
    ww = (rx1 - rx0) / len(wind)
    for i, w in enumerate(wind):
        cx = int(rx0 + ww * i + ww / 2)
        draw_layers(d, cx, rule + 30, w, step=16, width=int(ww) - 4)
        draw_wind(d, cx, rule + 112, 23, w, hy=rule + 18, img=img)
    # No "updated HH:MM" tag: freshness shows in the device's stale strip (docs/SLOWBOARD.md §13 Q1)
    return img


def bodies(canvas):
    """The canvas cut into the panels' card bodies."""
    return {p: canvas.crop((x0, 0, x1 + 1, BODY_H)) for p, (x0, x1) in PANELS.items()}


# ------------------------------------------------------------------ detail cards

def hours_card(vic, now, w):
    """Weather deck: the next 24 h in 3-hour steps."""
    img = Image.new("RGB", (w, BODY_H), PW)
    p = Painter(img)
    d = p.d
    x0, x1 = 16, w - 14
    d.text((x0, 2), "Next 24 hours", font=serif(32), fill=PK)
    h = vic["hourly"]
    _, i = now_conditions(vic, now)
    top, rh = 52, 49
    for k in range(8):
        j = i + 1 + k * 3                         # the 3 h starting at the next full hour
        if j + 3 >= len(h["time"]):
            break
        y = top + k * rh
        if k:
            d.line((x0, y - 3, x1, y - 3), fill=PK, width=1)
        t = datetime.fromisoformat(h["time"][j])
        d.text((x0, y + 20), t.strftime("%H") + "h", font=serif(22), fill=PR if t.hour == 0 else PK, anchor="lm")
        if t.hour == 0:
            d.text((x0 + 52, y + 20), t.strftime("%a"), font=font(13, False), fill=PR, anchor="lm")
        code = max(h["weather_code"][j:j + 3], key=lambda c: c or 0)
        weather_icon(p, code, x0 + 112, y + 20, 34)
        temps = [v for v in h["temperature_2m"][j:j + 3] if v is not None]
        d.text((x0 + 150, y + 20), f"{round(temps[0])}°" if temps else "–", font=font(24), fill=PK, anchor="lm")
        mm = sum(v or 0 for v in h["precipitation"][j + 1:j + 4])
        pop = max(v or 0 for v in h["precipitation_probability"][j:j + 3])
        if mm >= RAIN_MM or pop >= 30:
            grey_drop(img, d, x0 + 228, y + 21, r=4)
            d.text((x0 + 240, y + 20), (f"{mm:.1f} mm" if mm >= RAIN_MM else "dry") + f" · {pop}%", font=font(16, False), fill=PK, anchor="lm")
        wind = max(v or 0 for v in h["wind_speed_10m"][j:j + 3])
        d.text((x1, y + 20), f"{round(wind)} km/h", font=font(16, wind >= 30), fill=PR if wind >= 40 else PK, anchor="rm")
    return img


def fetch_tafs(path, ids=("CYYJ",)):
    if os.path.exists(path) and "--refresh" not in sys.argv:
        return json.load(open(path))
    try:
        out = {t["icaoId"]: t.get("rawTAF", "") for t in json.load(urllib.request.urlopen(
            f"https://aviationweather.gov/api/data/taf?ids={','.join(ids)}&format=json", timeout=60))}
    except Exception:                               # noqa: BLE001 - a missing TAF just isn't shown
        out = {}
    json.dump(out, open(path, "w"))
    return out


def metar_card(wind, tafs, w):
    """Agenda deck: raw METARs and the Victoria TAF, broken at its change groups."""
    img = Image.new("RGB", (w, BODY_H), PW)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    x0, x1 = 12, w - 14
    d.text((x0, 2), "METAR · TAF", font=serif(30), fill=PK)
    y = 48
    mono = condensed(16)
    for st in wind:
        if not st.get("raw"):
            continue
        d.text((x0, y), st["id"], font=serif(17), fill=PR if st.get("cat") in ("IFR", "LIFR") else PK)
        lines = wrap_words(d, st["raw"], mono, x1 - x0 - 62)
        for line in lines[:3]:
            d.text((x0 + 62, y + 2), line, font=mono, fill=PK)
            y += 20
        y += 6
    taf = tafs.get("CYYJ")
    if taf:
        d.line((x0, y, x1, y), fill=PK, width=2)
        y += 8
        d.text((x0, y), "TAF YYJ", font=serif(17), fill=PK)
        y += 24
        parts = re.split(r"\s(?=FM\d{6}|TEMPO|BECMG|PROB\d{2}|RMK)", re.sub(r"^TAF\s+(AMD\s+|COR\s+)?", "", taf.strip()))
        for part in parts:
            indent = 0 if part is parts[0] else 16
            for line in wrap_words(d, part, mono, x1 - x0 - indent):
                if y > BODY_H - 22:
                    break
                d.text((x0 + indent, y), line, font=mono, fill=PK)
                y += 19
    return img


def wrap_words(d, text, fnt, width):
    out, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if d.textlength(trial, font=fnt) <= width or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = word
    return out + ([cur] if cur else [])


def save_cards(cards, manifest):
    """cards: {panel: {card id: Image}}; manifest: {panel: [{id, title}, ...]} (home first)."""
    root = os.path.join(OUT, "cards")
    for panel, by_id in cards.items():
        os.makedirs(os.path.join(root, panel), exist_ok=True)
        for cid, im in by_id.items():                # atomic: a key press may be reading them
            im.save(os.path.join(root, panel, cid + ".tmp.png"))
            os.replace(os.path.join(root, panel, cid + ".tmp.png"), os.path.join(root, panel, cid + ".png"))
    with open(os.path.join(root, "manifest.tmp"), "w") as f:
        json.dump(manifest, f)
    os.replace(os.path.join(root, "manifest.tmp"), os.path.join(root, "manifest.json"))


FEED_VALID_MIN = 45        # a feed stays valid this long past its data time (renders run every 30 min)


def save_feeds(root, cards, manifest, now):
    """--feeds-out: cards / manifest as save_cards takes them -> root/feeds/<panel>/{deck.json, cards/<id>.png}.
    now is the data time (naive local). Cards must already be pure inks: anything else raises."""
    pal = Image.new("P", (1, 1))
    pal.putpalette([v for c in (PK, PW, PY, PR) for v in c])
    t0 = now.replace(tzinfo=ZONE) if now.tzinfo is None else now
    for panel, entries in manifest.items():
        d = os.path.join(root, "feeds", panel)
        os.makedirs(os.path.join(d, "cards"), exist_ok=True)
        deck = []
        for i, e in enumerate(entries):
            im = cards[panel][e["id"]].convert("RGB")
            stray = {c for _, c in im.getcolors(maxcolors=1 << 24)} - {PK, PW, PY, PR}
            if stray:
                raise ValueError(f"{panel}/{e['id']}: {len(stray)} colours are not inks, e.g. {sorted(stray)[:3]}")
            q = im.quantize(palette=pal, dither=Image.Dither.NONE)
            ix = Image.frombytes("P", q.size, q.tobytes())
            ix.putpalette([v for c in (PK, PW, PY, PR) for v in c])
            ix.save(os.path.join(d, "cards", e["id"] + ".png"), bits=2)
            deck.append({"id": e["id"], "title": e["title"], "peek": e.get("peek", ""), "kind": "home" if i == 0 else "detail"})
        w, h = cards[panel][entries[0]["id"]].size
        with open(os.path.join(d, "deck.json"), "w") as f:
            json.dump({"cards": deck, "surface": f"{w}x{h}.k4", "valid_from": t0.isoformat(timespec="seconds"),
                       "valid_until": (t0 + timedelta(minutes=FEED_VALID_MIN)).isoformat(timespec="seconds")}, f, indent=1)


def events_path():
    if "--events" in sys.argv:
        return sys.argv[sys.argv.index("--events") + 1]
    synced = os.path.join(os.environ.get("EPAPER_STATE", "/var/lib/epaper"), "calendar", "agenda.json")
    if os.path.exists(synced):
        return synced
    mine = os.path.join(REPO, "config", "events.json")
    return mine if os.path.exists(mine) else os.path.join(ASSETS, "events.example.json")


def _replace(path, write):
    """Write a file through path.tmp and os.replace, so a reader never sees half of it."""
    tmp = path + ".tmp"
    write(tmp)
    os.replace(tmp, path)


def save(name, rgb, zoom_box, previews=True):
    os.makedirs(OUT, exist_ok=True)
    img = Image.fromarray(rgb)
    data = pack(img)

    def write_bin(tmp):
        with open(tmp, "wb") as fh:
            fh.write(data)
    _replace(os.path.join(OUT, name + ".bin"), write_bin)
    _replace(os.path.join(OUT, name + ".png"), lambda tmp: img.save(tmp, format="PNG"))
    if not previews:
        return
    idx = np.zeros(rgb.shape[:2], np.uint8)
    for code, c in enumerate(ht.PURE):
        idx[(rgb == c).all(-1)] = code
    sim = Image.fromarray(ht.PANEL[idx])
    sim.filter(ImageFilter.GaussianBlur(1.6)).save(os.path.join(OUT, name + "_sim.png"))
    x0, y0, x1, y1 = zoom_box
    sim.crop(zoom_box).resize(((x1 - x0) * 4, (y1 - y0) * 4), Image.NEAREST).save(os.path.join(OUT, name + "_zoom.png"))


if __name__ == "__main__":
    os.makedirs(CACHE, exist_ok=True)
    evs = json.load(open(events_path()))
    wind = fetch_wind(os.path.join(CACHE, "wind.json"))

    # live: today onward
    vic = fetch_victoria(os.path.join(CACHE, "victoria_live.json"), forecast_days=6, current="temperature_2m")
    now = datetime.fromisoformat(vic["current"]["time"])
    aqi = fetch_aqi(os.path.join(CACHE, "aqi_live.json"), current="us_aqi")["current"]["us_aqi"]
    # hourly precipitation at T is the total for (T-1h, T]: with the series starting at the
    # current hour H, the next full hour [H+1, H+2) is index 2; cloud cover averages indices 1-2
    # --live-only: just the live frame (no storm demo, no _sim / _zoom previews), e.g. on the device
    live_only = "--live-only" in sys.argv
    pr = cc = None
    title = ""
    if "--radar" in sys.argv:                  # the radar map needs the Open-Meteo grid (hundreds of calls)
        live = fetch_grid(os.path.join(CACHE, f"grid_rot{ROT_DEG:g}_w{MAP_W}_live.json"), "https://api.open-meteo.com/v1/forecast",
                          forecast_hours=4)
        times = live[0]["hourly"]["time"]
        pr, cc = grid_fields(live, 2, (1, 2))
        title = f"NEXT HOUR {times[1][-5:-3]}–{times[2][-5:-3]}h"
    drawer = None
    from renderer import gfa
    valid = None
    if "--radar" not in sys.argv:
        valid = (now.replace(tzinfo=ZONE).astimezone(timezone.utc) + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0, tzinfo=None)
        fields = gfa.geomet_fields(valid)
        fz = gfa.gfs_column(*ORIGIN, valid)[1]
        drawer = lambda img: gfa.draw_gfa_map(img, fields, valid, gfa.fetch_wpc(),
                                              lambda la, lo: gfa.gfs_column(la, lo, valid), fz)
    canvas = build(pr, cc, title, vic, wind, now, aqi, evs, now, drawer)
    home = bodies(canvas)
    cards = {p: {"home": home[p]} for p in ORDER}
    manifest = {"left": [{"id": "home", "title": "Map now" if "--radar" in sys.argv else "GFA now"}],
                "middle": [{"id": "home", "title": "Today"}], "right": [{"id": "home", "title": "Upcoming"}]}
    if "--radar" not in sys.argv and "--no-detail" not in sys.argv:
        wpc = gfa.fetch_wpc()
        for hours in (6, 12):                      # map deck: the GFA later on
            v = valid + timedelta(hours=hours - 1)
            try:
                f_h = gfa.geomet_fields(v)
                m = Image.new("RGB", (MAP_W, MAP_H), PW)
                gfa.draw_gfa_map(m, f_h, v, wpc, lambda la, lo, v=v: gfa.gfs_column(la, lo, v), gfa.gfs_column(*ORIGIN, v)[1])
                cards["left"][f"gfa{hours}"] = m
                manifest["left"].append({"id": f"gfa{hours}", "title": f"GFA +{hours} h · {v:%H}Z"})
            except Exception as e:                 # noqa: BLE001 - a missing forecast hour just isn't in the deck
                print(f"GFA +{hours} h skipped: {e}")
    mw = PANELS["middle"][1] - PANELS["middle"][0] + 1
    cards["middle"]["hours"] = hours_card(vic, now, mw)
    manifest["middle"].append({"id": "hours", "title": "Next 24 hours"})
    rw = PANELS["right"][1] - PANELS["right"][0] + 1
    cards["right"]["metar"] = metar_card(wind, fetch_tafs(os.path.join(CACHE, "taf.json")), rw)
    manifest["right"].append({"id": "metar", "title": "METAR · TAF"})
    save_cards(cards, manifest)
    if "--feeds-out" in sys.argv:
        save_feeds(sys.argv[sys.argv.index("--feeds-out") + 1], cards, manifest, now)
    frame = decks.compose(manifest=manifest)
    decks.save_frame(frame)
    save("live", np.asarray(frame), (220, 180, 400, 300), previews=not live_only)
    if live_only:
        print("wrote", OUT)
        sys.exit(0)

    # storm demo: the whole weather side is 10 Dec 2025 at 02:30 (archive); AQI forced bad to show that line
    vic_s = fetch_victoria(os.path.join(CACHE, "victoria_storm.json"), "https://historical-forecast-api.open-meteo.com/v1/forecast",
                           start_date="2025-12-10", end_date="2025-12-15")
    storm = fetch_grid(os.path.join(CACHE, f"grid_rot{ROT_DEG:g}_w{MAP_W}_storm.json"), "https://historical-forecast-api.open-meteo.com/v1/forecast",
                       start_date="2025-12-10", end_date="2025-12-10")
    pr, cc = grid_fields(storm, 3, (2, 3))
    wind_demo = [dict(id="YYJ", dir=200, spd=16, gst=27, ceil=2800, cat="MVFR", src="demo", vis=6, wx="-RA",
                      layers=[("FEW", 1200), ("BKN", 2800), ("OVC", 6000)]),
                 dict(id="YCD", dir=150, spd=9, gst=None, ceil=700, cat="IFR", src="demo", vis=2, wx="-RA BR",
                      layers=[("OVC", 700)]),
                 dict(id="YVR", dir=110, spd=14, gst=None, ceil=4000, cat="VFR", src="demo", vis=15, wx="",
                      layers=[("SCT", 2500), ("BKN", 4000)]),
                 dict(id="CAT4", dir=160, spd=8, gst=18, ceil=2500, cat="MVFR", src="stn+GFS", vis=8, wx="-RA",
                      layers=[("SCT", 1500), ("BKN", 2500), ("OVC", 9000)], modelled=True),
                 dict(id="ZBB", dir=None, spd=6, gst=None, ceil=800, cat="IFR", src="stn+GFS", vis=2, wx="BR",
                      layers=[("OVC", 800)], modelled=True)]
    drawer = None
    if "--radar" not in sys.argv:
        from renderer import gfa
        valid_s = datetime(2025, 12, 10, 11)                 # 02:30 PST is 10:30Z; the 11Z rate covers 10-11Z
        hist = "https://historical-forecast-api.open-meteo.com/v1/forecast"
        fields_s = gfa.openmeteo_fields(os.path.join(CACHE, f"gfa_storm_w{MAP_W}.json"), hist, 11,
                                        start_date="2025-12-10", end_date="2025-12-10")
        col = lambda la, lo: gfa.gfs_column(la, lo, valid_s, hist, start_date="2025-12-10", end_date="2025-12-10")
        fz_s = col(*ORIGIN)[1]
        drawer = lambda img: gfa.draw_gfa_map(img, fields_s, valid_s, None, col, fz_s)
    storm_canvas = build(pr, cc, "DEMO 10 DEC 02–03h", vic_s, wind_demo, datetime(2025, 12, 10, 2, 30), 158, evs, now, drawer)
    storm = decks.compose(ui=decks.load_ui(os.devnull), manifest=manifest, bodies=bodies(storm_canvas))
    save("storm", np.asarray(storm), (220, 180, 400, 300))
    print("wrote", OUT)
