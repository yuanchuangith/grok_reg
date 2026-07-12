"""Mihomo-backed proxy pools for subscriptions and authenticated static IPs."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


LogFn = Callable[[str], None]
ROOT = Path(__file__).resolve().parent
GROUP_NAME = "PROXY_POOL"


class ProxyPoolError(RuntimeError):
    pass


def _noop(_: str) -> None:
    return


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "\x00".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha1(raw.encode('utf-8', errors='replace')).hexdigest()[:10]}"


def proxy_is_enabled(config: dict[str, Any], purpose: str = "register") -> bool:
    if "proxy_enabled" in config:
        return bool(config.get("proxy_enabled"))
    key = "cpa_proxy" if purpose == "cpa" else "proxy"
    return bool(str(config.get(key) or config.get("proxy") or "").strip())


def proxy_mode(config: dict[str, Any]) -> str:
    mode = str(config.get("proxy_mode") or "static_pool").strip().lower()
    if mode == "static":
        return "static_pool"
    if mode == "clash_subscription":
        return mode
    return "static_pool"


def selection_mode(config: dict[str, Any]) -> str:
    mode = str(config.get("proxy_selection_mode") or "random").strip().lower()
    return "manual" if mode == "manual" else "random"


def pool_proxy_url(config: dict[str, Any]) -> str:
    host = str(config.get("proxy_pool_host") or "127.0.0.1").strip()
    port = max(1, min(65535, int(config.get("proxy_pool_mixed_port", 17890) or 17890)))
    return f"http://{host}:{port}"


def effective_proxy(config: dict[str, Any], purpose: str = "register") -> str:
    """Both enabled modes are exposed through Mihomo's local mixed port."""
    return pool_proxy_url(config) if proxy_is_enabled(config, purpose) else ""


def health_target(config: dict[str, Any], purpose: str = "register") -> str:
    configured = str(config.get("proxy_pool_health_url") or "").strip()
    if configured:
        return configured
    return "https://accounts.x.ai/" if purpose in {"register", "cpa"} else "https://www.cloudflare.com/cdn-cgi/trace"


def _clean_name(value: Any, fallback: str) -> str:
    return str(value or "").strip() or fallback


def normalize_subscriptions(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = config.get("proxy_subscriptions")
    if not isinstance(raw_items, list):
        raw_items = []
    if not raw_items:
        legacy = str(config.get("proxy_subscription_url") or "").strip()
        if legacy:
            raw_items = [{"name": "默认订阅", "url": legacy, "enabled": True}]
    result: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, item in enumerate(raw_items, 1):
        if not isinstance(item, dict):
            continue
        name = _clean_name(item.get("name"), f"订阅 {index}")
        url = str(item.get("url") or "").strip()
        item_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(item.get("id") or "").strip())
        item_id = item_id or _stable_id("sub", name, url, index)
        while item_id in used:
            item_id = _stable_id("sub", item_id, index, len(used))
        used.add(item_id)
        result.append({"id": item_id, "name": name, "url": url, "enabled": bool(item.get("enabled", True))})
    return result


def parse_static_proxy(value: str, *, name: str = "", item_id: str = "", enabled: bool = True) -> dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        raise ProxyPoolError("静态代理内容为空")
    host = username = password = ""
    port = 0
    if "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        host = parsed.hostname or ""
        port = int(parsed.port or 0)
        username = urllib.parse.unquote(parsed.username or "")
        password = urllib.parse.unquote(parsed.password or "")
    else:
        parts = raw.split(":", 3)
        if len(parts) != 4:
            raise ProxyPoolError("静态代理格式应为 host:port:username:password")
        host, port_text, username, password = parts
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ProxyPoolError("静态代理端口必须是数字") from exc
    host = host.strip()
    username = username.strip()
    password = password.strip()
    if not host or not 1 <= port <= 65535 or not username or not password:
        raise ProxyPoolError("静态代理必须包含主机、端口、账号和密码")
    display = _clean_name(name, f"{host}:{port}")
    stable = item_id or _stable_id("static", host, port, username)
    return {
        "id": stable,
        "name": display,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "enabled": bool(enabled),
    }


def normalize_static_proxies(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = config.get("proxy_static_proxies")
    if not isinstance(raw_items, list):
        raw_items = []
    result: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, item in enumerate(raw_items, 1):
        if isinstance(item, str):
            parsed = parse_static_proxy(item, name=f"静态代理 {index}")
        elif isinstance(item, dict):
            try:
                parsed = parse_static_proxy(
                    f"{item.get('host', '')}:{item.get('port', '')}:{item.get('username', '')}:{item.get('password', '')}",
                    name=str(item.get("name") or f"静态代理 {index}"),
                    item_id=str(item.get("id") or ""),
                    enabled=bool(item.get("enabled", True)),
                )
            except ProxyPoolError:
                continue
        else:
            continue
        while parsed["id"] in used:
            parsed["id"] = _stable_id("static", parsed["id"], index, len(used))
        used.add(parsed["id"])
        result.append(parsed)
    if not result:
        legacy = str(config.get("proxy") or "").strip()
        if legacy and legacy not in {"http://127.0.0.1:7890", "http://127.0.0.1:7897"}:
            with contextlib.suppress(ProxyPoolError):
                result.append(parse_static_proxy(legacy, name="原固定代理"))
    return result


def _mask(value: str, left: int = 2, right: int = 2) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= left + right:
        return "•" * len(value)
    return value[:left] + "•" * min(10, len(value) - left - right) + value[-right:]


def configuration_payload(config: dict[str, Any]) -> dict[str, Any]:
    subscriptions = [
        {
            "id": item["id"],
            "name": item["name"],
            "enabled": item["enabled"],
            "has_url": bool(item["url"]),
            "url_masked": _mask(item["url"], 12, 6),
        }
        for item in normalize_subscriptions(config)
    ]
    static_proxies = [
        {
            "id": item["id"],
            "name": item["name"],
            "host": item["host"],
            "port": item["port"],
            "username_masked": _mask(item["username"], 4, 3),
            "password_masked": _mask(item["password"], 1, 1),
            "enabled": item["enabled"],
        }
        for item in normalize_static_proxies(config)
    ]
    return {
        "enabled": proxy_is_enabled(config),
        "mode": proxy_mode(config),
        "selection_mode": selection_mode(config),
        "selected_node": str(config.get("proxy_selected_node") or ""),
        "subscriptions": subscriptions,
        "static_proxies": static_proxies,
        "health_url": health_target(config),
        "test_timeout_sec": max(2, int(config.get("proxy_pool_test_timeout_sec", 8) or 8)),
        "max_test_nodes": max(1, int(config.get("proxy_pool_max_test_nodes", 12) or 12)),
        "refresh_seconds": max(300, int(config.get("proxy_pool_refresh_seconds", 3600) or 3600)),
        "mixed_port": max(1, int(config.get("proxy_pool_mixed_port", 17890) or 17890)),
        "controller_port": max(1, int(config.get("proxy_pool_controller_port", 19090) or 19090)),
        "mihomo_path": str(config.get("proxy_pool_mihomo_path") or ""),
        "runtime_dir": str(config.get("proxy_pool_runtime_dir") or "./proxy_pool_runtime"),
    }


def merge_configuration(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Merge sanitized browser rows with secrets already stored on disk."""
    old_subscriptions = {item["id"]: item for item in normalize_subscriptions(config)}
    subscriptions: list[dict[str, Any]] = []
    for index, item in enumerate(payload.get("subscriptions") or [], 1):
        if not isinstance(item, dict):
            continue
        old = old_subscriptions.get(str(item.get("id") or ""), {})
        url = str(item.get("url") or old.get("url") or "").strip()
        name = _clean_name(item.get("name") or old.get("name"), f"订阅 {index}")
        item_id = str(item.get("id") or old.get("id") or _stable_id("sub", name, url, index))
        subscriptions.append({"id": item_id, "name": name, "url": url, "enabled": bool(item.get("enabled", old.get("enabled", True)))})

    old_static = {item["id"]: item for item in normalize_static_proxies(config)}
    static_proxies: list[dict[str, Any]] = []
    for index, item in enumerate(payload.get("static_proxies") or [], 1):
        if not isinstance(item, dict):
            continue
        old = old_static.get(str(item.get("id") or ""), {})
        raw = str(item.get("raw") or "").strip()
        if raw:
            parsed = parse_static_proxy(raw, name=str(item.get("name") or f"静态代理 {index}"), enabled=bool(item.get("enabled", True)))
        elif old:
            parsed = dict(old)
            parsed["name"] = _clean_name(item.get("name"), str(old.get("name") or f"静态代理 {index}"))
            parsed["enabled"] = bool(item.get("enabled", old.get("enabled", True)))
        else:
            continue
        static_proxies.append(parsed)

    mode = str(payload.get("mode") or proxy_mode(config)).strip().lower()
    if mode not in {"clash_subscription", "static_pool"}:
        raise ProxyPoolError("代理类型只能是 Clash 订阅或静态 IP 池")
    values: dict[str, Any] = {
        "proxy_enabled": bool(payload.get("enabled", proxy_is_enabled(config))),
        "proxy_mode": mode,
        "proxy_subscriptions": subscriptions,
        "proxy_static_proxies": static_proxies,
        "proxy_selection_mode": "manual" if str(payload.get("selection_mode") or "random") == "manual" else "random",
        "proxy_selected_node": str(payload.get("selected_node") or "").strip(),
        "proxy_pool_health_url": str(payload.get("health_url") or health_target(config)).strip(),
        "proxy_pool_test_timeout_sec": max(2, min(120, int(payload.get("test_timeout_sec", 8) or 8))),
        "proxy_pool_max_test_nodes": max(1, min(100, int(payload.get("max_test_nodes", 12) or 12))),
        "proxy_pool_refresh_seconds": max(300, int(payload.get("refresh_seconds", 3600) or 3600)),
        "proxy_pool_mixed_port": max(1, min(65535, int(payload.get("mixed_port", 17890) or 17890))),
        "proxy_pool_controller_port": max(1, min(65535, int(payload.get("controller_port", 19090) or 19090))),
        "proxy_pool_mihomo_path": str(payload.get("mihomo_path") or "").strip(),
        "proxy_pool_runtime_dir": str(payload.get("runtime_dir") or "./proxy_pool_runtime").strip(),
    }
    if values["proxy_selection_mode"] == "random":
        values["proxy_selected_node"] = ""
    return values


def _json_request(url: str, *, method: str = "GET", data: Any = None, timeout: float = 5) -> Any:
    body = None if data is None else json.dumps(data).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def _controller_base(config: dict[str, Any]) -> str:
    host = str(config.get("proxy_pool_controller_host") or "127.0.0.1").strip()
    port = max(1, min(65535, int(config.get("proxy_pool_controller_port", 19090) or 19090)))
    return f"http://{host}:{port}"


def _yaml_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _runtime_dir(config: dict[str, Any]) -> Path:
    raw = str(config.get("proxy_pool_runtime_dir") or "./proxy_pool_runtime").strip()
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (ROOT / path).resolve()


def _mihomo_binary(config: dict[str, Any]) -> str:
    configured = str(config.get("proxy_pool_mihomo_path") or "").strip()
    candidates = [configured] if configured else []
    candidates += ["mihomo", "/usr/local/bin/mihomo", str(ROOT / "bin" / "mihomo"), str(ROOT / "bin" / "mihomo.exe")]
    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate) or (candidate if Path(candidate).is_file() else "")
        if resolved:
            return str(resolved)
    raise ProxyPoolError("未找到 Mihomo 内核，请先安装或配置 Mihomo 路径")


def _provider_name(item: dict[str, Any]) -> str:
    return f"provider_{item['id']}"


def _static_node_name(item: dict[str, Any]) -> str:
    return f"[静态IP] {item['name']} · {item['id'][-4:]}"


def build_mihomo_config(config: dict[str, Any]) -> str:
    mixed_port = max(1, min(65535, int(config.get("proxy_pool_mixed_port", 17890) or 17890)))
    controller_host = str(config.get("proxy_pool_controller_host") or "127.0.0.1").strip()
    controller_port = max(1, min(65535, int(config.get("proxy_pool_controller_port", 19090) or 19090)))
    refresh = max(300, int(config.get("proxy_pool_refresh_seconds", 3600) or 3600))
    check_interval = max(30, int(config.get("proxy_pool_health_interval_seconds", 120) or 120))
    target = health_target(config)
    header = f"""mixed-port: {mixed_port}
allow-lan: false
bind-address: 127.0.0.1
mode: rule
log-level: warning
ipv6: false
unified-delay: true
tcp-concurrent: true
external-controller: {_yaml_string(f'{controller_host}:{controller_port}')}
profile:
  store-selected: false
  store-fake-ip: false
"""
    if proxy_mode(config) == "clash_subscription":
        items = [item for item in normalize_subscriptions(config) if item["enabled"] and item["url"]]
        if not items:
            raise ProxyPoolError("请至少启用一个有效的 Clash 订阅")
        providers = ["proxy-providers:"]
        uses: list[str] = []
        for item in items:
            provider = _provider_name(item)
            prefix = f"[{item['name']}] "
            uses.append(f"      - {_yaml_string(provider)}")
            providers.extend(
                [
                    f"  {_yaml_string(provider)}:",
                    "    type: http",
                    f"    url: {_yaml_string(item['url'])}",
                    f"    interval: {refresh}",
                    f"    path: {_yaml_string(f'./providers/{provider}.yaml')}",
                    "    override:",
                    f"      additional-prefix: {_yaml_string(prefix)}",
                    "    health-check:",
                    "      enable: true",
                    f"      url: {_yaml_string(target)}",
                    f"      interval: {check_interval}",
                ]
            )
        body = "\n".join(providers) + "\nproxy-groups:\n  - name: " + _yaml_string(GROUP_NAME) + "\n    type: select\n    use:\n" + "\n".join(uses)
    else:
        items = [item for item in normalize_static_proxies(config) if item["enabled"]]
        if not items:
            raise ProxyPoolError("请至少启用一个带账号密码的静态代理")
        proxies = ["proxies:"]
        names: list[str] = []
        for item in items:
            node_name = _static_node_name(item)
            names.append(f"      - {_yaml_string(node_name)}")
            proxies.extend(
                [
                    f"  - name: {_yaml_string(node_name)}",
                    "    type: http",
                    f"    server: {_yaml_string(item['host'])}",
                    f"    port: {item['port']}",
                    f"    username: {_yaml_string(item['username'])}",
                    f"    password: {_yaml_string(item['password'])}",
                    "    tls: false",
                ]
            )
        body = "\n".join(proxies) + "\nproxy-groups:\n  - name: " + _yaml_string(GROUP_NAME) + "\n    type: select\n    proxies:\n" + "\n".join(names)
    return header + body + f"\nrules:\n  - MATCH,{GROUP_NAME}\n"


class MihomoRuntime:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.selection_lock = threading.RLock()
        self.process: subprocess.Popen[str] | None = None
        self.log_handle: Any = None
        self.config_hash = ""

    def controller_ready(self, config: dict[str, Any]) -> bool:
        try:
            _json_request(_controller_base(config) + "/version", timeout=1.5)
            return True
        except Exception:
            return False

    def controller_reuse_check(self, config: dict[str, Any], digest: str) -> tuple[bool, str]:
        """Check whether another app process already owns our exact runtime.

        The dashboard and each registration task are separate Python processes,
        so their in-memory ``self.process`` / ``config_hash`` values are not
        shared.  A worker may safely adopt the dashboard's Mihomo only when the
        on-disk generated config, mixed port and selector group all match.
        """
        try:
            config_path = _runtime_dir(config) / "config.yaml"
            if not config_path.is_file():
                return False, "runtime_config_missing"
            normalized_text = config_path.read_text(encoding="utf-8").replace("\r\n", "\n")
            disk_digest = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
            if disk_digest != digest:
                return False, "runtime_config_digest_mismatch"
            base = _controller_base(config)
            runtime_config = _json_request(base + "/configs", timeout=3)
            expected_port = max(1, min(65535, int(config.get("proxy_pool_mixed_port", 17890) or 17890)))
            if int(runtime_config.get("mixed-port") or 0) != expected_port:
                return False, f"mixed_port_mismatch:{runtime_config.get('mixed-port')}"
            group = urllib.parse.quote(GROUP_NAME, safe="")
            group_data = _json_request(base + f"/proxies/{group}", timeout=3)
            group_type = str(group_data.get("type") or "").lower()
            if group_type != "selector":
                return False, f"group_type_mismatch:{group_type or 'missing'}"
            return True, "matched"
        except Exception as exc:
            return False, f"controller_probe_failed:{type(exc).__name__}"

    def apply(self, config: dict[str, Any], log: LogFn | None = None, force: bool = False) -> dict[str, Any]:
        log = log or _noop
        if not proxy_is_enabled(config):
            self.stop()
            return self.status(config)
        text = build_mihomo_config(config)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self.lock:
            controller_running = self.controller_ready(config)
            log(
                f"[PX-101] 检查代理内核 mode={proxy_mode(config)} "
                f"controller={_controller_base(config)} running={controller_running} force={force}"
            )
            if not force and controller_running:
                if digest == self.config_hash:
                    log("[PX-102] 当前进程代理内核配置一致，直接复用")
                    return self.status(config)
                if self.process is None:
                    reusable, reason = self.controller_reuse_check(config, digest)
                    if reusable:
                        self.config_hash = digest
                        log("[PX-103] 已复用 Web 服务运行中的代理内核")
                        return self.status(config)
                    log(f"[PX-104] 现有控制器不可复用 reason={reason}")
            runtime = _runtime_dir(config)
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "providers").mkdir(parents=True, exist_ok=True)
            config_path = runtime / "config.yaml"
            config_path.write_text(text, encoding="utf-8")
            with contextlib.suppress(Exception):
                os.chmod(config_path, 0o600)
            self.stop()
            if self.controller_ready(config):
                log("[PX-105] 控制端口仍被不匹配的 Mihomo 占用")
                raise ProxyPoolError("[PX-105] 控制端口被不匹配的 Mihomo 实例占用，请检查代理运行日志")
            binary = _mihomo_binary(config)
            log_path = runtime / "mihomo.log"
            log(f"[PX-106] 启动代理内核 binary={binary} runtime={runtime}")
            self.log_handle = log_path.open("a", encoding="utf-8")
            self.process = subprocess.Popen(
                [binary, "-d", str(runtime), "-f", str(config_path)],
                cwd=str(runtime),
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            deadline = time.time() + 25
            while time.time() < deadline:
                if self.process.poll() is not None:
                    raise ProxyPoolError(f"Mihomo 启动失败，退出码 {self.process.returncode}，查看 {log_path}")
                if self.controller_ready(config):
                    self.config_hash = digest
                    log("[PX-107] 代理池内核启动完成")
                    selected = str(config.get("proxy_selected_node") or "").strip()
                    if selection_mode(config) == "manual" and selected:
                        with contextlib.suppress(Exception):
                            self._select_group(config, selected)
                    return self.status(config)
                time.sleep(0.4)
            raise ProxyPoolError("Mihomo 控制接口启动超时")

    def stop(self) -> None:
        with self.lock:
            process = self.process
            self.process = None
            self.config_hash = ""
            if process and process.poll() is None:
                with contextlib.suppress(Exception):
                    process.terminate()
                    process.wait(timeout=6)
                if process.poll() is None:
                    with contextlib.suppress(Exception):
                        process.kill()
            if self.log_handle:
                with contextlib.suppress(Exception):
                    self.log_handle.close()
                self.log_handle = None

    def refresh(self, config: dict[str, Any]) -> None:
        if proxy_mode(config) != "clash_subscription":
            return
        for item in normalize_subscriptions(config):
            if not item["enabled"] or not item["url"]:
                continue
            provider = urllib.parse.quote(_provider_name(item), safe="")
            _json_request(_controller_base(config) + f"/providers/proxies/{provider}", method="PUT", timeout=20)

    def _subscription_nodes(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in normalize_subscriptions(config):
            if not item["enabled"] or not item["url"]:
                continue
            provider = urllib.parse.quote(_provider_name(item), safe="")
            data = _json_request(_controller_base(config) + f"/providers/proxies/{provider}", timeout=8)
            for node in data.get("proxies") or []:
                if not isinstance(node, dict) or not node.get("name"):
                    continue
                full_name = str(node["name"])
                prefix = f"[{item['name']}] "
                display_name = full_name[len(prefix):] if full_name.startswith(prefix) else full_name
                history = node.get("history") if isinstance(node.get("history"), list) else []
                delay = int((history[-1] or {}).get("delay") or 0) if history else 0
                result.append(
                    {
                        "id": full_name,
                        "name": display_name,
                        "full_name": full_name,
                        "group": item["name"],
                        "group_id": item["id"],
                        "type": str(node.get("type") or "Proxy"),
                        "alive": node.get("alive") is True,
                        "delay": delay,
                    }
                )
        return result

    def _static_nodes(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        data = _json_request(_controller_base(config) + "/proxies", timeout=8)
        proxies = data.get("proxies") if isinstance(data, dict) else {}
        proxies = proxies if isinstance(proxies, dict) else {}
        result: list[dict[str, Any]] = []
        for item in normalize_static_proxies(config):
            if not item["enabled"]:
                continue
            full_name = _static_node_name(item)
            node = proxies.get(full_name) if isinstance(proxies.get(full_name), dict) else {}
            history = node.get("history") if isinstance(node.get("history"), list) else []
            delay = int((history[-1] or {}).get("delay") or 0) if history else 0
            result.append(
                {
                    "id": full_name,
                    "name": item["name"],
                    "full_name": full_name,
                    "group": "静态 IP 代理",
                    "group_id": "static_pool",
                    "type": "HTTP",
                    "alive": node.get("alive") is True if node else None,
                    "delay": delay,
                    "endpoint": f"{item['host']}:{item['port']}",
                    "username_masked": _mask(item["username"], 4, 3),
                }
            )
        return result

    def nodes(self, config: dict[str, Any], ensure_running: bool = True) -> list[dict[str, Any]]:
        if not proxy_is_enabled(config):
            return []
        if ensure_running:
            self.apply(config)
        if not self.controller_ready(config):
            return []
        return self._subscription_nodes(config) if proxy_mode(config) == "clash_subscription" else self._static_nodes(config)

    def _select_group(self, config: dict[str, Any], name: str) -> None:
        group = urllib.parse.quote(GROUP_NAME, safe="")
        _json_request(_controller_base(config) + f"/proxies/{group}", method="PUT", data={"name": name}, timeout=5)

    def current_node(self, config: dict[str, Any]) -> str:
        if not self.controller_ready(config):
            return ""
        try:
            group = urllib.parse.quote(GROUP_NAME, safe="")
            data = _json_request(_controller_base(config) + f"/proxies/{group}", timeout=4)
            return str(data.get("now") or "")
        except Exception:
            return ""

    def test_node(self, config: dict[str, Any], name: str, target: str | None = None) -> dict[str, Any]:
        self.apply(config)
        available = {item["full_name"]: item for item in self.nodes(config, ensure_running=False)}
        if name not in available:
            raise ProxyPoolError("节点不存在或所属分组未启用")
        target = str(target or health_target(config)).strip()
        timeout_ms = max(1000, int(float(config.get("proxy_pool_test_timeout_sec", 8) or 8) * 1000))
        encoded = urllib.parse.quote(name, safe="")
        query = urllib.parse.urlencode({"url": target, "timeout": timeout_ms})
        started = time.monotonic()
        try:
            data = _json_request(_controller_base(config) + f"/proxies/{encoded}/delay?{query}", timeout=(timeout_ms / 1000) + 4)
            delay = int(data.get("delay") or 0)
            if delay <= 0:
                raise ProxyPoolError("未返回有效延迟")
        except Exception as exc:
            raise ProxyPoolError(f"节点无法访问测试网站: {exc}") from exc
        return {
            "ok": True,
            "node": name,
            "display_name": available[name]["name"],
            "group": available[name]["group"],
            "delay": delay or int((time.monotonic() - started) * 1000),
            "target": target,
        }

    def select_node(self, config: dict[str, Any], name: str, target: str | None = None, test: bool = True) -> dict[str, Any]:
        with self.selection_lock:
            result = self.test_node(config, name, target) if test else {"ok": True, "node": name}
            self._select_group(config, name)
            result.update({"selected": True, "proxy": pool_proxy_url(config), "mode": proxy_mode(config)})
            return result

    def status(self, config: dict[str, Any]) -> dict[str, Any]:
        enabled = proxy_is_enabled(config)
        mode = proxy_mode(config)
        base: dict[str, Any] = {
            "ok": True,
            "enabled": enabled,
            "mode": mode,
            "selection_mode": selection_mode(config),
            "selected_node": str(config.get("proxy_selected_node") or ""),
            "effective_proxy": effective_proxy(config),
            "target": health_target(config),
            "running": False,
            "current": "",
            "nodes": [],
            "node_count": 0,
            "alive": 0,
        }
        if not enabled:
            return base
        if not self.controller_ready(config):
            base.update({"ok": False, "error": "代理池内核未运行"})
            return base
        base["running"] = True
        try:
            nodes = self.nodes(config, ensure_running=False)
            base["nodes"] = nodes
            base["node_count"] = len(nodes)
            base["alive"] = sum(1 for item in nodes if item.get("alive") is True)
        except Exception as exc:
            base["error"] = str(exc)
        base["current"] = self.current_node(config)
        return base

    def prepare(self, config: dict[str, Any], target: str | None = None, log: LogFn | None = None) -> dict[str, Any]:
        log = log or _noop
        if not proxy_is_enabled(config):
            return {"ok": True, "mode": "direct", "proxy": "", "message": "代理已关闭，使用直连"}
        with self.selection_lock:
            log(
                f"[PX-200] 准备代理 mode={proxy_mode(config)} "
                f"selection={selection_mode(config)} target={target or health_target(config)}"
            )
            self.apply(config, log=log)
            target = target or health_target(config)
            nodes = self.nodes(config, ensure_running=False)
            log(f"[PX-201] 已读取代理节点 count={len(nodes)}")
            if not nodes:
                raise ProxyPoolError("当前代理类型没有可用节点")
            selected = str(config.get("proxy_selected_node") or "").strip()
            if selection_mode(config) == "manual" and selected:
                log(f"[PX-210] 测试手动固定节点 name={selected}")
                result = self.select_node(config, selected, target=target, test=True)
                log(f"[PX-211] 手动节点可用 name={result.get('display_name') or selected} delay={result.get('delay')}ms")
                return result
            names = [item["full_name"] for item in nodes]
            random.SystemRandom().shuffle(names)
            attempts = max(1, min(len(names), int(config.get("proxy_pool_max_test_nodes", 12) or 12)))
            for index, name in enumerate(names[:attempts], 1):
                try:
                    log(f"[PX-220] 测试随机节点 attempt={index}/{attempts} name={name}")
                    result = self.select_node(config, name, target=target, test=True)
                    log(f"[PX-221] 随机节点可用并已切换 name={result.get('display_name') or name} delay={result.get('delay')}ms")
                    result["tested"] = index
                    return result
                except Exception as exc:
                    log(f"[PX-222] 节点不可用 name={name} reason={exc}")
            raise ProxyPoolError(f"未找到可访问 {target} 的可用节点（已测试 {attempts} 个）")


RUNTIME = MihomoRuntime()


def prepare_proxy(config: dict[str, Any], *, purpose: str = "register", target: str | None = None, log: LogFn | None = None) -> dict[str, Any]:
    return RUNTIME.prepare(config, target=target or health_target(config, purpose), log=log)


def apply_pool_config(config: dict[str, Any], log: LogFn | None = None, force: bool = False) -> dict[str, Any]:
    return RUNTIME.apply(config, log=log, force=force)


def pool_status(config: dict[str, Any]) -> dict[str, Any]:
    return RUNTIME.status(config)


def pool_nodes(config: dict[str, Any], ensure_running: bool = True) -> list[dict[str, Any]]:
    return RUNTIME.nodes(config, ensure_running=ensure_running)


def test_node(config: dict[str, Any], name: str, target: str | None = None) -> dict[str, Any]:
    return RUNTIME.test_node(config, name, target)


def select_node(config: dict[str, Any], name: str, target: str | None = None) -> dict[str, Any]:
    return RUNTIME.select_node(config, name, target)


def refresh_pool(config: dict[str, Any]) -> dict[str, Any]:
    RUNTIME.apply(config)
    RUNTIME.refresh(config)
    return RUNTIME.status(config)


def stop_pool() -> None:
    RUNTIME.stop()
