"""Weather app wrapper tests (no renderer run), inside the base image: PYTHONPATH=.:/opt/slowapp"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import slowapp_kit as kit
import weather_app.__main__ as wp
from weather_app import sleep


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


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "live-2026-10-01"


class Sleep(Base):
    """Contract 5: a run for a sleep screen draws only `sleep`, from one forecast, and never starts
    the renderer."""

    def test_sleep_only_run_skips_the_renderer(self):
        shutil.copy(FIXTURE / "victoria_live.json", self.tmp / "data")
        env = dict(
            SLOWAPP_CONTRACT="5", SLOWAPP_MODE="fixture", SLOWAPP_NOW="2026-10-01T07:00:00-07:00",
            SLOWAPP_STATE=str(self.tmp / "data"), SLOWAPP_CACHE=str(self.tmp / "cache"),
            SLOWAPP_OUT=str(self.tmp / "out"), SLOWAPP_MANIFEST="/nonexistent",
            SLOWAPP_FEEDS=json.dumps([{"feed": "sleep", "w": 1284, "h": 340, "inks": "g16", "bpp": 8}]))
        ctx = kit.Context.from_env(env=env)
        with mock.patch.object(wp.subprocess, "run", side_effect=AssertionError("renderer started")):
            self.assertEqual(kit.execute("render", ctx)[0], 0)
        out = self.tmp / "out"
        self.assertEqual(os.listdir(out / "feeds"), ["sleep"])
        deck = json.loads((out / "feeds" / "sleep" / "deck.json").read_text())
        self.assertEqual(deck["surface"], "1284x340.g16")
        self.assertEqual(deck["cards"][0]["peek"], "H 18° L 10°")
        # Named, not "Today": a Supernote can hold the strip a day or more.
        self.assertEqual(deck["cards"][0]["title"], "Thursday")
        self.assertEqual(deck["valid_until"], "2026-10-02T00:00:00-07:00")
        kit.check_png((out / "feeds" / "sleep" / "cards" / "home.png").read_bytes(), (1284, 340), "g16")

    def test_days_start_today(self):
        vic = json.loads((FIXTURE / "victoria_live.json").read_text())
        from datetime import datetime
        ds = sleep.days(vic, datetime.fromisoformat("2026-10-01T07:00:00-07:00"))
        self.assertEqual([d["date"].strftime("%a") for d in ds], ["Thu", "Fri", "Sat", "Sun", "Mon", "Tue"])
        self.assertEqual((ds[0]["hi"], ds[0]["lo"]), (18, 10))
        self.assertEqual(sleep.rain(dict(pop=0, mm=0)), "No rain expected")
        self.assertEqual(sleep.rain(dict(pop=5, mm=0.1)), "No rain expected")
        self.assertEqual(sleep.rain(dict(pop=60, mm=4.24)), "Rain 60% · 4.2 mm")
        self.assertEqual(sleep.rain(dict(pop=90, mm=23.6)), "Rain 90% · 24 mm")
        self.assertEqual(sleep.rain(dict(pop=0, mm=0.5)), "Rain 0% · 0.5 mm")
        self.assertEqual(sleep.rain(dict(pop=29, mm=0.0)), "Rain 29%")
        # A snapshot that doesn't cover today gives no days, not another day's as today's.
        self.assertEqual(sleep.days(vic, datetime.fromisoformat("2026-11-20T07:00:00-08:00")), [])


if __name__ == "__main__":
    unittest.main()
