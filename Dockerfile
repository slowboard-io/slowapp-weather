# The weather slowapp: the renderer (numpy, SciPy, Shapely, contourpy, tifffile) on the scientific
# base, pinned by tag and digest; Dependabot opens a PR when slowapp-base releases.
FROM ghcr.io/mike-anderson/slowapp-python-sci:1.1.1@sha256:e7f45258ec6803f52af83f9f4f0de1245921fc85d425a8a92a4d326ec9f0b1ca
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
