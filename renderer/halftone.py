"""Halftoning for the 4-ink panel (black, white, yellow, red).

Every colour the panel can show is a mix of the four inks, so the gamut is the
tetrahedron K-W-Y-R in linear light. A target colour becomes ink fractions
(fK, fW, fY, fR >= 0, summing to 1) by nonnegative least squares; a screen then
decides which ink each pixel gets so that the local coverage matches.

Fraction arrays are HxWx4 in panel-code order: [BLACK, WHITE, YELLOW, RED].
"""
import numpy as np
from scipy.optimize import nnls

BLACK, WHITE, YELLOW, RED = 0, 1, 2, 3
PURE = np.array([(0, 0, 0), (255, 255, 255), (255, 255, 0), (255, 0, 0)], np.uint8)
# Rough look of the real inks on a Spectra-style 4-colour panel: dim paper white, warm yellow, brick
# red. ESTIMATES: calibrate by photographing the panel next to a white card.
PANEL = np.array([(38, 38, 42), (214, 212, 202), (224, 188, 26), (170, 36, 34)], np.uint8)


def srgb_to_lin(c):
    c = np.asarray(c, float) / 255
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def lin_to_srgb(v):
    v = np.clip(v, 0, 1)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * v ** (1 / 2.4) - 0.055) * 255


INK_LIN = srgb_to_lin(PANEL)  # 4x3


def _solve(t_lin):
    """Nearest ink mix for one linear-light colour; the sum-to-1 row is weighted heavily."""
    lam = 10.0
    a = np.vstack([INK_LIN.T, lam * np.ones((1, 4))])
    f, _ = nnls(a, np.append(t_lin, lam))
    return f / f.sum()


_LUT_N = 33
_lut = None


def _lut_get():
    global _lut
    if _lut is None:
        k, w = INK_LIN[BLACK], INK_LIN[WHITE]
        g = np.linspace(0, 255, _LUT_N)
        _lut = np.zeros((_LUT_N, _LUT_N, _LUT_N, 4))
        for i, r in enumerate(g):
            for j, gg in enumerate(g):
                for m, b in enumerate(g):
                    # map sRGB black/white onto the panel's black/white, then solve
                    t = k + srgb_to_lin((r, gg, b)) * (w - k)
                    _lut[i, j, m] = _solve(t)
    return _lut


def fractions_from_rgb(img_rgb):
    """HxWx3 uint8 sRGB -> HxWx4 ink fractions (nearest LUT entry)."""
    lut = _lut_get()
    q = np.rint(np.asarray(img_rgb, float) / 255 * (_LUT_N - 1)).astype(int)
    return lut[q[..., 0], q[..., 1], q[..., 2]]


def mix(h, w, **inks):
    """Uniform fraction field, e.g. mix(40, 100, k=.3, y=.7)."""
    f = np.zeros((h, w, 4))
    for name, code in (("k", BLACK), ("w", WHITE), ("y", YELLOW), ("r", RED)):
        f[..., code] = inks.get(name, 0.0)
    f[..., WHITE] += 1 - f.sum(axis=2)
    return f


# ------------------------------------------------------------------ screens

def dot_screen(h, w, cell, angle_deg, x0=0, y0=0):
    """Clustered-dot threshold map in [0, 1): round dots that grow, touch at 50 % and
    invert beyond (the classic cosine spot function), rotated by angle_deg. Ranking
    the spot values makes coverage linear in the threshold."""
    y, x = np.mgrid[y0:y0 + h, x0:x0 + w].astype(float)
    a = np.radians(angle_deg)
    u = (x * np.cos(a) + y * np.sin(a)) / cell
    v = (-x * np.sin(a) + y * np.cos(a)) / cell
    spot = (np.cos(2 * np.pi * u) + np.cos(2 * np.pi * v)) / 2
    spot += 1e-9 * np.sin(x * 12.9898 + y * 78.233)   # break ties without a raster bias
    order = np.argsort(-spot, axis=None, kind="stable")
    t = np.empty(h * w)
    t[order] = (np.arange(h * w) + 0.5) / (h * w)
    return t.reshape(h, w)


def bayer(n):
    m = np.array([[0]])
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return (m + 0.5) / m.size


def bayer_screen(h, w, n=8):
    return np.tile(bayer(n), (h // n + 1, w // n + 1))[:h, :w]


# ------------------------------------------------------------------ renderers

def render_comic(f, cell=6, angles=(45, 75, 15), x0=0, y0=0):
    """Comic/print style: each ink has its own rotated dot screen (K 45°, R 75°, Y 15°
    by default, like CMYK screen angles). Where dots would overlap, K beats R beats Y,
    and the coverages are pre-scaled so the average mix still comes out right."""
    h, w, _ = f.shape
    fk, fy, fr = f[..., BLACK], f[..., YELLOW], f[..., RED]
    cr = fr / np.maximum(1 - fk, 1e-6)
    cy = fy / np.maximum(1 - fk - fr, 1e-6)
    out = np.full((h, w), WHITE, np.uint8)
    out[dot_screen(h, w, cell, angles[2], x0, y0) < cy] = YELLOW
    out[dot_screen(h, w, cell, angles[1], x0, y0) < cr] = RED
    out[dot_screen(h, w, cell, angles[0], x0, y0) < fk] = BLACK
    return out


def render_stacked(f, t):
    """One threshold map for all inks, stacked darkest first: K, then R, then Y, then W.
    With a dot screen this gives concentric dots (black core, red ring, yellow ring)."""
    c1 = f[..., BLACK]
    c2 = c1 + f[..., RED]
    c3 = c2 + f[..., YELLOW]
    out = np.full(t.shape, WHITE, np.uint8)
    out[t < c3] = YELLOW
    out[t < c2] = RED
    out[t < c1] = BLACK
    return out


def render_nearest(img_rgb):
    """No halftoning: each pixel takes the closest ink (what quantize() without dither does)."""
    k, w = INK_LIN[BLACK], INK_LIN[WHITE]
    t = k + srgb_to_lin(img_rgb) * (w - k)
    d = ((t[..., None, :] - INK_LIN[None, None]) ** 2).sum(-1)
    return d.argmin(-1).astype(np.uint8)


def render_error_diffusion(img_rgb):
    """Floyd-Steinberg in linear light against the panel inks: finest grain, no visible pattern.
    Targets are first pulled into the ink gamut; otherwise the error from unreachable colours
    (saturated oranges, anything blue) piles up and dumps out as solid stripes."""
    buf = fractions_from_rgb(img_rgb) @ INK_LIN
    h, wd, _ = buf.shape
    out = np.zeros((h, wd), np.uint8)
    for y in range(h):
        for x in range(wd):
            old = buf[y, x]
            i = int(((INK_LIN - old) ** 2).sum(1).argmin())
            out[y, x] = i
            e = old - INK_LIN[i]
            if x + 1 < wd:
                buf[y, x + 1] += e * 7 / 16
            if y + 1 < h:
                if x > 0:
                    buf[y + 1, x - 1] += e * 3 / 16
                buf[y + 1, x] += e * 5 / 16
                if x + 1 < wd:
                    buf[y + 1, x + 1] += e * 1 / 16
    return out


def to_rgb(idx, inks=PURE):
    return inks[idx]
