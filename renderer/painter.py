"""Drawing helpers shared by the renderers (renderer/weather.py, renderer/wifi_screen.py): the
four panel inks, the bundled fonts, and Painter, which draws text, bars and the weather icons in
exactly those inks with anti-aliasing off, so a drawing packs without dithering.
"""
import math
import os

from PIL import ImageDraw, ImageFont

K, W, Y, R = (0, 0, 0), (255, 255, 255), (255, 255, 0), (255, 0, 0)
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")


def _font(size, bold):
    return ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), size)


class Painter:
    def __init__(self, img):
        self.d = ImageDraw.Draw(img)
        self.d.fontmode = "1"  # no anti-aliasing, so every pixel is one of the four panel colours

    def text(self, xy, s, size, fill=K, bold=True, anchor="la"):
        self.d.text(xy, s, font=_font(size, bold), fill=fill, anchor=anchor)

    def bar(self, x0, y0, x1, y1, pct, fill):
        self.d.rectangle((x0, y0, x1, y1), outline=K, width=3)
        self.d.rectangle((x0 + 5, y0 + 5, x0 + 5 + (x1 - x0 - 10) * pct / 100, y1 - 5), fill=fill)

    # icons: drawn, not bitmaps, so they scale and stay in palette

    def sun(self, cx, cy, r):
        for i in range(8):
            a = i * math.pi / 4
            r0, r1 = r + max(4, r * .2), r + max(9, r * .5)
            self.d.line((cx + math.cos(a) * r0, cy + math.sin(a) * r0,
                         cx + math.cos(a) * r1, cy + math.sin(a) * r1), fill=K, width=max(3, r // 8))
        self.d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=Y, outline=K, width=max(2, r // 10))

    def cloud(self, x, y, w, h, fill=W):
        """Cloud in the box (x, y, w, h): black outline, then the fill inset."""
        parts = [(x, y + h * .45, x + w * .45, y + h), (x + w * .2, y + h * .1, x + w * .7, y + h * .8),
                 (x + w * .5, y + h * .35, x + w, y + h), (x + w * .2, y + h * .6, x + w * .85, y + h)]
        t = max(2, int(w // 22))
        for x0, y0, x1, y1 in parts:
            self.d.ellipse((x0, y0, x1, y1), fill=K)
        for x0, y0, x1, y1 in parts:
            self.d.ellipse((x0 + t, y0 + t, x1 - t, y1 - t), fill=fill)

    def rain(self, x, y, w, h):
        self.cloud(x, y, w, h * .7)
        for i in range(3):
            bx = x + w * (.25 + i * .25)
            self.d.line((bx, y + h * .78, bx - w * .08, y + h), fill=K, width=max(2, int(w // 16)))

    def partly(self, x, y, w, h):
        self.sun(int(x + w * .62), int(y + h * .3), int(w * .16))
        self.cloud(x, y + h * .3, w * .8, h * .7)

    def small_icon(self, name, cx, top):
        """Forecast-size icon centred on cx, 40 px tall from top."""
        if name == "sun":
            self.sun(cx, top + 20, 12)
        else:
            getattr(self, name)(cx - 26, top, 52, 40)

    def envelope(self, x, y, w, h):
        self.d.rectangle((x, y, x + w, y + h), fill=W, outline=K, width=4)
        self.d.line((x, y, x + w / 2, y + h * .6, x + w, y), fill=K, width=4)

