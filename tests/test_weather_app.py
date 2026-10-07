"""Weather app wrapper tests (no renderer run), inside the base image: PYTHONPATH=.:/opt/slowapp"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import slowapp_kit as kit
import weather_app.__main__ as wp


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for d in ("data", "cache", "imports", "scratch"):
            (self.tmp / d).mkdir()
        patches = [mock.patch.object(wp, "IMPORTS", self.tmp / "imports"),
                   mock.patch.object(wp, "SCRATCH", self.tmp / "scratch" / "weather")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def ctx(self, mode="live"):
        return kit.Context.from_env(env=dict(
            SLOWAPP_MODE=mode, SLOWAPP_NOW="2026-10-01T13:15:00-07:00", SLOWAPP_STATE=str(self.tmp / "data"),
            SLOWAPP_CACHE=str(self.tmp / "cache"), SLOWAPP_OUT=str(self.tmp / "out"), SLOWAPP_MANIFEST="/nonexistent"))


class Events(Base):
    def test_precedence(self):
        c = self.ctx()
        self.assertIsNone(wp.events_file(c))
        (self.tmp / "data" / "events.json").write_text("[]")
        self.assertEqual(wp.events_file(c), self.tmp / "data" / "events.json")
        agenda = self.tmp / "imports" / "calendar" / "agenda.json"
        agenda.parent.mkdir()
        agenda.write_text("[]")
        self.assertEqual(wp.events_file(c), agenda)          # the calendar's export wins (R3)


class Cache(Base):
    def test_live_keeps_only_geomet_on_data(self):
        run, _ = wp.prepare(self.ctx())
        cache = run / "cache"
        self.assertEqual(cache.resolve(), (self.tmp / "cache" / "weather").resolve())
        (cache / "victoria_live.json").write_text("{}")
        (cache / "geomet" / "tile.tif").write_bytes(b"II")
        self.assertEqual(sorted(os.listdir(self.tmp / "data")), ["geomet"])
        self.assertTrue((self.tmp / "data" / "geomet" / "tile.tif").exists())
        # the next run starts with an empty RAM cache but keeps the tiles
        run, _ = wp.prepare(self.ctx())
        self.assertEqual(sorted(os.listdir(run / "cache")), ["geomet"])
        self.assertTrue((run / "cache" / "geomet" / "tile.tif").exists())

    def test_fixture_copies_data(self):
        (self.tmp / "data" / "wind.json").write_text("[]")
        (self.tmp / "data" / "snapshots").mkdir()
        run, _ = wp.prepare(self.ctx("fixture"))
        self.assertFalse((run / "cache").is_symlink())
        self.assertEqual(sorted(os.listdir(run / "cache")), ["wind.json"])


if __name__ == "__main__":
    unittest.main()
