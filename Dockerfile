# The weather slowapp: the renderer (numpy, SciPy, Shapely, contourpy, tifffile) on the scientific
# base, pinned by tag and digest; Dependabot opens a PR when slowapp-base releases.
FROM ghcr.io/mike-anderson/slowapp-python-sci:1.2.0@sha256:8199b8ab24ff99ee4791e3dd06b724244dec0b293af3fc9c7e6fca9046db8e43
COPY slowapp.toml /app/slowapp.toml
# renderer/assets/fonts is a symlink to the base's /opt/slowapp/fonts (the same files).
COPY epd /opt/app/epd
COPY renderer /opt/app/renderer
COPY weather_app /opt/app/weather_app
ENV PYTHONPATH=/opt/app:/opt/slowapp
RUN python3 -m compileall -q /opt/app \
 && python3 -c "import renderer.weather, weather_app" \
 && test -f /opt/app/renderer/assets/fonts/DejaVuSans.ttf
ENTRYPOINT ["python3", "-m", "weather_app"]
CMD ["job", "render"]
