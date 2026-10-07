"""Card decks: put each panel's current card together with its peek strip into one frame.

  python3 -m renderer.cards [--name N] [--ui UI.json]
                                      compose out/.../N.bin + N.png (default live) from the rendered
                                      cards and a deck state (default /var/lib/epaper/ui.json)

renderer.weather draws every card's body (BODY_H tall) into out/.../cards/<panel>/<id>.png, lists
the decks in cards/manifest.json and composes its frame here. On the board slowbd owns the deck
state (sb-core::deck, since P2): which card each panel shows, whether its strip is armed (offers
its key) and any reminder prompt, and it draws the strips from a glyph atlas that tools/make_atlas.py
makes from draw_strip below (the parity gate, proto/golden/strips/). tools/reminders.py render
draws its rest cards with rest_card. Card bodies are composed here until per-slot panes (P5).
This module only draws a deck state:

  armed strip      [key] ▸ next card's title           what the key does now, and the deck position
  resting strip    the card's title, no key            right after a press: nothing to do here yet
  prompt strip     question + up to four answers       a reminder, on its panel's key and the spare
"""
import json
import os
import sys
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from epd.layout import (BODY_H, HEIGHT, KEY_OF, ORDER, PANELS, SPARE,
                        STRIP_H, WIDTH)
from epd.pack import pack

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FONTS = os.path.join(HERE, "assets", "fonts")
RUN = os.environ.get("EPAPER_RUN")
OUT = os.path.join(RUN, "out") if RUN else os.path.join(REPO, "out", "weather")
CARDS = os.path.join(OUT, "cards")
STATE_DIR = os.environ.get("EPAPER_STATE", "/var/lib/epaper")
UI_FILE = os.path.join(STATE_DIR, "ui.json")
# the four inks: black, white, yellow, red
PK, PW, PY, PR = (0, 0, 0), (255, 255, 255), (255, 255, 0), (255, 0, 0)

# What the keys answer on a reminder prompt: (own key or spare, tap or hold) -> answer
ANSWERS = (("own", "tap", "yes", "Yes"), ("spare", "tap", "not_yet", "Not yet"),
           ("own", "hold", "snooze", "1 h"), ("spare", "hold", "skip", "Skip"))

_fonts = {}


def font(name, size):
    key = (name, size)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(os.path.join(FONTS, name), size)
    return _fonts[key]


def serif(size):
    return font("AlfaSlabOne-Regular.ttf", size)


def cond(size, bold=True):
    return font("DejaVuSansCondensed-Bold.ttf" if bold else "DejaVuSansCondensed.ttf", size)


def sans(size, bold=True):
    return font("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", size)


def ellipsize(d, text, fnt, width):
    if d.textlength(text, font=fnt) <= width:
        return text
    while text and d.textlength(text + "…", font=fnt) > width:
        text = text[:-1]
    return text.rstrip(" -:·") + "…"


def wrap(d, text, fnt, width, lines):
    out, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if d.textlength(trial, font=fnt) <= width or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = word
    if cur:
        out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = ellipsize(d, out[-1] + " …", fnt, width)
    return out


# ------------------------------------------------------------------ state

def load_manifest(cards=CARDS):
    try:
        with open(os.path.join(cards, "manifest.json")) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {p: [{"id": "home", "title": "Home"}] for p in ORDER}


def load_ui(path=UI_FILE):
    try:
        with open(path) as f:
            ui = json.load(f)
    except (FileNotFoundError, ValueError):
        ui = {}
    panels = ui.get("panels") or {}
    ui["panels"] = {p: {"card": "home", "armed": True, **panels.get(p, {})} for p in ORDER}
    ui.setdefault("prompt", None)
    return ui


# ------------------------------------------------------------------ drawing

def key_glyph(d, key, cx, cy, s, fill):
    """The key's symbol, about s px across, in `fill`."""
    h = s / 2
    if key == "red":
        d.polygon([(cx, cy - h), (cx + h * 1.1, cy + h * .85), (cx - h * 1.1, cy + h * .85)], fill=fill)
    elif key == "yellow":
        d.polygon([(cx, cy - h * 1.15), (cx + h * 1.05, cy), (cx, cy + h * 1.15), (cx - h * 1.05, cy)], fill=fill)
    elif key == "black":
        d.rectangle((cx - h * .85, cy - h * .85, cx + h * .85, cy + h * .85), fill=fill)
    else:
        d.ellipse((cx - h, cy - h, cx + h, cy + h), fill=fill)


# the physical keycaps: cap colour and legend colour
CAP = {"red": (PR, PW), "yellow": (PY, PK), "black": (PK, PW), "white": (PW, PK)}


def key_chip(d, key, x, cy, w=34, h=24, hold=False):
    """The key as it looks on the device (a long cap in its colour, the symbol in the contrasting
    ink): the soft-key label. Returns its right edge. A hold is drawn as a doubled cap edge."""
    cap, legend = CAP[key]
    if hold:
        d.rounded_rectangle((x + 3, cy - h / 2 + 3, x + w + 3, cy + h / 2 + 3), 6, fill=PK)
    d.rounded_rectangle((x, cy - h / 2, x + w, cy + h / 2), 6, fill=cap, outline=PK, width=2)
    key_glyph(d, key, x + w / 2, cy, h * .55, legend)
    return x + w + (3 if hold else 0)


def dots(d, x1, cy, n, cur):
    """Deck position, right-aligned to x1: one dot per card, the shown one filled."""
    if n < 2:
        return x1
    x = x1 - n * 13 + 6
    for i in range(n):
        box = (x + i * 13 - 4, cy - 4, x + i * 13 + 4, cy + 4)
        d.ellipse(box, fill=PK if i == cur else PW, outline=PK, width=1)
    return x - 10


def draw_strip(img, panel, state, deck, prompt):
    x0, x1 = PANELS[panel]
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    y0 = BODY_H
    d.rectangle((x0, y0, x1, HEIGHT - 1), fill=PW)
    pad_l = 10 if panel == "left" else 18
    lx, rx = x0 + pad_l, x1 - 12
    key = KEY_OF[panel]
    ids = [c["id"] for c in deck]
    cur = ids.index(state["card"]) if state["card"] in ids else -1
    n = len(deck)
    cy = y0 + STRIP_H // 2 + 2

    if prompt and prompt.get("panel") == panel and state.get("armed"):
        d.rectangle((x0 + (0 if panel == "left" else 2), y0, x1, HEIGHT - 1), fill=PY)
        d.line((x0, y0, x1, y0), fill=PK, width=2)
        q = ellipsize(d, prompt.get("question") or prompt.get("title", ""), cond(15), x1 - lx - 8)
        d.text((lx, y0 + 2), q, font=cond(15), fill=PK)
        x = lx
        for who, tap, _, label in ANSWERS:
            k = key if who == "own" else SPARE
            x = key_chip(d, k, x, y0 + 26, w=28, h=16, hold=tap == "hold") + 5
            d.text((x, y0 + 26), label, font=cond(15), fill=PK, anchor="lm")
            x += d.textlength(label, font=cond(15)) + 14
        return

    # the card's top edge, and the edge of the next card peeking out under it
    d.line((x0, y0, x1, y0), fill=PK, width=2)
    if n > 1 or state["card"] == "rest":
        d.line((x0 + 10, y0 + 4, x1 - 10, y0 + 4), fill=PK, width=1)
    rx = dots(d, rx, cy, n, cur)
    if state.get("armed"):
        if n < 2 and state["card"] != "rest":
            d.text((lx, cy), deck[0]["title"], font=cond(16, False), fill=PK, anchor="lm")
            return
        nxt = deck[(cur + 1) % n] if cur >= 0 else deck[0]
        x = key_chip(d, key, lx, cy) + 10
        d.text((x, cy), "▸", font=sans(16), fill=PK, anchor="lm")
        x += 18
        if cur > 0 or state["card"] == "rest":
            hint = "hold: home"
            rx -= d.textlength(hint, font=cond(14, False)) + 6
            d.text((rx + 6, cy), hint, font=cond(14, False), fill=PK, anchor="lm")
            rx -= 8
        d.text((x, cy), ellipsize(d, nxt["title"], cond(19), rx - x), font=cond(19), fill=PK, anchor="lm")
    else:                                        # resting: what's showing, and no key to press
        title = (state.get("rest") or {}).get("strip") or (deck[cur]["title"] if cur >= 0 else "")
        d.text((lx, cy), ellipsize(d, title, cond(17, False), rx - lx), font=cond(17, False), fill=PK, anchor="lm")


def rest_card(w, rest):
    """The card a reminder answer produces. It asks nothing (the panel is resting)."""
    img = Image.new("RGB", (w, BODY_H), PW)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    x, x1 = 18, w - 16
    d.text((x, 10), "REMINDER", font=cond(16), fill=PR)
    if rest.get("at"):
        d.text((x1, 10), rest["at"], font=cond(16, False), fill=PK, anchor="ra")
    y = 38
    for line in wrap(d, rest.get("title", ""), serif(30), x1 - x, 2):
        d.text((x, y), line, font=serif(30), fill=PK)
        y += 40
    y += 16
    head = rest.get("head", "")
    tw = d.textlength(head, font=serif(54))
    if rest.get("answer") == "yes":
        d.rectangle((x - 6, y + 14, x + tw + 64, y + 66), fill=PY)
        # a drawn tick, since the slab face has no ✓
        tx = x + tw + 18
        d.line([(tx, y + 40), (tx + 12, y + 54), (tx + 36, y + 22)], fill=PK, width=7, joint="curve")
    d.text((x, y), head, font=serif(54), fill=PK)
    y += 92
    for line in rest.get("lines", []):
        for part in wrap(d, line, sans(20, False), x1 - x, 2):
            d.text((x, y), part, font=sans(20, False), fill=PK)
            y += 28
        y += 6
    if rest.get("footer"):
        d.text((x, BODY_H - 14), rest["footer"], font=cond(14, False), fill=PK, anchor="ls")
    return img


def body(panel, state, deck, cards=CARDS):
    x0, x1 = PANELS[panel]
    w = x1 - x0 + 1
    if state["card"] == "rest" and state.get("rest"):
        return rest_card(w, state["rest"])
    ids = [c["id"] for c in deck]
    cid = state["card"] if state["card"] in ids else "home"
    for c in (cid, "home"):
        path = os.path.join(cards, panel, c + ".png")
        try:
            key = (path, os.stat(path).st_mtime_ns)
        except FileNotFoundError:
            continue
        if key not in _bodies:
            _bodies[key] = Image.open(path).convert("RGB")
        if _bodies[key].size == (w, BODY_H):
            return _bodies[key].copy()
    return Image.new("RGB", (w, BODY_H), PW)


_bodies = {}


def compose(ui=None, manifest=None, cards=CARDS, bodies=None):
    """The whole frame for this deck state. bodies: {panel: Image} to use instead of the saved cards."""
    ui = ui or load_ui()
    manifest = manifest or load_manifest(cards)
    img = Image.new("RGB", (WIDTH, HEIGHT), PW)
    for p in ORDER:
        st = ui["panels"][p]
        deck = manifest.get(p) or [{"id": "home", "title": "Home"}]
        b = bodies[p] if bodies and st["card"] == "home" else body(p, st, deck, cards)
        img.paste(b, (PANELS[p][0], 0))
        draw_strip(img, p, st, deck, ui.get("prompt"))
    d = ImageDraw.Draw(img)
    d.line((PANELS["left"][1] + 1, 0, PANELS["left"][1] + 1, HEIGHT), fill=PK, width=3)
    rx0 = PANELS["right"][0]
    d.line((rx0 - 2, 12, rx0 - 2, BODY_H - 12), fill=PK, width=1)
    d.line((rx0 - 2, BODY_H + 8, rx0 - 2, HEIGHT - 8), fill=PK, width=1)
    return img


def save_frame(img, out=OUT, name="live"):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, name + ".bin.tmp"), "wb") as f:
        f.write(pack(img))
    os.replace(os.path.join(out, name + ".bin.tmp"), os.path.join(out, name + ".bin"))
    img.save(os.path.join(out, name + ".tmp.png"))
    os.replace(os.path.join(out, name + ".tmp.png"), os.path.join(out, name + ".png"))


if __name__ == "__main__":
    ui_path = sys.argv[sys.argv.index("--ui") + 1] if "--ui" in sys.argv else UI_FILE
    name = sys.argv[sys.argv.index("--name") + 1] if "--name" in sys.argv else "live"
    save_frame(compose(load_ui(ui_path)), name=name)
    print("composed", os.path.join(OUT, name + ".bin"), datetime.now().strftime("%H:%M:%S"))
