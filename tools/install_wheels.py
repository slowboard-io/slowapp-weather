"""Unpack the pure-Python wheels in a lock file into a directory on PYTHONPATH, checking each
one's sha256 (the Dockerfile's only use; the base image has no pip).

  python3 tools/install_wheels.py wheels.lock /opt/app/site
"""
import hashlib
import io
import sys
import urllib.request
import zipfile


def main(lock, dest):
    for line in open(lock):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, sha = line.split()
        if not url.endswith("-none-any.whl"):
            sys.exit(f"{url}: not a pure-Python wheel")
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
        got = hashlib.sha256(data).hexdigest()
        if got != sha:
            sys.exit(f"{url}: sha256 {got}, lock says {sha}")
        zipfile.ZipFile(io.BytesIO(data)).extractall(dest)
        print(f"{url.rsplit('/', 1)[1]}: ok")


if __name__ == "__main__":
    main(*sys.argv[1:])
