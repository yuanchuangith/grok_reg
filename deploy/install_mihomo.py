"""Install the latest stable Mihomo Linux amd64 binary."""

from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path


API = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
TARGET = Path("/usr/local/bin/mihomo")


def request(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "grok-account-studio-deployer"})
    with urllib.request.urlopen(req, timeout=90) as response:
        return response.read()


release = json.loads(request(API).decode("utf-8"))
assets = release.get("assets") or []
patterns = (
    re.compile(r"^mihomo-linux-amd64-v3-.*\.gz$"),
    re.compile(r"^mihomo-linux-amd64-compatible-.*\.gz$"),
)
asset = next((item for pattern in patterns for item in assets if pattern.match(str(item.get("name") or ""))), None)
if not asset:
    raise SystemExit("No supported Linux amd64 Mihomo asset found")

compressed = request(str(asset["browser_download_url"]))
binary = gzip.decompress(compressed)
TARGET.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(dir=TARGET.parent, prefix="mihomo-", delete=False) as handle:
    handle.write(binary)
    temp_path = Path(handle.name)
os.chmod(temp_path, 0o755)
os.replace(temp_path, TARGET)
print(subprocess.check_output([str(TARGET), "-v"], text=True).strip())
