# Third-party notices

- `renderer/assets/geo/coast.geojson` is cut from Natural Earth's 10 m land and minor islands
  (https://www.naturalearthdata.com/, public domain).
- The runway headings in `renderer/weather.py` come from OurAirports' `runways.csv`
  (https://ourairports.com/data/, public domain).
- The fonts (DejaVu, Alfa Slab One) come from the base image at `/opt/slowapp/fonts`.
  `renderer/assets/fonts` is a symlink to that directory. The fonts' licences are in slowapp-base.
- The image bundles three Python packages from PyPI, unpacked into `/opt/app/site` (`wheels.lock`),
  each with its licence file in its `*.dist-info`: `holidays` (MIT,
  https://github.com/vacanza/holidays), `python-dateutil` (Apache-2.0 OR BSD-3-Clause) and `six` (MIT).
- Weather data is fetched at run time:
  - Open-Meteo (https://open-meteo.com/, CC BY 4.0; the frame credits it).
  - METARs and TAFs from aviationweather.gov (US government, public domain).
  - GeoMet / HRDPS from Environment and Climate Change Canada.
  - GFS and WPC from NOAA.
