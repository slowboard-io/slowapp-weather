"""Convert images to and from the panel's packed 2-bit format. Needs Pillow;
runs on the desktop (or on the device if Pillow is installed)."""
from PIL import Image

from .epd10in85g import FRAME_BYTES, HEIGHT, WIDTH

PALETTE_RGB = [(0, 0, 0), (255, 255, 255), (255, 255, 0), (255, 0, 0)]  # index = panel code


def _palette_image():
    pal = Image.new("P", (1, 1))
    flat = [v for rgb in PALETTE_RGB for v in rgb]
    pal.putpalette(flat + [0, 0, 0] * (256 - len(PALETTE_RGB)))
    return pal


def pack(image, dither=False):
    """PIL image (1360x480, or 480x1360 portrait) -> packed frame bytes.
    UI graphics should be drawn in the four palette colours with dither=False;
    use dither=True for photos."""
    if image.size == (HEIGHT, WIDTH):
        image = image.rotate(90, expand=True)
    if image.size != (WIDTH, HEIGHT):
        raise ValueError(f"image is {image.size}, expected {(WIDTH, HEIGHT)}")
    q = image.convert("RGB").quantize(
        palette=_palette_image(),
        dither=Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE)
    return _pack_indices(q.tobytes())


def _pack_indices(px):
    """Palette indices, 1 byte per pixel -> 4 pixels per byte, first pixel in the high bits."""
    try:
        import numpy as np
        a = np.frombuffer(px, np.uint8).reshape(-1, 4)
        return ((a[:, 0] << 6) | (a[:, 1] << 4) | (a[:, 2] << 2) | a[:, 3]).astype(np.uint8).tobytes()
    except ImportError:
        out = bytearray(len(px) // 4)
        for i in range(len(out)):
            j = i * 4
            out[i] = (px[j] << 6) | (px[j + 1] << 4) | (px[j + 2] << 2) | px[j + 3]
        return bytes(out)


def pack_region(image):
    """Any PIL image whose width is a multiple of 4 -> its packed rows, back to back (no padding):
    a panel area, or a card body as panes ship it (proto/golden/blobs/)."""
    if image.width % 4:
        raise ValueError(f"width {image.width} isn't a multiple of 4")
    q = image.convert("RGB").quantize(palette=_palette_image(), dither=Image.Dither.NONE)
    return _pack_indices(q.tobytes())


def unpack(frame):
    """Packed frame bytes -> RGB PIL image, to preview exactly what the panel gets."""
    if len(frame) != FRAME_BYTES:
        raise ValueError(f"frame is {len(frame)} bytes, expected {FRAME_BYTES}")
    idx = bytearray(WIDTH * HEIGHT)
    for i, b in enumerate(frame):
        j = i * 4
        idx[j], idx[j + 1], idx[j + 2], idx[j + 3] = b >> 6, (b >> 4) & 3, (b >> 2) & 3, b & 3
    img = Image.frombytes("P", (WIDTH, HEIGHT), bytes(idx))
    img.putpalette([v for rgb in PALETTE_RGB for v in rgb])
    return img.convert("RGB")
