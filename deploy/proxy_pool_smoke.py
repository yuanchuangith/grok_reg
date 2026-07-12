from __future__ import annotations

import json
import sys
from pathlib import Path


root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
sys.path.insert(0, str(root))
import proxy_pool  # noqa: E402


config = json.loads((root / "config.json").read_text(encoding="utf-8"))
print(json.dumps(proxy_pool.prepare_proxy(config), ensure_ascii=False))
