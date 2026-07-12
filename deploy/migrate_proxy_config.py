"""Add proxy-pool defaults to an existing config.json without overwriting choices."""

from __future__ import annotations

import json
import sys
from pathlib import Path


root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
config_path = root / "config.json"
template_path = root / "config.example.json"
config = json.loads(config_path.read_text(encoding="utf-8"))
template = json.loads(template_path.read_text(encoding="utf-8"))
for key, value in template.items():
    if key == "proxy_enabled" or key == "proxy_mode" or key.startswith("proxy_pool_") or key in {
        "proxy_subscription_url",
        "proxy_subscriptions",
        "proxy_static_proxies",
        "proxy_selection_mode",
        "proxy_selected_node",
        "proxy_fallback_direct",
    }:
        config.setdefault(key, value)
config["proxy_pool_mihomo_path"] = "/usr/local/bin/mihomo"
config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"proxy mode: {config.get('proxy_mode')} enabled={config.get('proxy_enabled')}")
