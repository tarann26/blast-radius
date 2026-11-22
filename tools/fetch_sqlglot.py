"""Build step: download sqlglot at the pinned commit from GitHub and unpack it to /app/repo."""
import io
import os
import shutil
import sys
import tarfile
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"
dest = sys.argv[1] if len(sys.argv) > 1 else "/app/repo"
data = urllib.request.urlopen(URL, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    tar.extractall("/app/unpack", filter="tar")
top = os.path.join("/app/unpack", os.listdir("/app/unpack")[0])
shutil.move(top, dest)
shutil.rmtree("/app/unpack")
print("sqlglot at", COMMIT, "in", dest)
