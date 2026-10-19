"""Public holidays, built in: the `holidays` package (pure Python, installed in the Dockerfile from
wheels.lock), so a hub without a calendar app (the cloud hub) still gets the date tag on the
board's middle third and the holiday name on the sleep strip.

Settings: `holiday_country` (ISO 3166-1 alpha-2, default CA) and `holiday_subdiv` (the province
or state, default BC). "none" (or "off", "-") turns either off; an empty country does too where a
hub passes it through (slowhub falls back to the default for an empty text box).

The calendar app's agenda, when imported, wins: a built-in holiday is dropped on any date the
agenda already has a `cal == "holiday"` entry for (the same holiday under its calendar's name,
"Thanksgiving" vs "Thanksgiving Day", or another holiday the user chose to show instead).

Events have the renderer's shape: {date, title, start, end, cal, reps}.
"""
import re
from datetime import timedelta

from slowapp_kit import log

COUNTRY, SUBDIV = "CA", "BC"          # the board is in Victoria
OFF = {"", "none", "off", "-"}
AHEAD = 14                            # days after today; the renderer only tags today's


def _code(value):
    v = str(value or "").strip()
    return None if v.lower() in OFF else v.upper()


def setting(settings):
    """-> (country or None, subdiv or None) from the app's settings (missing keys: CA, BC)."""
    return _code(settings.get("holiday_country", COUNTRY)), _code(settings.get("holiday_subdiv", SUBDIV))


def builtin(country, subdiv, today, ahead=AHEAD):
    """The country's (and subdivision's) public holidays from `today` (a date) to `ahead` days
    after, as holiday events in date order. [] when off, unknown, or the library is missing."""
    if not country:
        return []
    try:
        import holidays
    except ImportError:
        log("weather: the holidays package is not installed; no built-in holidays")
        return []
    end = today + timedelta(days=ahead)
    years = sorted({today.year, end.year})
    try:
        cal = holidays.country_holidays(country, subdiv=subdiv, years=years)
    except NotImplementedError:
        if not subdiv:
            log(f"weather: no holidays for country {country!r}")
            return []
        log(f"weather: no holidays for {country}-{subdiv}; using {country}'s national holidays")
        try:
            cal = holidays.country_holidays(country, years=years)
        except NotImplementedError:
            return []
    out = []
    for day, names in sorted(cal.items()):
        if today <= day <= end:
            # One entry per holiday: the library joins two on one day with "; ".
            for name in names.split("; "):
                out.append(dict(date=day.isoformat(), title=name, start=None, end=None, cal="holiday", reps=1))
    return out


def merge(events, extra):
    """`events` (the agenda) plus the built-in `extra`, without a built-in holiday on a date the
    agenda already has a holiday for."""
    taken = {e["date"] for e in events if e.get("cal") == "holiday"}
    return list(events) + [e for e in extra if e["date"] not in taken]


def name(title):
    """A holiday's title for the wall: without the calendar's "(regional holiday)" notes."""
    return re.sub(r"\s*\(.*?\)", "", title).strip()


def today(events, day):
    """The first holiday's name on `day` (a date), or None."""
    key = day.isoformat()
    return next((name(e["title"]) for e in events if e.get("cal") == "holiday" and e["date"] == key), None)
