"""Local macOS-style web dashboard for the Grok registration workspace.

The dashboard intentionally binds to loopback by default.  It reuses the
project's existing text/json files so the Tk GUI and CLI remain compatible.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import proxy_pool


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "webui"
CONFIG_PATH = ROOT / "config.json"
CONFIG_EXAMPLE_PATH = ROOT / "config.example.json"
MAIL_PATH = ROOT / "mail_credentials.txt"
ACCOUNTS_PATH = ROOT / "accounts_cli.txt"
EMAILS_USED_PATH = ROOT / "emails_used.txt"
EMAILS_ERROR_PATH = ROOT / "emails_error.txt"
CPA_DIR = ROOT / "cpa_auths"
CPA_FAIL_PATH = CPA_DIR / "cpa_auth_failed.txt"
BACKFILL_FAIL_PATH = CPA_DIR / "backfill_failed.jsonl"

FILE_LOCK = threading.RLock()
MAX_BODY_BYTES = 12 * 1024 * 1024


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def read_lines(path: Path) -> list[str]:
    return [line for line in read_text(path).splitlines() if line.strip()]


def configured_path(key: str, fallback: Path) -> Path:
    """Resolve a config path the same way as the registration scripts."""
    try:
        value = str(load_json_object(CONFIG_PATH).get(key) or "").strip()
    except Exception:
        value = ""
    if not value:
        return fallback
    path = Path(value).expanduser()
    return path if path.is_absolute() else (ROOT / path).resolve()


def current_mail_path() -> Path:
    return configured_path("hotmail_accounts_file", MAIL_PATH)


def current_cpa_dir() -> Path:
    return configured_path("cpa_auth_dir", CPA_DIR)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".webtmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def stable_id(*parts: str) -> str:
    return hashlib.sha1("\x00".join(parts).encode("utf-8", errors="replace")).hexdigest()[:14]


def mask(value: str, *, left: int = 2, right: int = 3) -> str:
    value = str(value or "")
    if not value:
        return "—"
    if len(value) <= left + right:
        return "•" * len(value)
    return value[:left] + "•" * min(10, len(value) - left - right) + value[-right:]


def mask_email(email: str) -> str:
    if "@" not in email:
        return mask(email)
    local, domain = email.split("@", 1)
    return f"{mask(local, left=2, right=1)}@{domain}"


def public_account(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": stable_id(record["email"]),
        "email": record["email"],
        "password_masked": mask(record.get("password", "")),
        "sso_masked": mask(record.get("sso", ""), left=5, right=5),
        "has_sso": bool(record.get("sso")),
        "has_cpa": bool(record.get("cpa_path")),
        "cpa_file": Path(record["cpa_path"]).name if record.get("cpa_path") else "",
        "source": record.get("source", ""),
        "updated_at": record.get("updated_at", ""),
    }


def parse_mail_text(text: str) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    valid: list[dict[str, str]] = []
    invalid: list[dict[str, Any]] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        parts = line.split("----", 3)
        if len(parts) != 4:
            invalid.append({"line": line_no, "reason": "需要四段数据", "preview": line[:90]})
            continue
        email, password, client_id, token = (part.strip() for part in parts)
        if not email or "@" not in email:
            invalid.append({"line": line_no, "reason": "邮箱格式无效", "preview": mask_email(email)})
            continue
        if not password or not client_id or not token:
            invalid.append({"line": line_no, "reason": "凭证字段不能为空", "preview": mask_email(email)})
            continue
        valid.append(
            {
                "email": email,
                "password": password,
                "client_id": client_id,
                "token": token,
                "raw": "----".join((email, password, client_id, token)),
            }
        )
    return valid, invalid


def tracked_email_counts() -> tuple[dict[str, int], dict[str, int]]:
    used: dict[str, int] = {}
    failed: dict[str, int] = {}
    for path, target in ((EMAILS_USED_PATH, used), (EMAILS_ERROR_PATH, failed)):
        for line in read_lines(path):
            email = line.split("----", 1)[0].strip().lower()
            if not email:
                continue
            target[email] = target.get(email, 0) + 1
    return used, failed


def is_alias_of(candidate: str, main_email: str) -> bool:
    if "@" not in candidate or "@" not in main_email:
        return candidate.lower() == main_email.lower()
    c_local, c_domain = candidate.lower().split("@", 1)
    m_local, m_domain = main_email.lower().split("@", 1)
    return c_domain == m_domain and (c_local == m_local or c_local.startswith(m_local + "+"))


def list_mailboxes() -> list[dict[str, Any]]:
    records, _ = parse_mail_text(read_text(current_mail_path()))
    used, failed = tracked_email_counts()
    out = []
    for record in records:
        email = record["email"]
        used_count = sum(count for item, count in used.items() if is_alias_of(item, email))
        failed_count = sum(count for item, count in failed.items() if is_alias_of(item, email))
        out.append(
            {
                "id": stable_id(email),
                "email": email,
                "password_masked": mask(record["password"]),
                "client_id_masked": mask(record["client_id"], left=4, right=4),
                "token_masked": mask(record["token"], left=5, right=5),
                "used_count": used_count,
                "failed_count": failed_count,
                "status": "attention" if failed_count else ("active" if used_count else "ready"),
            }
        )
    return out


def import_mailboxes(text: str, mode: str = "append") -> dict[str, Any]:
    incoming, invalid = parse_mail_text(text)
    target_path = current_mail_path()
    existing, _ = parse_mail_text(read_text(target_path))
    if mode not in {"append", "replace"}:
        raise ValueError("导入模式必须是 append 或 replace")
    merged: dict[str, dict[str, str]] = {}
    if mode == "append":
        for item in existing:
            merged[item["email"].lower()] = item
    before = len(merged)
    replaced = 0
    for item in incoming:
        key = item["email"].lower()
        if key in merged:
            replaced += 1
        merged[key] = item
    lines = [item["raw"] for item in merged.values()]
    with FILE_LOCK:
        atomic_write(target_path, ("\n".join(lines) + "\n") if lines else "")
    return {
        "ok": True,
        "mode": mode,
        "received": len(incoming),
        "invalid": invalid,
        "added": max(0, len(merged) - before) if mode == "append" else len(merged),
        "updated": replaced,
        "total": len(merged),
    }


def remove_mailboxes(emails: list[str]) -> int:
    wanted = {str(email).strip().lower() for email in emails if str(email).strip()}
    target_path = current_mail_path()
    records, _ = parse_mail_text(read_text(target_path))
    kept = [item for item in records if item["email"].lower() not in wanted]
    removed = len(records) - len(kept)
    with FILE_LOCK:
        atomic_write(target_path, ("\n".join(item["raw"] for item in kept) + "\n") if kept else "")
    return removed


def account_files() -> list[Path]:
    files: list[Path] = []
    if ACCOUNTS_PATH.is_file():
        files.append(ACCOUNTS_PATH)
    for path in sorted(ROOT.glob("accounts_*.txt"), key=lambda p: p.stat().st_mtime if p.exists() else 0):
        if path not in files:
            files.append(path)
    return files


def find_cpa_path(email: str) -> Path | None:
    auth_dir = current_cpa_dir()
    direct = auth_dir / f"xai-{email}.json"
    if direct.is_file():
        return direct
    target = email.lower()
    for path in auth_dir.glob("xai-*.json") if auth_dir.is_dir() else ():
        if path.stem[4:].lower() == target:
            return path
        try:
            data = json.loads(read_text(path))
            if str(data.get("email") or "").strip().lower() == target:
                return path
        except Exception:
            continue
    return None


def discover_accounts() -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for path in account_files():
        mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        for line in read_lines(path):
            if line.startswith("#"):
                continue
            parts = line.split("----", 2)
            if len(parts) < 2:
                continue
            email = parts[0].strip()
            password = parts[1].strip()
            sso = parts[2].strip() if len(parts) > 2 else ""
            if not email:
                continue
            key = email.lower()
            current = merged.get(key)
            record = {
                "email": email,
                "password": password,
                "sso": sso,
                "source": path.name,
                "updated_at": mtime,
            }
            if current is None or (not current.get("sso") and sso) or path == ACCOUNTS_PATH:
                merged[key] = record
    for record in merged.values():
        cpa_path = find_cpa_path(record["email"])
        record["cpa_path"] = str(cpa_path) if cpa_path else ""
    return sorted(merged.values(), key=lambda item: item["email"].lower())


def sync_primary_accounts_ledger() -> int:
    """Materialize all discovered successful accounts into accounts_cli.txt."""
    accounts = discover_accounts()
    lines = ["----".join((item["email"], item["password"], item["sso"])) for item in accounts]
    with FILE_LOCK:
        atomic_write(ACCOUNTS_PATH, ("\n".join(lines) + "\n") if lines else "")
    return len(lines)


def list_failures() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    auth_dir = current_cpa_dir()
    cpa_fail_path = auth_dir / "cpa_auth_failed.txt"
    backfill_fail_path = auth_dir / "backfill_failed.jsonl"
    for index, line in enumerate(read_lines(EMAILS_ERROR_PATH), 1):
        parts = line.split("----", 2)
        email = parts[0].strip() if parts else ""
        if not email:
            continue
        reason = parts[2].strip() if len(parts) > 2 else "未知错误"
        out.append(
            {
                "id": stable_id("registration", str(index), email, reason),
                "email": email,
                "stage": "注册",
                "reason": reason or "未知错误",
                "source": EMAILS_ERROR_PATH.name,
                "retryable": True,
                "time": "",
            }
        )
    for index, line in enumerate(read_lines(cpa_fail_path), 1):
        parts = line.split("----", 2)
        email = parts[0].strip() if parts else ""
        if not email:
            continue
        reason = parts[1].strip() if len(parts) > 1 else "CPA 导出失败"
        timestamp = parts[2].strip() if len(parts) > 2 else ""
        display_time = ""
        try:
            display_time = datetime.fromtimestamp(int(timestamp)).astimezone().isoformat(timespec="seconds")
        except (TypeError, ValueError, OSError):
            display_time = timestamp
        out.append(
            {
                "id": stable_id("cpa", str(index), email, reason, timestamp),
                "email": email,
                "stage": "CPA",
                "reason": reason,
                "source": str(cpa_fail_path.relative_to(ROOT)) if cpa_fail_path.is_relative_to(ROOT) else str(cpa_fail_path),
                "retryable": True,
                "time": display_time,
            }
        )
    for index, line in enumerate(read_lines(backfill_fail_path), 1):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        email = str(data.get("email") or "").strip()
        if not email:
            continue
        reason = str(data.get("error") or data.get("reason") or "补全失败")
        out.append(
            {
                "id": stable_id("backfill", str(index), email, reason),
                "email": email,
                "stage": "补全 CPA",
                "reason": reason,
                "source": str(backfill_fail_path.relative_to(ROOT)) if backfill_fail_path.is_relative_to(ROOT) else str(backfill_fail_path),
                "retryable": True,
                "time": str(data.get("time") or data.get("timestamp") or ""),
            }
        )
    return out


def filter_delimited_file(path: Path, emails: set[str]) -> int:
    original = read_lines(path)
    kept = [line for line in original if line.split("----", 1)[0].strip().lower() not in emails]
    removed = len(original) - len(kept)
    if path.exists() or removed:
        atomic_write(path, ("\n".join(kept) + "\n") if kept else "")
    return removed


def filter_jsonl_file(path: Path, emails: set[str]) -> int:
    original = read_lines(path)
    kept: list[str] = []
    removed = 0
    for line in original:
        try:
            email = str(json.loads(line).get("email") or "").strip().lower()
        except Exception:
            email = ""
        if email and email in emails:
            removed += 1
        else:
            kept.append(line)
    if path.exists() or removed:
        atomic_write(path, ("\n".join(kept) + "\n") if kept else "")
    return removed


def clear_failures(emails: list[str]) -> int:
    wanted = {str(email).strip().lower() for email in emails if str(email).strip()}
    if not wanted:
        return 0
    auth_dir = current_cpa_dir()
    with FILE_LOCK:
        return (
            filter_delimited_file(EMAILS_ERROR_PATH, wanted)
            + filter_delimited_file(auth_dir / "cpa_auth_failed.txt", wanted)
            + filter_jsonl_file(auth_dir / "backfill_failed.jsonl", wanted)
        )


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(read_text(path))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def is_comment_key(key: str) -> bool:
    return key.startswith("//") or key.startswith("#")


def config_group(key: str) -> str:
    if key.startswith(("hotmail_", "cloudmail_", "cloudflare_", "duckmail_", "yyds_")) or key in {
        "email_provider",
        "defaultDomains",
    }:
        return "邮箱服务"
    if key.startswith("grok2api_"):
        return "Grok2API"
    if key.startswith("cpa_") or key == "api_reverse_tools":
        return "CPA / OIDC"
    if key.startswith("register_") or key in {"thread_start_interval", "account_hard_timeout"}:
        return "注册任务"
    if key in {"proxy", "user_agent", "enable_nsfw"} or key.startswith("proxy_") or "timeout" in key or key.startswith("turnstile_") or key.startswith("mail_"):
        return "浏览器与网络"
    if key.startswith("show_"):
        return "界面"
    return "其他"


def config_description(template: dict[str, Any], key: str) -> str:
    direct_prefixes = (f"// {key}", f"//{key}")
    for comment_key, value in template.items():
        if any(comment_key.startswith(prefix) for prefix in direct_prefixes):
            return str(value)
    return ""


def friendly_label(key: str) -> str:
    labels = {
        "email_provider": "邮箱服务商",
        "defaultDomains": "默认邮箱域名",
        "proxy": "注册代理",
        "user_agent": "浏览器 User-Agent",
        "register_count": "默认注册数量",
        "register_threads": "注册并发数",
        "cpa_export_enabled": "注册后生成 CPA 凭证",
        "cpa_auth_dir": "CPA 凭证目录",
        "cpa_proxy": "CPA 专用代理",
        "cpa_prefer_protocol": "优先使用协议模式",
        "cpa_protocol_only": "仅使用协议模式",
        "grok2api_auto_add_remote": "自动推送到远端 Grok2API",
        "grok2api_remote_base": "Grok2API 管理地址",
        "grok2api_remote_app_key": "Grok2API App Key",
        "hotmail_accounts_file": "Hotmail 凭证文件",
        "hotmail_max_aliases_per_account": "单邮箱最大别名数",
    }
    if key in labels:
        return labels[key]
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower().split("_")
    translations = {
        "hotmail": "Hotmail",
        "cloudmail": "CloudMail",
        "cloudflare": "Cloudflare",
        "duckmail": "DuckMail",
        "yyds": "YYDS",
        "grok2api": "Grok2API",
        "cpa": "CPA",
        "sso": "SSO",
        "imap": "IMAP",
        "api": "API",
        "jwt": "JWT",
        "url": "地址",
        "dir": "目录",
        "file": "文件",
        "path": "路径",
        "provider": "服务商",
        "default": "默认",
        "domains": "域名",
        "domain": "域名",
        "accounts": "账户",
        "account": "账户",
        "email": "邮箱",
        "admin": "管理员",
        "password": "密码",
        "token": "Token",
        "key": "密钥",
        "auth": "认证",
        "mode": "模式",
        "alias": "别名",
        "random": "随机",
        "sequential": "顺序",
        "length": "长度",
        "max": "最大",
        "attempts": "尝试次数",
        "poll": "轮询",
        "interval": "间隔",
        "recent": "最近",
        "seconds": "秒数",
        "hosts": "主机列表",
        "last": "最近",
        "require": "必须",
        "recipient": "收件人",
        "match": "匹配",
        "base": "基础地址",
        "retry": "重试",
        "count": "数量",
        "messages": "邮件列表",
        "proxy": "代理",
        "enable": "启用",
        "nsfw": "NSFW",
        "nav": "导航",
        "button": "按钮",
        "form": "表单",
        "timeout": "超时",
        "mail": "邮件",
        "code": "验证码",
        "profile": "资料",
        "turnstile": "Turnstile",
        "stuck": "卡住",
        "cookie": "Cookie",
        "read": "读取",
        "submit": "提交",
        "confirm": "确认",
        "progress": "进度",
        "extension": "延长",
        "register": "注册",
        "threads": "线程数",
        "thread": "线程",
        "start": "启动",
        "hard": "强制",
        "show": "显示",
        "tutorial": "教程",
        "on": "于",
        "auto": "自动",
        "add": "添加",
        "local": "本地",
        "remote": "远端",
        "pool": "池",
        "name": "名称",
        "import": "导入",
        "delay": "延迟",
        "reverse": "反向工具",
        "tools": "工具目录",
        "export": "导出",
        "enabled": "已启用",
        "copy": "复制",
        "to": "到",
        "hotload": "热加载",
        "headless": "无头模式",
        "force": "强制",
        "standalone": "独立浏览器",
        "mint": "凭证生成",
        "after": "之后",
        "write": "写入",
        "probe": "探测",
        "chat": "对话",
        "workers": "工作线程",
        "queue": "队列",
        "prefer": "优先",
        "protocol": "协议",
        "only": "仅限",
        "inject": "注入",
        "gui": "GUI",
        "close": "关闭",
        "browser": "浏览器",
        "reuse": "复用",
        "recycle": "回收",
        "every": "周期",
    }
    return " ".join(translations.get(word, word.upper() if len(word) <= 3 else word) for word in words)


def config_payload() -> dict[str, Any]:
    current = load_json_object(CONFIG_PATH)
    template = load_json_object(CONFIG_EXAMPLE_PATH)
    keys: list[str] = []
    for source in (template, current):
        for key in source:
            if not is_comment_key(key) and key not in keys:
                keys.append(key)
    fields = []
    for key in keys:
        value = current.get(key, template.get(key))
        field_type = "boolean" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else "string"
        lowered = key.lower()
        secret = any(word in lowered for word in ("password", "token", "app_key", "api_key", "jwt", "secret", "subscription"))
        fields.append(
            {
                "key": key,
                "label": friendly_label(key),
                "value": value,
                "type": field_type,
                "secret": secret,
                "group": config_group(key),
                "description": config_description(template, key),
            }
        )
    return {"fields": fields, "path": str(CONFIG_PATH), "exists": CONFIG_PATH.exists()}


def save_config(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("配置内容必须是对象")
    current = load_json_object(CONFIG_PATH)
    template = load_json_object(CONFIG_EXAMPLE_PATH)
    allowed = {key for source in (template, current) for key in source if not is_comment_key(key)}
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError("包含未知配置项: " + ", ".join(unknown[:5]))
    base = current if current else dict(template)
    for key, value in values.items():
        sample = current.get(key, template.get(key))
        if isinstance(sample, bool) and not isinstance(value, bool):
            raise ValueError(f"{key} 必须是布尔值")
        if isinstance(sample, int) and not isinstance(sample, bool):
            if not isinstance(value, (int, float)):
                raise ValueError(f"{key} 必须是数字")
            value = int(value)
        elif isinstance(sample, float) and not isinstance(value, (int, float)):
            raise ValueError(f"{key} 必须是数字")
        base[key] = value
    with FILE_LOCK:
        if CONFIG_PATH.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = ROOT / f"config.json.bak-web-{stamp}"
            backup.write_text(read_text(CONFIG_PATH), encoding="utf-8")
        atomic_write(CONFIG_PATH, json.dumps(base, ensure_ascii=False, indent=2) + "\n")
    return {"ok": True, "saved": len(values), "path": str(CONFIG_PATH)}


def save_proxy_configuration(payload: dict[str, Any]) -> dict[str, Any]:
    current = load_json_object(CONFIG_PATH)
    values = proxy_pool.merge_configuration(current, payload)
    result = save_config(values)
    cfg = load_json_object(CONFIG_PATH)
    result["configuration"] = proxy_pool.configuration_payload(cfg)
    if proxy_pool.proxy_is_enabled(cfg):
        try:
            result["proxy_pool"] = proxy_pool.apply_pool_config(cfg, force=True)
        except Exception as exc:  # The settings remain saved so the UI can show/fix them.
            result["proxy_pool_warning"] = str(exc)
    else:
        proxy_pool.stop_pool()
        result["proxy_pool"] = proxy_pool.pool_status(cfg)
    return result


class TaskManager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.process: subprocess.Popen[str] | None = None
        self.reader: threading.Thread | None = None
        self.kind = ""
        self.status = "idle"
        self.started_at = ""
        self.ended_at = ""
        self.returncode: int | None = None
        self.command: list[str] = []
        self.logs: deque[dict[str, str]] = deque(maxlen=1500)

    def append(self, message: str, level: str = "info") -> None:
        clean = message.rstrip("\r\n")
        if not clean:
            return
        lowered = clean.lower()
        if any(marker in lowered for marker in ("失败", "error", "traceback", "exception")):
            level = "error"
        elif any(marker in lowered for marker in ("成功", "success", "完成")):
            level = "success"
        with self.lock:
            self.logs.append({"time": time.strftime("%H:%M:%S"), "message": clean, "level": level})

    def start(self, kind: str, options: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.process and self.process.poll() is None:
                raise RuntimeError("已有任务正在运行")
            synced: int | None = None
            if kind in {"register", "backfill"}:
                synced = sync_primary_accounts_ledger()
            command = self._build_command(kind, options)
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONUNBUFFERED"] = "1"
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            self.logs.clear()
            self.kind = kind
            self.status = "running"
            self.started_at = now_iso()
            self.ended_at = ""
            self.returncode = None
            self.command = command
            self.process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=creationflags,
            )
            if synced is not None:
                self.append(f"已同步主账本：{synced} 个成功账户")
            self.append(f"启动任务：{kind}")
            self.reader = threading.Thread(target=self._read_output, daemon=True, name="web-task-output")
            self.reader.start()
            return self.snapshot()

    def _build_command(self, kind: str, options: dict[str, Any]) -> list[str]:
        python = sys.executable
        if kind == "register":
            extra = max(1, min(100, int(options.get("extra", 1))))
            threads = max(1, min(10, int(options.get("threads", 1))))
            cfg = load_json_object(CONFIG_PATH)
            if proxy_pool.proxy_is_enabled(cfg) and proxy_pool.selection_mode(cfg) == "random":
                threads = 1
            command = [
                python,
                "-u",
                str(ROOT / "register_cli.py"),
                "--extra",
                str(extra),
                "--threads",
                str(threads),
                "--accounts-file",
                str(ACCOUNTS_PATH),
            ]
            mint_workers = options.get("mint_workers")
            if mint_workers not in (None, ""):
                command.extend(("--mint-workers", str(max(-1, min(10, int(mint_workers))))))
            return command
        if kind == "backfill":
            limit = max(0, min(10000, int(options.get("limit", 0))))
            sleep_seconds = max(0.0, min(120.0, float(options.get("sleep", 3))))
            timeout = max(30.0, min(1800.0, float(options.get("timeout", 300))))
            command = [
                python,
                "-u",
                str(ROOT / "scripts" / "backfill_cpa_xai_from_accounts.py"),
                "--accounts",
                str(ACCOUNTS_PATH),
                "--limit",
                str(limit),
                "--sleep",
                str(sleep_seconds),
                "--timeout",
                str(timeout),
            ]
            if bool(options.get("probe", True)):
                command.append("--probe")
            email = str(options.get("email") or "").strip()
            if email:
                command.extend(("--email", email))
            return command
        if kind == "check":
            return [python, "-u", str(ROOT / "optimization_checks.py")]
        raise ValueError("未知任务类型")

    def _read_output(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.stdout:
                for line in process.stdout:
                    self.append(line)
            code = process.wait()
        except Exception as exc:  # noqa: BLE001
            self.append(f"任务输出读取失败: {exc}", "error")
            code = process.poll() if process else -1
        with self.lock:
            self.returncode = code
            if self.status == "stopping":
                self.status = "stopped"
            else:
                self.status = "completed" if code == 0 else "failed"
            self.ended_at = now_iso()
            self.append(f"任务结束，退出码 {code}", "success" if code == 0 else "error")

    def stop(self) -> dict[str, Any]:
        with self.lock:
            process = self.process
            if not process or process.poll() is not None:
                return self.snapshot()
            self.status = "stopping"
            self.append("正在停止任务…", "warning")
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
            except Exception:
                process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            running = bool(self.process and self.process.poll() is None)
            return {
                "kind": self.kind,
                "status": self.status,
                "running": running,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "returncode": self.returncode,
                "command": self.command,
                "logs": list(self.logs),
            }


TASKS = TaskManager()


def overview_payload() -> dict[str, Any]:
    mailboxes = list_mailboxes()
    accounts = discover_accounts()
    failures = list_failures()
    config = load_json_object(CONFIG_PATH)
    return {
        "mailboxes": len(mailboxes),
        "mailboxes_ready": sum(1 for item in mailboxes if item["status"] == "ready"),
        "accounts": len(accounts),
        "accounts_with_sso": sum(1 for item in accounts if item.get("sso")),
        "accounts_with_cpa": sum(1 for item in accounts if item.get("cpa_path")),
        "failures": len(failures),
        "provider": config.get("email_provider", "未配置"),
        "proxy_enabled": proxy_pool.proxy_is_enabled(config),
        "proxy_mode": proxy_pool.proxy_mode(config),
        "effective_proxy": proxy_pool.effective_proxy(config),
        "cpa_enabled": bool(config.get("cpa_export_enabled", True)),
        "task": {key: value for key, value in TASKS.snapshot().items() if key != "logs"},
        "time": now_iso(),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "GrokDashboard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    def send_common_headers(self, content_type: str, length: int | None = None) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if length is not None:
            self.send_header("Content-Length", str(length))

    def json_response(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_common_headers("application/json; charset=utf-8", len(payload))
        self.end_headers()
        self.wfile.write(payload)

    def error_response(self, message: str, status: int = 400) -> None:
        self.json_response({"ok": False, "error": message}, status)

    @staticmethod
    def session_token() -> str:
        password = os.environ.get("WEB_DASHBOARD_PASSWORD", "")
        return hmac.new(password.encode("utf-8"), b"grok-account-studio-session-v1", hashlib.sha256).hexdigest()

    def authorized(self) -> bool:
        password = os.environ.get("WEB_DASHBOARD_PASSWORD", "")
        if not password:
            return True
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            supplied = cookie.get("grok_studio_session")
            value = supplied.value if supplied else ""
        except Exception:
            return False
        return hmac.compare_digest(value, self.session_token())

    def require_authorization(self) -> bool:
        if self.authorized():
            return True
        if self.path.startswith("/api/"):
            self.error_response("登录状态已失效", HTTPStatus.UNAUTHORIZED)
            return False
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/login")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return False

    def login_page(self, error: str = "", status: int = 200) -> None:
        error_html = f'<div class="error">{error}</div>' if error else ""
        page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · Grok Account Studio</title><style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at 20% 10%,#f9fbff,#e9edf4 58%,#e1e6ef);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;color:#172033}}
.card{{width:min(410px,calc(100vw - 32px));padding:34px;background:rgba(255,255,255,.88);border:1px solid rgba(255,255,255,.9);border-radius:24px;box-shadow:0 30px 90px rgba(38,52,82,.2);backdrop-filter:blur(24px)}}
.dots{{display:flex;gap:8px;margin-bottom:30px}}.dots i{{width:11px;height:11px;border-radius:50%}}.dots i:nth-child(1){{background:#ff5f57}}.dots i:nth-child(2){{background:#febc2e}}.dots i:nth-child(3){{background:#28c840}}
.mark{{width:54px;height:54px;display:grid;place-items:center;border-radius:17px;background:linear-gradient(145deg,#171b26,#3c4355);color:white;font-size:24px;font-weight:800;box-shadow:0 10px 24px rgba(24,29,40,.22)}}
h1{{margin:20px 0 7px;font-size:24px;letter-spacing:-.6px}}p{{margin:0 0 24px;color:#7a8498;font-size:13px}}label{{display:block;font-size:11px;color:#7a8498;margin-bottom:7px}}input{{width:100%;height:46px;border:1px solid rgba(27,36,56,.11);border-radius:12px;background:#f5f7fa;padding:0 13px;font-size:15px;outline:0}}input:focus{{border-color:#2878ff;box-shadow:0 0 0 3px rgba(40,120,255,.1);background:#fff}}button{{width:100%;height:46px;margin-top:14px;border:0;border-radius:12px;background:linear-gradient(180deg,#3987ff,#216fe9);color:#fff;font-weight:700;cursor:pointer;box-shadow:0 9px 22px rgba(40,120,255,.24)}}
.error{{margin:0 0 13px;padding:10px 12px;border-radius:10px;color:#c44343;background:#fff0f0;font-size:12px}}.foot{{margin-top:18px;text-align:center;color:#9aa3b3;font-size:10px}}
</style></head><body><form class="card" method="post" action="/login"><div class="dots"><i></i><i></i><i></i></div><div class="mark">G</div><h1>Grok Account Studio</h1><p>输入管理密码以访问控制台</p>{error_html}<label for="password">访问密码</label><input id="password" name="password" type="password" autocomplete="current-password" autofocus required><button type="submit">登录控制台</button><div class="foot">Secure local credential management</div></form></body></html>"""
        data = page.encode("utf-8")
        self.send_response(status)
        self.send_common_headers("text/html; charset=utf-8", len(data))
        self.end_headers()
        self.wfile.write(data)

    def handle_login(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 8192:
            return self.login_page("请求内容无效", HTTPStatus.BAD_REQUEST)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        supplied = str((form.get("password") or [""])[0])
        expected = os.environ.get("WEB_DASHBOARD_PASSWORD", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            return self.login_page("密码不正确，请重新输入", HTTPStatus.UNAUTHORIZED)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"grok_studio_session={self.session_token()}; Path=/; Max-Age=604800; HttpOnly; SameSite=Strict",
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def handle_logout(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", "grok_studio_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("请求内容过大")
        if not length:
            return {}
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求 JSON 必须是对象")
        return data

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            return self.json_response({"ok": True, "service": "grok-account-studio"})
        if path == "/login":
            if self.authorized():
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/")
                self.end_headers()
                return
            return self.login_page()
        if path == "/logout":
            return self.handle_logout()
        if not self.require_authorization():
            return
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/overview":
                return self.json_response(overview_payload())
            if path == "/api/mailboxes":
                mail_path = current_mail_path()
                records, invalid = parse_mail_text(read_text(mail_path))
                return self.json_response({"items": list_mailboxes(), "invalid_count": len(invalid), "total": len(records), "path": str(mail_path)})
            if path == "/api/accounts":
                items = [public_account(item) for item in discover_accounts()]
                return self.json_response({"items": items, "total": len(items)})
            if path == "/api/account/credential":
                email = str((query.get("email") or [""])[0]).strip().lower()
                account = next((item for item in discover_accounts() if item["email"].lower() == email), None)
                if not account:
                    return self.error_response("未找到该账户", 404)
                return self.json_response(
                    {"email": account["email"], "password": account["password"], "sso": account["sso"]}
                )
            if path == "/api/failures":
                items = list_failures()
                return self.json_response({"items": items, "total": len(items)})
            if path == "/api/config":
                return self.json_response(config_payload())
            if path == "/api/proxy-pool/status":
                return self.json_response(proxy_pool.pool_status(load_json_object(CONFIG_PATH)))
            if path == "/api/proxy-pool/config":
                return self.json_response(proxy_pool.configuration_payload(load_json_object(CONFIG_PATH)))
            if path == "/api/task":
                return self.json_response(TASKS.snapshot())
            if path == "/api/export/accounts":
                return self.download_accounts()
            if path == "/api/export/failures":
                return self.download_failures()
            if path == "/api/export/cpa":
                return self.download_cpa_zip()
            return self.serve_static(path)
        except Exception as exc:  # noqa: BLE001
            return self.error_response(str(exc), 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            return self.handle_login()
        if not self.require_authorization():
            return
        try:
            body = self.read_json()
            if path == "/api/mailboxes/import":
                return self.json_response(import_mailboxes(str(body.get("text") or ""), str(body.get("mode") or "append")))
            if path == "/api/mailboxes/delete":
                removed = remove_mailboxes(list(body.get("emails") or []))
                return self.json_response({"ok": True, "removed": removed})
            if path == "/api/failures/retry":
                removed = clear_failures(list(body.get("emails") or []))
                return self.json_response({"ok": True, "removed": removed})
            if path == "/api/config":
                result = save_config(body.get("values") or {})
                cfg = load_json_object(CONFIG_PATH)
                if proxy_pool.proxy_is_enabled(cfg):
                    try:
                        result["proxy_pool"] = proxy_pool.apply_pool_config(cfg, force=True)
                    except Exception as exc:  # config is still saved; report runtime issue separately
                        result["proxy_pool_warning"] = str(exc)
                else:
                    proxy_pool.stop_pool()
                return self.json_response(result)
            if path == "/api/proxy-pool/config":
                return self.json_response(save_proxy_configuration(body))
            if path == "/api/proxy-pool/apply":
                cfg = load_json_object(CONFIG_PATH)
                return self.json_response(proxy_pool.apply_pool_config(cfg, force=True))
            if path == "/api/proxy-pool/refresh":
                cfg = load_json_object(CONFIG_PATH)
                return self.json_response(proxy_pool.refresh_pool(cfg))
            if path == "/api/proxy-pool/rotate":
                cfg = load_json_object(CONFIG_PATH)
                target = str(body.get("target") or proxy_pool.health_target(cfg)).strip()
                return self.json_response(proxy_pool.prepare_proxy(cfg, target=target))
            if path == "/api/proxy-pool/node/test":
                cfg = load_json_object(CONFIG_PATH)
                name = str(body.get("name") or "").strip()
                if not name:
                    raise ValueError("请选择要测试的代理节点")
                target = str(body.get("target") or proxy_pool.health_target(cfg)).strip()
                return self.json_response(proxy_pool.test_node(cfg, name, target))
            if path == "/api/proxy-pool/node/select":
                cfg = load_json_object(CONFIG_PATH)
                name = str(body.get("name") or "").strip()
                if not name:
                    raise ValueError("请选择代理节点")
                target = str(body.get("target") or proxy_pool.health_target(cfg)).strip()
                result = proxy_pool.select_node(cfg, name, target)
                save_config({"proxy_selection_mode": "manual", "proxy_selected_node": name})
                result["selection_mode"] = "manual"
                return self.json_response(result)
            if path == "/api/proxy-pool/random":
                cfg = load_json_object(CONFIG_PATH)
                enabled = bool(body.get("enabled", True))
                if enabled:
                    save_config({"proxy_selection_mode": "random", "proxy_selected_node": ""})
                else:
                    selected = str(body.get("selected_node") or proxy_pool.pool_status(cfg).get("current") or "").strip()
                    save_config({"proxy_selection_mode": "manual", "proxy_selected_node": selected})
                updated = load_json_object(CONFIG_PATH)
                return self.json_response(proxy_pool.pool_status(updated))
            if path == "/api/task/start":
                return self.json_response(TASKS.start(str(body.get("kind") or ""), body.get("options") or {}))
            if path == "/api/task/stop":
                return self.json_response(TASKS.stop())
            return self.error_response("接口不存在", 404)
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            return self.error_response(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            return self.error_response(str(exc), 500)

    def serve_static(self, request_path: str) -> None:
        name = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        safe = (WEB_ROOT / name).resolve()
        if WEB_ROOT.resolve() not in safe.parents and safe != WEB_ROOT.resolve():
            return self.error_response("路径无效", 403)
        if not safe.is_file():
            safe = WEB_ROOT / "index.html"
        data = safe.read_bytes()
        content_type = mimetypes.guess_type(safe.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_common_headers(content_type, len(data))
        self.end_headers()
        self.wfile.write(data)

    def send_download(self, data: bytes, filename: str, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_common_headers(content_type, len(data))
        quoted = urllib.parse.quote(filename)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
        self.end_headers()
        self.wfile.write(data)

    def download_accounts(self) -> None:
        lines = ["----".join((item["email"], item["password"], item["sso"])) for item in discover_accounts()]
        data = (("\n".join(lines) + "\n") if lines else "").encode("utf-8")
        self.send_download(data, f"成功账户_{datetime.now():%Y%m%d_%H%M%S}.txt", "text/plain; charset=utf-8")

    def download_failures(self) -> None:
        rows = ["邮箱\t阶段\t原因\t来源\t时间"]
        for item in list_failures():
            rows.append("\t".join(str(item.get(key, "")).replace("\t", " ") for key in ("email", "stage", "reason", "source", "time")))
        self.send_download(("\ufeff" + "\n".join(rows) + "\n").encode("utf-8"), f"失败记录_{datetime.now():%Y%m%d_%H%M%S}.tsv", "text/tab-separated-values; charset=utf-8")

    def download_cpa_zip(self) -> None:
        buffer = io.BytesIO()
        auth_dir = current_cpa_dir()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(auth_dir.glob("xai-*.json")) if auth_dir.is_dir() else ():
                archive.write(path, arcname=path.name)
        self.send_download(buffer.getvalue(), f"CPA凭证_{datetime.now():%Y%m%d_%H%M%S}.zip", "application/zip")


def main() -> int:
    parser = argparse.ArgumentParser(description="Local web dashboard for grok_reg-protocol_cpa")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if not WEB_ROOT.is_dir():
        print(f"Web assets missing: {WEB_ROOT}", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    server.quiet = args.quiet  # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}/"
    print(f"Grok Account Studio running at {url}", flush=True)
    if os.environ.get("WEB_DASHBOARD_PASSWORD"):
        print("Password login enabled", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    cfg = load_json_object(CONFIG_PATH)
    if proxy_pool.proxy_is_enabled(cfg):
        try:
            status = proxy_pool.apply_pool_config(cfg)
            print(f"Proxy pool ready: {status.get('node_count', 0)} nodes", flush=True)
        except Exception as exc:
            print(f"Proxy pool startup warning: {exc}", flush=True)
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\nStopping dashboard…", flush=True)
    finally:
        try:
            TASKS.stop()
        except Exception:
            pass
        proxy_pool.stop_pool()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
