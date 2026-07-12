from __future__ import annotations

import json
import sys
from pathlib import Path


path = Path(sys.argv[1])
mode = sys.argv[2]
enabled = sys.argv[3].lower() in {"1", "true", "yes", "on"}
if mode not in {"static_pool", "clash_subscription"}:
    raise SystemExit("mode must be static_pool or clash_subscription; use enabled=false for direct connection")
data = json.loads(path.read_text(encoding="utf-8"))
data["proxy_enabled"] = enabled
data["proxy_mode"] = mode
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"proxy mode={mode} enabled={enabled}")
