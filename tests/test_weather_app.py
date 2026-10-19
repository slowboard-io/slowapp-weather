"""Weather app wrapper tests (no renderer run), inside the base image: PYTHONPATH=.:/opt/slowapp"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

import slowapp_kit as kit
import weather_app.__main__ as wp
from weather_app import holiday, sleep

try:
    import holidays  # noqa: F401  (in the app image, from wheels.lock; not in the bare base CI tests in)
    HAVE_HOLIDAYS = True
except ImportError:
    HAVE_HOLIDAYS = False
ROOT = Path(__file__).resolve().parents[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for d in ("data", "cache", "imports", "scratch"):
            (self.tmp / d).mkdir()
        patches = [mock.patch.object(wp, "IMPORTS", self.tmp / "imports"),
                   mock.patch.object(wp, "SCRATCH", self.tmp / "scratch" / "weather"),
                   mock.patch.object(wp, "RENDERER_ROOT", ROOT)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def ctx(self, mode="live", now="2026-10-01T13:15:00-07:00"):
        return kit.Context.from_env(env=dict(
            SLOWAPP_MODE=mode, SLOWAPP_NOW=now, SLOWAPP_STATE=str(self.tmp / "data"),
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


def hol(day, title):
    return dict(date=day, title=title, start=None, end=None, cal="holiday", reps=1)


THANKSGIVING = [hol("2026-10-12", "Thanksgiving Day"), hol("2026-11-11", "Remembrance Day")]


class Holidays(Base):
    @unittest.skipUnless(HAVE_HOLIDAYS, "the holidays package is in the app image, not the bare base")
    def test_ca_bc_known_dates(self):
        got = holiday.builtin("CA", "BC", date(2026, 10, 10))
        self.assertEqual([(e["date"], e["title"]) for e in got], [("2026-10-12", "Thanksgiving Day")])
        self.assertEqual(got[0]["cal"], "holiday")
        days = {e["date"]: e["title"] for e in holiday.builtin("CA", "BC", date(2026, 7, 25), ahead=45)}
        self.assertEqual(days.get("2026-08-03"), "British Columbia Day")
        self.assertEqual(days.get("2026-09-07"), "Labour Day")
        on = {e["date"] for e in holiday.builtin("CA", "ON", date(2026, 7, 25), ahead=45)}
        self.assertNotIn("2026-08-03", on)                   # BC Day is BC's
        self.assertIn("2026-09-07", on)
        # A subdivision the country doesn't have falls back to the national holidays; an unknown
        # country gives none rather than failing the run.
        self.assertIn("2026-09-07", {e["date"] for e in holiday.builtin("CA", "XX", date(2026, 9, 1))})
        self.assertEqual(holiday.builtin("ZZ", None, date(2026, 9, 1)), [])

    def test_settings(self):
        self.assertEqual(holiday.setting({}), ("CA", "BC"))  # the board is in Victoria
        self.assertEqual(holiday.setting({"holiday_country": " us ", "holiday_subdiv": "ny"}), ("US", "NY"))
        self.assertEqual(holiday.setting({"holiday_country": "CA", "holiday_subdiv": "none"}), ("CA", None))
        for off in ("", "none", "Off", "-"):
            self.assertIsNone(holiday.setting({"holiday_country": off})[0])

    def test_empty_country_gives_none(self):
        self.assertEqual(holiday.builtin(None, "BC", date(2026, 10, 10)), [])
        c = self.ctx()
        with mock.patch.object(kit.Context, "settings", {"holiday_country": "", "holiday_subdiv": "BC"}):
            evs, _ = wp.events(c)
        self.assertFalse([e for e in evs if e["cal"] == "holiday" and e["date"] >= "2026-10-01"])

    def test_imported_holiday_wins(self):
        agenda = [hol("2026-10-12", "Thanksgiving (regional holiday)"),
                  dict(date="2026-10-12", title="Dinner", start="18:00", end="21:00", cal="personal", reps=1)]
        merged = holiday.merge(agenda, THANKSGIVING)
        on12 = [e["title"] for e in merged if e["date"] == "2026-10-12" and e["cal"] == "holiday"]
        self.assertEqual(on12, ["Thanksgiving (regional holiday)"])           # no second banner
        self.assertIn(hol("2026-11-11", "Remembrance Day"), merged)          # the rest still added
        self.assertEqual(len(merged), 3)
        self.assertEqual(holiday.today(merged, date(2026, 10, 12)), "Thanksgiving")
        self.assertIsNone(holiday.today(merged, date(2026, 10, 13)))

    def test_events_merge_the_import_or_stand_alone(self):
        c = self.ctx()
        with mock.patch.object(holiday, "builtin", return_value=THANKSGIVING):
            # No import and no /data/events.json: the renderer's own agenda plus the holidays.
            evs, src = wp.events(c)
            self.assertEqual(src, ROOT / wp.RENDERER_EVENTS)
            self.assertIn(THANKSGIVING[0], evs)
            agenda = self.tmp / "imports" / "calendar" / "agenda.json"
            agenda.parent.mkdir()
            agenda.write_text(json.dumps([hol("2026-10-12", "Thanksgiving")]))
            evs, src = wp.events(c)
            self.assertEqual(src, agenda)
            self.assertEqual(evs, [hol("2026-10-12", "Thanksgiving"), THANKSGIVING[1]])
            agenda.write_text("{not json")
            self.assertEqual(wp.events(c)[0], THANKSGIVING)                   # a broken export: holidays only

    @unittest.skipUnless(HAVE_HOLIDAYS, "the holidays package is in the app image, not the bare base")
    def test_events_real_library(self):
        evs, _ = wp.events(self.ctx(now="2026-10-12T07:00:00-07:00"))
        self.assertEqual(holiday.today(evs, date(2026, 10, 12)), "Thanksgiving Day")


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

    def sleep_env(self, fixture, now):
        shutil.copy(fixture / "victoria_live.json", self.tmp / "data")
        return dict(
            SLOWAPP_CONTRACT="5", SLOWAPP_MODE="fixture", SLOWAPP_NOW=now,
            SLOWAPP_STATE=str(self.tmp / "data"), SLOWAPP_CACHE=str(self.tmp / "cache"),
            SLOWAPP_OUT=str(self.tmp / "out"), SLOWAPP_MANIFEST="/nonexistent",
            SLOWAPP_FEEDS=json.dumps([{"feed": "sleep", "w": 1284, "h": 340, "inks": "g16", "bpp": 8}]))

    def test_sleep_on_a_holiday(self):
        ctx = kit.Context.from_env(env=self.sleep_env(ROOT / "fixtures" / "sleep-2026-10-12", "2026-10-12T07:00:00-07:00"))
        with mock.patch.object(holiday, "builtin", return_value=THANKSGIVING):
            self.assertEqual(kit.execute("render", ctx)[0], 0)
        deck = json.loads((self.tmp / "out" / "feeds" / "sleep" / "deck.json").read_text())
        self.assertEqual(deck["cards"][0]["title"], "Monday, Thanksgiving Day")
        kit.check_png((self.tmp / "out" / "feeds" / "sleep" / "cards" / "home.png").read_bytes(), (1284, 340), "g16")

    def test_holiday_fits_the_strip(self):
        """At 1284 x 340 the holiday stays inside the left panel, in one or two lines, and nothing
        in the panel runs into the next block (each block is a separate band of inked rows)."""
        from renderer.weather import condensed
        w, h = 1284, 340
        s = h / 360
        width = int(w * .3) - 2 * int(24 * s)
        d = ImageDraw.Draw(Image.new("RGB", (w, h)))
        hf = condensed(int(24 * s), True)
        for name, most, want in (("Thanksgiving Day", 2, ["Thanksgiving Day"]),
                                 ("National Day for Truth and Reconciliation", 2, None),
                                 ("National Day for Truth and Reconciliation", 1, None),
                                 ("Saint-Jean-Baptiste Day; National Day for Truth and Reconciliation", 2, None)):
            lines = sleep.holiday_lines(d, name, hf, width, most)
            self.assertLessEqual(len(lines), most)
            self.assertTrue(all(d.textlength(t, font=hf) <= width for t in lines), lines)
            if want:
                self.assertEqual(lines, want)
        vic = json.loads((ROOT / "fixtures" / "sleep-2026-10-12" / "victoria_live.json").read_text())
        ds = sleep.days(vic, datetime(2026, 10, 12))
        left = int(w * .3) - 2

        def bands(img):
            px = img.convert("L").load()
            inked = [any(px[x, y] < 200 for x in range(left)) for y in range(h)]
            return sum(1 for y in range(h) if inked[y] and (y == 0 or not inked[y - 1]))
        # day, date, high/low, conditions, rain; then one band per holiday line
        self.assertEqual(bands(sleep.draw(w, h, ds, None)), 5)
        self.assertEqual(bands(sleep.draw(w, h, ds, None, "Thanksgiving Day")), 6)
        self.assertEqual(bands(sleep.draw(w, h, ds, None, "National Day for Truth and Reconciliation")), 7)

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
