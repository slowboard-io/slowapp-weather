"""The panel's geometry, the only part of epaper-dashboard's epd/epd10in85g.py (the SPI driver) that
the renderer and pack.py use. The driver itself stays with the board (sb-hw in Rust)."""
WIDTH, HEIGHT = 1360, 480
ROW_BYTES = WIDTH // 4
FRAME_BYTES = ROW_BYTES * HEIGHT  # 163,200
