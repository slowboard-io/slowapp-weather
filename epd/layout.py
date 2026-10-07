"""Screen layout shared by the renderer (Pillow) and the board services (stdlib only).

The panel is three panels side by side, each a card deck (docs/INTERACTION.md). Every panel is a
body (the card, BODY_H tall) over a peek strip (STRIP_H) that shows what its key does next. The strip
can be refreshed on its own, so a key's action can be offered after the panel has rested.

Edges suit partial refresh (x0 % 4 == 0, x1 % 4 == 3): left sits wholly on the master IC (x < 680),
right wholly on the slave, the middle straddles both.
"""

WIDTH, HEIGHT = 1360, 480
ROW_BYTES = WIDTH // 4
STRIP_H = 36
BODY_H = HEIGHT - STRIP_H                    # 444

PANELS = {"left": (0, 451), "middle": (452, 907), "right": (908, 1359)}
ORDER = ("left", "middle", "right")

# The four soft keys, by ink. A layout gives one key to each panel, left to right; the last is spare.
KEYS = ("red", "yellow", "black", "white")
GLYPH = {"red": "▲", "yellow": "◆", "black": "■", "white": "●"}
OWNER = {"red": "left", "yellow": "middle", "black": "right"}
SPARE = "white"
KEY_OF = {p: k for k, p in OWNER.items()}


def rect(panel, part="all"):
    """A panel's area as (x0, x1, y0, y1): "body" (the card), "strip" (the peek strip) or "all"."""
    x0, x1 = PANELS[panel]
    return {"body": (x0, x1, 0, BODY_H - 1), "strip": (x0, x1, BODY_H, HEIGHT - 1), "all": (x0, x1, 0, HEIGHT - 1)}[part]


FULL = (0, WIDTH - 1, 0, HEIGHT - 1)


def bbox(rects):
    """One refresh window covering these rects. Each IC takes one window per refresh, so a set of
    areas goes out as their bounding box; anything inside it that wasn't asked for refreshes with
    what it already shows (and counts as refreshed for the cooldown)."""
    rects = list(rects)
    return (min(r[0] for r in rects), max(r[1] for r in rects), min(r[2] for r in rects), max(r[3] for r in rects))


def overlaps(a, b):
    return a[0] <= b[1] and b[0] <= a[1] and a[2] <= b[3] and b[2] <= a[3]


def window_arg(win):
    """panel_test.py --window form: X0:X1, or X0:X1:Y0:Y1 when it doesn't cover the full height.
    None for the whole panel (a full refresh)."""
    if win is None or tuple(win) == FULL:
        return None
    x0, x1, y0, y1 = win
    return f"{x0}:{x1}" if (y0, y1) == (0, HEIGHT - 1) else f"{x0}:{x1}:{y0}:{y1}"


def parse_window(arg):
    if not arg:
        return FULL
    v = [int(s) for s in arg.split(":")]
    return (v[0], v[1], 0, HEIGHT - 1) if len(v) == 2 else tuple(v)
