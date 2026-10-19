"""The weather slowapp (legacy, P4): runs `python3 -m renderer.weather --live-only --feeds-out` (the
renderer the board ran on the host until P4) and hands its three thirds to the hub as feeds through
slowapp_kit, which checks every card's size and inks.

Live: the renderer refetches everything (--refresh). Its API cache lives in RAM (/cache, a tmpfs, as
/run/epaper/cache does on the board today), except the GeoMet tiles: those go to /data/geomet, so a
valid hour's six tiles are fetched once and reused by later runs (memoisation, §9), and the
renderer's own 2-day pruning keeps the directory small. SD writes are only new tiles: about 9 KB x 6
layers per new valid hour, roughly 1.3 MB a day, ~4 MB with the +6 / +12 h detail hours. Fixture: the
example's /data is copied to a scratch cache and the renderer runs without --refresh under
--network none, so it reads only what the fixture ships (gfs columns and the +6 / +12 h GeoMet hours
aren't in it, so the map has no GFS cloud labels and the deck has no GFA detail cards).

Events: /imports/calendar/agenda.json when the calendar app exports one (R3, transitional), else
/data/events.json, else the renderer's own fallback (its made-up agenda), plus the built-in public
holidays (weather_app/holiday.py, settings holiday_country / holiday_subdiv) on dates the agenda has
none for. The merged list goes to the renderer as one --events file.

Contract 5: a run draws for one device and SLOWAPP_FEEDS lists only that device's feeds. The board
gets the three thirds (the renderer); a sleep screen gets `sleep` alone (weather_app/sleep.py, one
forecast request, no renderer run).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

from slowapp_kit import Card, Transient, job, log, main

from . import holiday, sleep

THIRDS = ("left", "middle", "right")
RENDERER_ROOT = Path(os.environ.get("WEATHER_RENDERER_ROOT", "/opt/app"))
RENDERER_EVENTS = Path("renderer/assets/events.example.json")   # the renderer's own fallback, under RENDERER_ROOT
SCRATCH = Path(os.environ.get("WEATHER_SCRATCH", "/tmp/weather"))
IMPORTS = Path(os.environ.get("SLOWAPP_IMPORTS", "/imports"))


def events_file(ctx):
    for p in (IMPORTS / "calendar" / "agenda.json", ctx.data / "events.json"):
        if p.is_file():
            return p
    return None


def events(ctx):
    """-> (the agenda plus the built-in holidays, where the agenda came from)."""
    src = events_file(ctx) or RENDERER_ROOT / RENDERER_EVENTS
    try:
        agenda = json.loads(src.read_text()) if src.is_file() else []
    except (OSError, ValueError) as e:
        log(f"weather: {src}: {e}; holidays only")
        agenda = []
    country, subdiv = holiday.setting(ctx.settings)
    return holiday.merge(agenda, holiday.builtin(country, subdiv, ctx.now.date())), src


def prepare(ctx):
    """A fresh EPAPER_RUN directory (the renderer's own variable); returns (run dir, feeds dir).
    run/cache is the fixture copy (fixture mode) or a RAM cache whose geomet/ is /data/geomet (live)."""
    shutil.rmtree(SCRATCH, ignore_errors=True)
    run, feeds = SCRATCH / "run", SCRATCH / "feeds"
    run.mkdir(parents=True)
    if ctx.fixture:
        shutil.copytree(ctx.data, run / "cache", ignore=shutil.ignore_patterns("snapshots"))
        return run, feeds
    ram = ctx.cache / "weather" if ctx.cache.is_dir() and os.access(ctx.cache, os.W_OK) else SCRATCH / "cache"
    shutil.rmtree(ram, ignore_errors=True)
    ram.mkdir(parents=True)
    (ctx.data / "geomet").mkdir(parents=True, exist_ok=True)
    (ram / "geomet").symlink_to(ctx.data / "geomet")
    (run / "cache").symlink_to(ram)
    return run, feeds


@job("render")
def render(ctx):
    titles = []
    thirds = [s for s in ctx.feeds if s.feed in THIRDS]
    if thirds:
        titles += render_thirds(ctx, thirds)
    if ctx.feed("sleep"):
        try:
            titles.append(sleep.write(ctx, ctx.feed("sleep"), holiday.today(events(ctx)[0], ctx.now.date())))
        except Exception as e:
            if not thirds:
                raise
            # The board's thirds are drawn: a strip the board doesn't show mustn't fail them.
            # The sleep feed keeps its last bundle until a run for its own device draws it.
            log(f"weather: sleep strip skipped: {e}")
            titles.append(f"sleep: skipped ({e})")
    ctx.write_summary("ok", titles, [{"label": "Update now", "job": "render"}])


def render_thirds(ctx, thirds):
    """The board's three thirds from the renderer; returns their summary lines."""
    run, feeds = prepare(ctx)

    cmd = [sys.executable, "-m", "renderer.weather", "--live-only", "--feeds-out", str(feeds)]
    if not ctx.fixture:
        cmd.append("--refresh")
    evs, src = events(ctx)
    ev = SCRATCH / "events.json"
    ev.write_text(json.dumps(evs))
    cmd += ["--events", str(ev)]
    env = dict(os.environ, EPAPER_RUN=str(run), EPAPER_STATE=str(SCRATCH / "state"))
    left = ctx.remaining()
    timeout = max(10, left - 10) if left is not None else int(ctx.manifest.get("app", {}).get("timeout_s", 300)) - 10
    log(f"weather: {' '.join(cmd[1:])} (events: {src} + holidays, {len(evs)} in all)")
    try:
        p = subprocess.run(cmd, cwd=RENDERER_ROOT, env=env, timeout=timeout, stdout=sys.stderr)
    except subprocess.TimeoutExpired:
        raise Transient(f"renderer timed out after {timeout:.0f} s")
    if p.returncode:
        msg = f"renderer exited {p.returncode}"
        if ctx.fixture:
            raise RuntimeError(msg)            # a fixture run has no network to blame: a real failure
        raise Transient(msg)                   # live: almost always an upstream fetch

    titles = []
    for surface in thirds:
        root = feeds / "feeds" / surface.feed
        if not (root / "deck.json").is_file():
            raise RuntimeError(f"renderer wrote no feed {surface.feed!r}")
        deck = json.loads((root / "deck.json").read_text())
        cards = [Card(c["id"], Image.open(root / "cards" / f"{c['id']}.png").convert("RGB"), c["title"],
                      c.get("peek", ""), c["kind"]) for c in deck["cards"]]
        ctx.write_feed(surface.feed, cards, deck["valid_from"], deck["valid_until"])
        titles.append(f"{surface.feed}: " + " · ".join(c.title for c in cards))
    return titles


if __name__ == "__main__":
    main()
