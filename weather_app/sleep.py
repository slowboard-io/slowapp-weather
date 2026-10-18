"""The sleep-screen strip (contract 5, feed `sleep`, g16): today's high and low, then the next five
days. Drawn for greyscale glass that only refreshes when the device goes to sleep, so nothing in it
is "now": no current temperature, sparkline or callouts.

One Open-Meteo request (the same Victoria forecast the board's middle third uses), a second or two,
none of the map's GeoMet tiles. Fixture mode reads the example's victoria_live.json.
"""
import math
import urllib.parse
from datetime import datetime, timedelta

from PIL import Image, ImageDraw

from renderer.weather import CONDITIONS, MODEL, ORIGIN, ZONE_NAME, day_kind, font, serif
from slowapp_kit import Card, grey, snap_g16

SNAPSHOT = "victoria_live.json"
URL = "https://api.open-meteo.com/v1/forecast"

INK, SOFT, RULE, FILL, PAPER = grey(0), grey(5), grey(9), grey(12), grey(15)


def forecast(ctx):
    q = dict(latitude=ORIGIN[0], longitude=ORIGIN[1], timezone=ZONE_NAME, models=MODEL, forecast_days=6,
             hourly="weather_code,precipitation,precipitation_probability,cloud_cover",
             daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum")
    return ctx.fetch(SNAPSHOT, lambda: ctx.http_json(URL + "?" + urllib.parse.urlencode(q)))


def days(vic, today):
    """Today and the five after it, from the daily series; the icon from daytime hours (day_kind)."""
    d = vic["daily"]
    start = d["time"].index(today.strftime("%Y-%m-%d")) if today.strftime("%Y-%m-%d") in d["time"] else 0
    out = []
    for i in range(start, min(start + 6, len(d["time"]))):
        out.append(dict(
            date=datetime.fromisoformat(d["time"][i]), kind=day_kind(vic, d["time"][i]),
            code=d["weather_code"][i], hi=round(d["temperature_2m_max"][i]), lo=round(d["temperature_2m_min"][i]),
            pop=d["precipitation_probability_max"][i] or 0, mm=d["precipitation_sum"][i] or 0))
    return out


def sun(d, cx, cy, r):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=PAPER, outline=INK, width=max(3, r // 6))
    for k in range(8):
        a = k * math.pi / 4
        x0, y0 = cx + math.cos(a) * r * 1.35, cy + math.sin(a) * r * 1.35
        x1, y1 = cx + math.cos(a) * r * 1.75, cy + math.sin(a) * r * 1.75
        d.line((x0, y0, x1, y1), fill=INK, width=max(3, r // 6))


def cloud(d, cx, cy, w, fill):
    h = int(w * .6)
    x, y = cx - w // 2, cy - h // 2
    t = max(3, w // 18)
    parts = [(x, y + h * .45, x + w * .45, y + h), (x + w * .2, y + h * .1, x + w * .7, y + h * .8),
             (x + w * .5, y + h * .35, x + w, y + h), (x + w * .2, y + h * .6, x + w * .85, y + h)]
    for e in parts:
        d.ellipse(e, fill=INK)
    for a, b, c, e in parts:
        d.ellipse((a + t, b + t, c - t, e - t), fill=fill)
    return y + h


def icon(d, kind, pop, cx, cy, size):
    """The board's icons in greys: the cloud's fill darkens with the chance of rain."""
    if kind == "sun" and pop < 30:
        sun(d, cx, cy, size // 4)
        return
    if kind == "partly":
        sun(d, cx + size // 4, cy - size // 5, size // 6)
    fill = grey(15 - round(min(pop, 100) / 100 * 6))
    bottom = cloud(d, cx, cy + (0 if kind != "rain" else -size // 10), size, fill)
    if kind == "rain":
        for dx in (-.28, 0, .28):
            x, y = cx + dx * size, bottom + size * .12
            d.line((x + 4, y - 6, x - 4, y + 10), fill=INK, width=max(3, size // 20))


def draw(w, h, days_, now):
    """The strip at w x h in the sixteen greys."""
    img = Image.new("RGB", (w, h), PAPER)
    d = ImageDraw.Draw(img)
    s = h / 360                                   # drawn for 1360 x 360; scales with the height
    pad = int(24 * s)
    today, rest = days_[0], days_[1:6]

    # Today: the date, the high and the low, what the day looks like.
    left_w = int(w * .3)
    d.text((pad, pad), "Today", font=serif(int(40 * s)), fill=INK, anchor="lt")
    d.text((pad, pad + int(52 * s)), now.strftime("%A %-d %B"), font=font(int(24 * s), False), fill=SOFT, anchor="lt")
    y = pad + int(110 * s)
    d.text((pad, y), f"{today['hi']}°", font=font(int(104 * s)), fill=INK, anchor="lt")
    hi_w = d.textlength(f"{today['hi']}°", font=font(int(104 * s)))
    d.text((pad + hi_w + int(18 * s), y + int(16 * s)), "high", font=font(int(22 * s), False), fill=SOFT, anchor="lt")
    d.text((pad + hi_w + int(18 * s), y + int(52 * s)), f"low {today['lo']}°", font=font(int(30 * s)), fill=INK, anchor="lt")
    words = CONDITIONS.get(today["code"], "")
    if today["pop"] >= 30:
        words = f"{words}, {today['pop']}% rain" if words else f"{today['pop']}% rain"
    d.text((pad, h - pad), words, font=font(int(26 * s), False), fill=INK, anchor="lb")
    d.line((left_w, pad, left_w, h - pad), fill=RULE, width=max(2, int(3 * s)))

    # The next five days.
    x0, x1 = left_w + pad, w - pad
    cw = (x1 - x0) / max(1, len(rest))
    for i, day in enumerate(rest):
        cx = int(x0 + cw * i + cw / 2)
        if i:
            d.line((x0 + cw * i, pad + int(10 * s), x0 + cw * i, h - pad - int(10 * s)), fill=FILL, width=max(1, int(2 * s)))
        d.text((cx, pad + int(4 * s)), day["date"].strftime("%a"), font=serif(int(34 * s)), fill=INK, anchor="mt")
        icon(d, day["kind"], day["pop"], cx, pad + int(118 * s), int(96 * s))
        d.text((cx, h - pad - int(64 * s)), f"{day['hi']}°", font=font(int(40 * s)), fill=INK, anchor="mb")
        lo = f"{day['lo']}°"
        if day["pop"] >= 30:
            lo += f"  {day['pop']}%"
        d.text((cx, h - pad), lo, font=font(int(26 * s), False), fill=SOFT, anchor="mb")
    return snap_g16(img)


def write(ctx, surface):
    """Fetch, draw and write the sleep feed for `surface`. Returns its summary line."""
    vic = forecast(ctx)
    ds = days(vic, ctx.now)
    if len(ds) < 6:
        raise RuntimeError(f"forecast has {len(ds)} days from today, need 6")
    img = draw(surface.w, surface.h, ds, ctx.now)
    t = ds[0]
    # Good until the day changes: the strip is about today and the days after.
    midnight = (ctx.now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    ctx.write_feed(surface.feed, [Card("home", img, title="Today", peek=f"H {t['hi']}° L {t['lo']}°")],
                   valid_until=midnight)
    return f"sleep: H {t['hi']}° L {t['lo']}°, then " + " ".join(f"{d['date']:%a} {d['hi']}°" for d in ds[1:])
