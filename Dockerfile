# The weather slowapp: the renderer (numpy, SciPy, Shapely, contourpy, tifffile) on the scientific
# base, pinned by tag and digest; Dependabot opens a PR when slowapp-base releases.
FROM ghcr.io/mike-anderson/slowapp-python-sci:1.5.0@sha256:fe0eadd3570670c298dff90f7301c5851612f7188c5b7cae450ec5137c84eec9
# Public holidays: the `holidays` wheel and its two dependencies, pinned with their sha256 in
# wheels.lock and unpacked into /opt/app/site (no pip in the base). First, so app changes reuse the layer.
COPY wheels.lock tools/install_wheels.py /tmp/wheels/
RUN python3 /tmp/wheels/install_wheels.py /tmp/wheels/wheels.lock /opt/app/site && rm -rf /tmp/wheels
COPY slowapp.toml /app/slowapp.toml
# renderer/assets/fonts is a symlink to the base's /opt/slowapp/fonts (the same files).
COPY epd /opt/app/epd
COPY renderer /opt/app/renderer
COPY weather_app /opt/app/weather_app
ENV PYTHONPATH=/opt/app:/opt/app/site:/opt/slowapp
RUN python3 -m compileall -q /opt/app \
 && python3 -c "import renderer.weather, weather_app" \
 && python3 -c "import datetime, holidays; assert holidays.CA(subdiv='BC', years=2026)[datetime.date(2026, 10, 12)] == 'Thanksgiving Day'" \
 && test -f /opt/app/renderer/assets/fonts/DejaVuSans.ttf
ENTRYPOINT ["python3", "-m", "weather_app"]
CMD ["job", "render"]
