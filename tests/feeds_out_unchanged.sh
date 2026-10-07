#!/usr/bin/env bash
# --feeds-out must be additive: render the shipped fixture inside the app image with and without
# the flag, and require identical live.bin and cards, plus all three feeds from the flagged run.
#   tests/feeds_out_unchanged.sh [image]      (SLOWAPP_PLATFORM / SLOWAPP_ENGINE as for `slowapp fixture`)
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${1:-slowapp-weather:dev}"
ENGINE="${SLOWAPP_ENGINE:-$(command -v podman >/dev/null && echo podman || echo docker)}"
FX="$PWD/fixtures/live-2026-10-01"
"$ENGINE" run --rm --network none ${SLOWAPP_PLATFORM:+--platform "$SLOWAPP_PLATFORM"} \
  -v "$FX:/fx:ro" --entrypoint sh "$IMAGE" -ec '
    cd /opt/app
    for run in plain flagged; do
      mkdir -p /tmp/$run && cp -r /fx /tmp/$run/cache
      extra=""; [ $run = flagged ] && extra="--feeds-out /tmp/feeds"
      EPAPER_RUN=/tmp/$run EPAPER_STATE=/tmp/state python3 -m renderer.weather --live-only --events /fx/events.json $extra >/dev/null 2>&1
    done
    cmp /tmp/plain/out/live.bin /tmp/flagged/out/live.bin
    diff -r /tmp/plain/out/cards /tmp/flagged/out/cards
    for f in left middle right; do test -s /tmp/feeds/feeds/$f/deck.json; done
    echo "--feeds-out is additive: live.bin and cards identical; feeds: $(ls /tmp/feeds/feeds | tr "\n" " ")"'
