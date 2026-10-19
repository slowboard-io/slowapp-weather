# slowapp-weather

The Slowboard dashboard's weather renderer packaged as a
[slowapp](https://github.com/mike-anderson/slowapp-base). One `render` job draws all three thirds
and returns them as three feeds:
- **left:** the HRDPS GFA map with WPC fronts;
- **middle:** Victoria conditions;
- **right:** airports and the agenda.

This is the *legacy* app (plan P4a). P5 splits it into map, today and airports apps.

| | |
|---|---|
| Base | `slowapp-python-sci` (numpy, SciPy, Shapely, contourpy, tifffile), pinned by tag and digest in `Dockerfile` |
| Grants | `network = "egress"`, 256 MB, `/data` ≤ 8 MB (GeoMet tiles memoised per valid hour), 8 MB RAM cache, imports `calendar:agenda` |
| Code | `weather_app/` is the kit wrapper; `renderer/` is the renderer; `epd/` holds just the layout, the pixel packing and the panel size |
| Fixture | `fixtures/live-2026-10-01`, a snapshot of every fetch, so CI renders offline (`network none`); `fixtures/sleep-2026-10-12`, its forecast relabelled to Thanksgiving for the sleep strip |
| Holidays | Built in with [`holidays`](https://pypi.org/project/holidays/) (`wheels.lock`, unpacked into `/opt/app/site`). Settings `holiday_country` (ISO 3166-1 alpha-2, default `CA`) and `holiday_subdiv` (default `BC`); `none` turns either off. Shown as the tag under the middle third's date and under the date on the sleep strip. An imported agenda's own holidays win on their dates. |

## Where the renderer lives

Until the board runs this app under Podman, its HostRunner still runs the copy in
epaper-dashboard's `renderer/`. That copy is frozen: make renderer changes here.
`tests/feeds_out_unchanged.sh` checks that `--feeds-out` changes nothing in the renderer's own
output.

## Releasing

Tag `vX.Y.Z`. The app workflow pushes `ghcr.io/mike-anderson/slowapp-weather` for armv7, arm64 and
amd64. The release notes hold the `image = "…@sha256:…"` line for `slowbhub.toml`.

Notices: [NOTICES.md](NOTICES.md).
