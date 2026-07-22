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
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import proxy_pool
from cpa_xai.model_capabilities import (
    delete_model_capability,
    read_model_capability,
    record_model_list,
    record_model_test,
)
from cpa_xai.probe import probe_mini_response, probe_models
from cpa_xai.refresh import refresh_cpa_auth
from cpa_xai.schema import DEFAULT_BASE_URL


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "webui"
CONFIG_PATH = ROOT / "config.json"
CONFIG_EXAMPLE_PATH = ROOT / "config.example.json"
MAIL_PATH = ROOT / "mail_credentials.txt"
ACCOUNTS_PATH = ROOT / "accounts_cli.txt"
EMAILS_USED_PATH = ROOT / "emails_used.txt"
EMAILS_ERROR_PATH = ROOT / "emails_error.txt"
HOTMAIL_INVALID_PATH = ROOT / "hotmail_invalid.txt"
CPA_DIR = ROOT / "cpa_auths"
CPA_FAIL_PATH = CPA_DIR / "cpa_auth_failed.txt"
BACKFILL_FAIL_PATH = CPA_DIR / "backfill_failed.jsonl"
CPA_MANAGEMENT_URL = "http://127.0.0.1:8317"
CPA_PUSH_STATUS_FILE = ".cpa_push_status.json"
DRISSION_PROFILE_ROOT = Path("/tmp/DrissionPage/autoPortData")

FILE_LOCK = threading.RLock()
MAX_BODY_BYTES = 12 * 1024 * 1024


class FullCredentialRefreshRequired(ValueError):
    """The refresh token failed and the account needs a full OIDC mint."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def project_browser_pids() -> list[int]:
    if os.name == "nt" or not Path("/proc").is_dir():
        return []
    marker = str(DRISSION_PROFILE_ROOT) + "/"
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="ignore"
            )
            process_name = (entry / "comm").read_text(
                encoding="utf-8", errors="ignore"
            ).strip().lower()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        if marker in command and ("chrome" in process_name or "chromium" in process_name):
            pids.append(int(entry.name))
    return pids


def cleanup_project_browsers(grace_seconds: float = 2.0) -> int:
    pids = project_browser_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    if pids:
        deadline = time.time() + max(0.0, grace_seconds)
        while time.time() < deadline and project_browser_pids():
            time.sleep(0.1)
        for pid in project_browser_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    if os.name != "nt" and DRISSION_PROFILE_ROOT.is_dir():
        try:
            resolved = DRISSION_PROFILE_ROOT.resolve()
            if resolved == Path("/tmp/DrissionPage/autoPortData"):
                for child in list(resolved.iterdir()):
                    try:
                        if child.is_dir() and not child.is_symlink():
                            shutil.rmtree(child)
                        else:
                            child.unlink(missing_ok=True)
                    except OSError:
                        pass
        except OSError:
            pass
    return len(pids)


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


def cpa_management_base_url() -> str:
    config = load_json_object(CONFIG_PATH)
    url = str(
        config.get("cpa_management_url")
        or os.environ.get("CPA_MANAGEMENT_URL")
        or CPA_MANAGEMENT_URL
    ).strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("CPA 管理地址无效，请配置完整的 http/https 地址")
    if parsed.username or parsed.password:
        raise ValueError("CPA 管理地址不能包含账户或密码")
    return url.rstrip("/")


def cpa_management_settings() -> tuple[str, str]:
    config = load_json_object(CONFIG_PATH)
    url = cpa_management_base_url()
    key = str(
        config.get("cpa_management_key")
        or os.environ.get("CPA_MANAGEMENT_KEY")
        or ""
    ).strip()
    if not key:
        raise ValueError("CPA 管理密码/密钥未配置")
    return url, key


def cpa_auth_files_endpoint(base_url: str) -> str:
    suffix = "/v0/management"
    return base_url + "/auth-files" if base_url.endswith(suffix) else base_url + suffix + "/auth-files"


def cpa_management_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            payload = json.loads(exc.read(16 * 1024).decode("utf-8", errors="replace"))
            message = str(payload.get("error") or "").strip() if isinstance(payload, dict) else ""
        except Exception:
            message = ""
        return f"HTTP {exc.code}{f'：{message}' if message else ''}"
    if isinstance(exc, urllib.error.URLError):
        return f"连接失败：{exc.reason}"
    return str(exc)


def cpa_push_status_path(auth_dir: Path | None = None) -> Path:
    return (auth_dir or current_cpa_dir()).resolve() / CPA_PUSH_STATUS_FILE


def read_cpa_push_status(auth_dir: Path | None = None) -> dict[str, Any]:
    path = cpa_push_status_path(auth_dir)
    try:
        payload = json.loads(read_text(path))
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    files = payload.get("files")
    return {"version": 1, "files": files if isinstance(files, dict) else {}}


def write_cpa_push_status(auth_dir: Path, payload: dict[str, Any]) -> None:
    with FILE_LOCK:
        atomic_write(
            cpa_push_status_path(auth_dir),
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )


def cpa_file_push_state(
    path: Path,
    *,
    registry: dict[str, Any] | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    registry = registry or read_cpa_push_status(path.parent)
    item = (registry.get("files") or {}).get(path.name)
    if not isinstance(item, dict) or not path.is_file():
        return {"pushed": False, "pushed_at": "", "target": ""}
    try:
        fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return {"pushed": False, "pushed_at": "", "target": ""}
    current_target = (target or cpa_management_base_url()).rstrip("/")
    pushed = (
        str(item.get("sha256") or "") == fingerprint
        and str(item.get("target") or "").rstrip("/") == current_target
    )
    return {
        "pushed": pushed,
        "pushed_at": str(item.get("pushed_at") or "") if pushed else "",
        "target": urllib.parse.urlparse(current_target).netloc if pushed else "",
    }


def delete_cpa_push_status(auth_dir: Path, filenames: set[str]) -> int:
    if not filenames:
        return 0
    payload = read_cpa_push_status(auth_dir)
    files = payload["files"]
    lowered = {name.lower() for name in filenames}
    removed = 0
    for name in list(files):
        if name.lower() in lowered:
            files.pop(name, None)
            removed += 1
    if removed:
        write_cpa_push_status(auth_dir, payload)
    return removed


def push_cpa_auths_to_management(
    *,
    auth_dir: Path | None = None,
    management_url: str | None = None,
    management_key: str | None = None,
    emails: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    if management_url is None or management_key is None:
        configured_url, configured_key = cpa_management_settings()
        management_url = management_url or configured_url
        management_key = management_key or configured_key
    base_url = str(management_url).rstrip("/")
    endpoint = cpa_auth_files_endpoint(base_url)
    source_dir = (auth_dir or current_cpa_dir()).resolve()
    if emails:
        wanted = {str(email).strip().lower() for email in emails if str(email).strip()}
        paths = []
        for email in sorted(wanted):
            path = find_cpa_path_in_dir(source_dir, email)
            if path and path.resolve().parent == source_dir:
                paths.append(path.resolve())
        paths = sorted(set(paths))
    else:
        paths = sorted(source_dir.glob("xai-*.json")) if source_dir.is_dir() else []
    if not paths:
        raise ValueError("当前没有可推送的 CPA 凭证")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "grok-account-studio/1.0",
        "X-Management-Key": str(management_key),
    }
    registry = read_cpa_push_status(source_dir)
    pushed: list[str] = []
    fingerprints: dict[str, str] = {}
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    for path in paths:
        try:
            data = path.read_bytes()
            payload = json.loads(data.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("凭证 JSON 必须是对象")
            fingerprint = hashlib.sha256(data).hexdigest()
            previous = (registry.get("files") or {}).get(path.name)
            already_pushed = isinstance(previous, dict) and (
                str(previous.get("sha256") or "") == fingerprint
                and str(previous.get("target") or "").rstrip("/") == base_url
            )
            if already_pushed and not force:
                skipped.append(path.name)
                continue
            upload_url = endpoint + "?" + urllib.parse.urlencode({"name": path.name})
            request = urllib.request.Request(upload_url, data=data, headers=headers, method="POST")
            with opener.open(request, timeout=20) as response:
                response_data = json.loads(response.read(64 * 1024).decode("utf-8", errors="replace") or "{}")
                if response.status != HTTPStatus.OK or response_data.get("status") != "ok":
                    raise ValueError(f"CPA 返回异常状态：HTTP {response.status}")
            pushed.append(path.name)
            fingerprints[path.name] = fingerprint
        except Exception as exc:  # noqa: BLE001
            failed.append({"name": path.name, "error": cpa_management_error(exc)})

    recognized = 0
    if pushed:
        try:
            request = urllib.request.Request(endpoint, headers=headers, method="GET")
            with opener.open(request, timeout=20) as response:
                payload = json.loads(response.read(1024 * 1024).decode("utf-8", errors="replace") or "{}")
            remote_names = {
                str(item.get("name") or "")
                for item in payload.get("files", [])
                if isinstance(item, dict)
            }
            for name in pushed:
                if name not in remote_names:
                    failed.append({"name": name, "error": "CPA 未在热加载列表中识别该凭证"})
                    continue
                recognized += 1
                registry["files"][name] = {
                    "sha256": fingerprints[name],
                    "pushed_at": now_iso(),
                    "target": base_url,
                }
            if recognized:
                write_cpa_push_status(source_dir, registry)
        except Exception as exc:  # noqa: BLE001
            failed.append({"name": "热加载校验", "error": cpa_management_error(exc)})

    target = urllib.parse.urlparse(base_url)
    return {
        "ok": not failed and recognized == len(pushed),
        "total": len(paths),
        "pending": len(paths) - len(skipped),
        "pushed": len(pushed),
        "recognized": recognized,
        "skipped": len(skipped),
        "failed": len(failed),
        "failures": failed,
        "target": target.netloc,
    }


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


def model_test_reason(item: dict[str, Any]) -> str:
    if item.get("ok"):
        return "available"
    status = int(item.get("status") or 0)
    error = str(item.get("error") or "").lower()
    if status == 403 and (
        "permission-denied" in error or "access to the chat endpoint is denied" in error
    ):
        return "permission_denied"
    if status == 429:
        return "quota_limited"
    if status == 401 or "invalid or expired credentials" in error or "no auth context" in error:
        return "credential_invalid"
    return "unavailable"


def public_account(
    record: dict[str, Any],
    push_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cpa_path = Path(record["cpa_path"]) if record.get("cpa_path") else None
    push_state = (
        cpa_file_push_state(cpa_path, registry=push_registry)
        if cpa_path
        else {"pushed": False, "pushed_at": "", "target": ""}
    )
    capability = (
        read_model_capability(current_cpa_dir(), record["email"])
        if record.get("cpa_path")
        else {}
    )
    raw_tests = capability.get("tests")
    tests = raw_tests if isinstance(raw_tests, dict) else {}
    return {
        "id": stable_id(record["email"]),
        "email": record["email"],
        "password_masked": mask(record.get("password", "")),
        "sso_masked": mask(record.get("sso", ""), left=5, right=5),
        "has_sso": bool(record.get("sso")),
        "has_cpa": bool(record.get("cpa_path")),
        "cpa_file": Path(record["cpa_path"]).name if record.get("cpa_path") else "",
        "cpa_pushed": bool(push_state.get("pushed")),
        "cpa_pushed_at": str(push_state.get("pushed_at") or ""),
        "cpa_push_target": str(push_state.get("target") or ""),
        "source": record.get("source", ""),
        "updated_at": record.get("updated_at", ""),
        "models": capability.get("models") if isinstance(capability.get("models"), list) else [],
        "models_ok": bool(capability.get("models_ok")),
        "models_status": int(capability.get("models_status") or 0),
        "models_error": str(capability.get("models_error") or ""),
        "models_checked_at": str(capability.get("checked_at") or ""),
        "model_tests": {
            str(model): {
                "ok": bool(item.get("ok")),
                "status": int(item.get("status") or 0),
                "reason": model_test_reason(item),
                "rate_limits": item.get("rate_limits") if isinstance(item.get("rate_limits"), dict) else {},
                "tested_at": str(item.get("tested_at") or ""),
            }
            for model, item in tests.items()
            if isinstance(item, dict)
        },
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


def hotmail_invalid_accounts() -> dict[str, dict[str, str]]:
    invalid: dict[str, dict[str, str]] = {}
    for line in read_lines(HOTMAIL_INVALID_PATH):
        parts = line.split("----", 2)
        email = parts[0].strip().lower() if parts else ""
        if not email or "@" not in email:
            continue
        invalid[email] = {
            "email": email,
            "time": parts[1].strip() if len(parts) > 1 else "",
            "reason": parts[2].strip() if len(parts) > 2 else "授权已失效，需要重新登录",
        }
    return invalid


def clear_hotmail_invalid_markers(emails: set[str]) -> int:
    wanted = {str(email).strip().lower() for email in emails if str(email).strip()}
    if not wanted:
        return 0
    original = read_lines(HOTMAIL_INVALID_PATH)
    kept = [line for line in original if line.split("----", 1)[0].strip().lower() not in wanted]
    removed = len(original) - len(kept)
    if HOTMAIL_INVALID_PATH.exists() or removed:
        atomic_write(HOTMAIL_INVALID_PATH, ("\n".join(kept) + "\n") if kept else "")
    return removed


def tracked_registration_emails() -> set[str]:
    consumed: set[str] = set()
    paths = [*account_files(), EMAILS_USED_PATH, EMAILS_ERROR_PATH]
    for path in paths:
        for line in read_lines(path):
            email = line.split("----", 1)[0].strip().lower()
            if email and "@" in email:
                consumed.add(email)
    return consumed


def registration_capacity(
    mailbox_records: list[dict[str, str]] | None = None,
    consumed_emails: set[str] | None = None,
    max_aliases: int | None = None,
    invalid_emails: set[str] | None = None,
) -> int:
    if mailbox_records is None:
        mailbox_records, _ = parse_mail_text(read_text(current_mail_path()))
    if consumed_emails is None:
        consumed_emails = tracked_registration_emails()
    if max_aliases is None:
        config = load_json_object(CONFIG_PATH)
        try:
            max_aliases = int(config.get("hotmail_max_aliases_per_account", 5) or 5)
        except (TypeError, ValueError):
            max_aliases = 5
    if invalid_emails is None:
        invalid_emails = set(hotmail_invalid_accounts())
    limit = max(1, int(max_aliases))
    return sum(
        max(
            0,
            limit - sum(1 for email in consumed_emails if is_alias_of(email, record["email"])),
        )
        for record in mailbox_records
        if record["email"].lower() not in invalid_emails
    )


def is_alias_of(candidate: str, main_email: str) -> bool:
    if "@" not in candidate or "@" not in main_email:
        return candidate.lower() == main_email.lower()
    c_local, c_domain = candidate.lower().split("@", 1)
    m_local, m_domain = main_email.lower().split("@", 1)
    return c_domain == m_domain and (c_local == m_local or c_local.startswith(m_local + "+"))


def list_mailboxes() -> list[dict[str, Any]]:
    records, _ = parse_mail_text(read_text(current_mail_path()))
    used, failed = tracked_email_counts()
    invalid = hotmail_invalid_accounts()
    out = []
    for record in records:
        email = record["email"]
        invalid_record = invalid.get(email.lower())
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
                "status": "oauth_expired" if invalid_record else ("attention" if failed_count else ("active" if used_count else "ready")),
                "invalid_reason": invalid_record["reason"] if invalid_record else "",
                "invalid_time": invalid_record["time"] if invalid_record else "",
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
        clear_hotmail_invalid_markers(wanted)
    return removed


def account_files() -> list[Path]:
    files: list[Path] = []
    if ACCOUNTS_PATH.is_file():
        files.append(ACCOUNTS_PATH)
    for path in sorted(ROOT.glob("accounts_*.txt"), key=lambda p: p.stat().st_mtime if p.exists() else 0):
        if path not in files:
            files.append(path)
    return files


def find_cpa_path_in_dir(auth_dir: Path, email: str) -> Path | None:
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


def find_cpa_path(email: str) -> Path | None:
    return find_cpa_path_in_dir(current_cpa_dir(), email)


def remove_success_accounts(emails: list[str]) -> dict[str, Any]:
    wanted = {str(email).strip().lower() for email in emails if str(email).strip()}
    if not wanted:
        raise ValueError("请选择要删除的账户")

    auth_dirs = [current_cpa_dir().resolve()]
    config = load_json_object(CONFIG_PATH)
    hotload_value = str(config.get("cpa_hotload_dir") or "").strip()
    if hotload_value:
        hotload = Path(hotload_value).expanduser()
        hotload = hotload.resolve() if hotload.is_absolute() else (ROOT / hotload).resolve()
        if hotload not in auth_dirs:
            auth_dirs.append(hotload)

    auth_paths: set[Path] = set()
    for email in wanted:
        for auth_dir in auth_dirs:
            path = find_cpa_path_in_dir(auth_dir, email)
            if path:
                resolved = path.resolve()
                if resolved.parent == auth_dir:
                    auth_paths.add(resolved)

    mailbox_records, _ = parse_mail_text(read_text(current_mail_path()))
    mailbox_emails = {
        record["email"].lower()
        for record in mailbox_records
        if any(is_alias_of(account_email, record["email"]) for account_email in wanted)
    }

    files = account_files()
    with FILE_LOCK:
        account_rows = sum(filter_delimited_file(path, wanted) for path in files)
        mailbox_rows = remove_mailboxes(list(mailbox_emails))
        failure_rows = clear_failures(list(wanted))
        cpa_files = 0
        for path in auth_paths:
            try:
                path.unlink()
                cpa_files += 1
            except FileNotFoundError:
                pass
        model_caches = sum(
            1 for email in wanted if delete_model_capability(auth_dirs[0], email)
        )
        push_markers = delete_cpa_push_status(
            auth_dirs[0],
            {path.name for path in auth_paths if path.parent == auth_dirs[0]},
        )

    return {
        "ok": True,
        "requested": len(wanted),
        "account_rows": account_rows,
        "mailbox_rows": mailbox_rows,
        "failure_rows": failure_rows,
        "cpa_files": cpa_files,
        "model_caches": model_caches,
        "push_markers": push_markers,
        "used_markers_retained": True,
    }


def load_cpa_auth(email: str) -> tuple[Path, dict[str, Any]]:
    path = find_cpa_path(email)
    if not path:
        raise ValueError("该账户还没有 CPA 凭证，请先获取 CPA")
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise ValueError("CPA 凭证文件不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("CPA 凭证内容无效")
    if not str(data.get("access_token") or "").strip():
        raise ValueError("CPA 凭证缺少 access_token")
    return path, data


def cpa_probe_proxy(target: str = "") -> str:
    config = load_json_object(CONFIG_PATH)
    if not proxy_pool.proxy_is_enabled(config, purpose="cpa"):
        raise ValueError("模型与额度接口必须走代理，请先在“代理网络”中开启代理")
    selected = proxy_pool.prepare_proxy(config, purpose="cpa", target=target or None)
    proxy = str(selected.get("proxy") or proxy_pool.effective_proxy(config, purpose="cpa")).strip()
    if not proxy:
        raise ValueError("代理已开启但未获得可用代理地址")
    return proxy


def run_cpa_probe(
    target: str,
    callback: Callable[[str], dict[str, Any]],
    *,
    attempts: int = 3,
) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for attempt in range(1, max(1, attempts) + 1):
        proxy = cpa_probe_proxy(target)
        last = callback(proxy)
        last["attempts"] = attempt
        status = int(last.get("status") or 0)
        if invalid_cpa_auth_result(last) or model_test_reason(last) == "permission_denied":
            return last
        if last.get("ok") or status not in {0, 403, 408, 502, 503, 504}:
            return last
    return last


def invalid_cpa_auth_result(result: dict[str, Any]) -> bool:
    if result.get("ok"):
        return False
    status = int(result.get("status") or 0)
    error = str(result.get("error") or "").lower()
    markers = (
        "invalid or expired credentials",
        "no auth context",
        "permissiondenied",
        "invalid_token",
    )
    return status == 401 or any(marker in error for marker in markers)


def auto_refresh_cpa_auth(
    auth_path: Path,
    auth: dict[str, Any],
    failed_result: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    if not invalid_cpa_auth_result(failed_result):
        return auth, False
    base_url = str(auth.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    try:
        proxy = cpa_probe_proxy(f"{base_url}/models")
        refresh_cpa_auth(auth_path, proxy=proxy, probe=True, timeout=60.0)
        refreshed = json.loads(read_text(auth_path))
        if not isinstance(refreshed, dict):
            raise ValueError("刷新后的 CPA 凭证内容无效")
        return refreshed, True
    except Exception as exc:
        raise FullCredentialRefreshRequired(
            f"凭证已失效且自动刷新失败，请点击“刷新凭证”执行完整重取: {exc}"
        ) from exc


def refresh_account_models(email: str) -> dict[str, Any]:
    email = email.strip().lower()
    if not email:
        raise ValueError("缺少账户邮箱")
    auth_path, auth = load_cpa_auth(email)
    base_url = str(auth.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    result = run_cpa_probe(
        f"{base_url}/models",
        lambda proxy: probe_models(
            str(auth.get("access_token") or ""),
            base_url=base_url,
            proxy=proxy,
        ),
    )
    auth, credential_refreshed = auto_refresh_cpa_auth(auth_path, auth, result)
    if credential_refreshed:
        base_url = str(auth.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        result = run_cpa_probe(
            f"{base_url}/models",
            lambda proxy: probe_models(
                str(auth.get("access_token") or ""),
                base_url=base_url,
                proxy=proxy,
            ),
        )
    cached = record_model_list(current_cpa_dir(), email, result)
    return {
        "ok": bool(result.get("ok")),
        "email": email,
        "status": int(result.get("status") or 0),
        "models": cached.get("models") or [],
        "checked_at": cached.get("checked_at") or "",
        "attempts": int(result.get("attempts") or 1),
        "credential_refreshed": credential_refreshed,
        "error": str(result.get("error") or ""),
    }


def test_account_model(email: str, model: str) -> dict[str, Any]:
    email = email.strip().lower()
    model = model.strip()
    if not email:
        raise ValueError("缺少账户邮箱")
    if not model or len(model) > 120 or not re.fullmatch(r"[A-Za-z0-9._:/-]+", model):
        raise ValueError("模型名称格式无效")
    auth_path, auth = load_cpa_auth(email)
    base_url = str(auth.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    result = run_cpa_probe(
        f"{base_url}/responses",
        lambda proxy: probe_mini_response(
            str(auth.get("access_token") or ""),
            model=model,
            base_url=base_url,
            proxy=proxy,
        ),
    )
    auth, credential_refreshed = auto_refresh_cpa_auth(auth_path, auth, result)
    if credential_refreshed:
        base_url = str(auth.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        result = run_cpa_probe(
            f"{base_url}/responses",
            lambda proxy: probe_mini_response(
                str(auth.get("access_token") or ""),
                model=model,
                base_url=base_url,
                proxy=proxy,
            ),
        )
    cached = record_model_test(current_cpa_dir(), email, model, result)
    tested = (cached.get("tests") or {}).get(model) or {}
    return {
        "ok": bool(result.get("ok")),
        "email": email,
        "model": model,
        "status": int(result.get("status") or 0),
        "tested_at": str(tested.get("tested_at") or ""),
        "attempts": int(result.get("attempts") or 1),
        "credential_refreshed": credential_refreshed,
        "reason": model_test_reason(result),
        "text": str(result.get("text") or "")[:200],
        "rate_limits": result.get("rate_limits") or {},
        "error": str(result.get("error") or "")[:500],
    }


def refresh_account_quota(email: str) -> dict[str, Any]:
    email = email.strip().lower()
    if not email:
        raise ValueError("缺少账户邮箱")
    auth_path, auth = load_cpa_auth(email)
    base_url = str(auth.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    credential_refreshed = False
    capability = read_model_capability(current_cpa_dir(), email)
    models = [str(item).strip() for item in (capability.get("models") or []) if str(item).strip()]
    if not models:
        discovered = run_cpa_probe(
            f"{base_url}/models",
            lambda proxy: probe_models(
                str(auth.get("access_token") or ""),
                base_url=base_url,
                proxy=proxy,
            ),
        )
        auth, credential_refreshed = auto_refresh_cpa_auth(auth_path, auth, discovered)
        if credential_refreshed:
            base_url = str(auth.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
            discovered = run_cpa_probe(
                f"{base_url}/models",
                lambda proxy: probe_models(
                    str(auth.get("access_token") or ""),
                    base_url=base_url,
                    proxy=proxy,
                ),
            )
        capability = record_model_list(current_cpa_dir(), email, discovered)
        if not discovered.get("ok"):
            return {
                "ok": False,
                "email": email,
                "models": [],
                "results": [],
                "error": str(discovered.get("error") or f"HTTP {discovered.get('status') or 0}"),
            }
        models = [str(item).strip() for item in (capability.get("models") or []) if str(item).strip()]
    results: list[dict[str, Any]] = []
    for model in models:
        tested = run_cpa_probe(
            f"{base_url}/responses",
            lambda proxy, model=model: probe_mini_response(
                str(auth.get("access_token") or ""),
                model=model,
                base_url=base_url,
                proxy=proxy,
            ),
        )
        if not credential_refreshed:
            auth, refreshed_now = auto_refresh_cpa_auth(auth_path, auth, tested)
            if refreshed_now:
                credential_refreshed = True
                base_url = str(auth.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
                tested = run_cpa_probe(
                    f"{base_url}/responses",
                    lambda proxy, model=model: probe_mini_response(
                        str(auth.get("access_token") or ""),
                        model=model,
                        base_url=base_url,
                        proxy=proxy,
                    ),
                )
        record_model_test(current_cpa_dir(), email, model, tested)
        results.append(
            {
                "model": model,
                "ok": bool(tested.get("ok")),
                "status": int(tested.get("status") or 0),
                "attempts": int(tested.get("attempts") or 1),
                "reason": model_test_reason(tested),
                "rate_limits": tested.get("rate_limits") or {},
                "error": str(tested.get("error") or "")[:500],
            }
        )
    return {
        "ok": bool(results) and all(item["ok"] for item in results),
        "email": email,
        "models": models,
        "results": results,
        "credential_refreshed": credential_refreshed,
        "error": "" if results else "该账户没有可测试的模型",
    }


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


def remove_invalid_mailboxes() -> dict[str, Any]:
    invalid = hotmail_invalid_accounts()
    main_emails = set(invalid)
    if not main_emails:
        return {"ok": True, "removed": 0, "invalid_markers": 0, "failure_rows": 0}

    def is_related_failure(line: str) -> bool:
        failed_email = line.split("----", 1)[0].strip().lower()
        return bool(failed_email) and any(is_alias_of(failed_email, main) for main in main_emails)

    with FILE_LOCK:
        removed = remove_mailboxes(list(main_emails))
        original_failures = read_lines(EMAILS_ERROR_PATH)
        kept_failures = [line for line in original_failures if not is_related_failure(line)]
        failure_rows = len(original_failures) - len(kept_failures)
        if EMAILS_ERROR_PATH.exists() or failure_rows:
            atomic_write(EMAILS_ERROR_PATH, ("\n".join(kept_failures) + "\n") if kept_failures else "")
    return {
        "ok": True,
        "removed": removed,
        "invalid_markers": len(main_emails),
        "failure_rows": failure_rows,
    }


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
        "browser_script_timeout_sec": "浏览器脚本硬超时",
        "cpa_export_enabled": "注册后生成 CPA 凭证",
        "cpa_auth_dir": "CPA 凭证目录",
        "cpa_management_url": "CPA 管理地址",
        "cpa_management_key": "CPA 管理密码/密钥",
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
        secret = key == "cpa_management_key" or any(
            word in lowered
            for word in ("password", "token", "app_key", "api_key", "jwt", "secret", "subscription")
        )
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
            cleaned_browsers = cleanup_project_browsers()
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
                start_new_session=os.name != "nt",
            )
            if cleaned_browsers:
                self.append(f"启动前已清理残留浏览器进程：{cleaned_browsers} 个")
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
            if bool(options.get("refresh_existing", False)):
                command.append("--refresh-existing")
            elif bool(options.get("force", False)):
                command.append("--no-skip-existing")
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
            cleaned_browsers = cleanup_project_browsers()
            self.returncode = code
            if self.status == "stopping":
                self.status = "stopped"
            else:
                self.status = "completed" if code == 0 else "failed"
            self.ended_at = now_iso()
            if cleaned_browsers:
                self.append(f"任务结束后已回收浏览器进程：{cleaned_browsers} 个")
            self.append(f"任务结束，退出码 {code}", "success" if code == 0 else "error")

    @staticmethod
    def _signal_process_group(process: subprocess.Popen[str], sig: int) -> None:
        if os.name == "nt":
            if sig == signal.SIGTERM:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.kill()
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def stop(self) -> dict[str, Any]:
        with self.lock:
            process = self.process
            if not process or process.poll() is not None:
                return self.snapshot()
            self.status = "stopping"
            self.append("正在停止任务…", "warning")
            try:
                self._signal_process_group(process, signal.SIGTERM)
            except Exception:
                process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self._signal_process_group(process, signal.SIGKILL)
            process.wait(timeout=3)
        cleanup_project_browsers()
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
    capacity = registration_capacity()
    return {
        "mailboxes": len(mailboxes),
        "mailboxes_ready": sum(1 for item in mailboxes if item["status"] == "ready"),
        "register_capacity": capacity,
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
                push_registry = read_cpa_push_status()
                items = [public_account(item, push_registry) for item in discover_accounts()]
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
            if path == "/api/mailboxes/delete-invalid":
                return self.json_response(remove_invalid_mailboxes())
            if path == "/api/accounts/delete":
                return self.json_response(remove_success_accounts(list(body.get("emails") or [])))
            if path == "/api/cpa/push":
                return self.json_response(
                    push_cpa_auths_to_management(
                        emails=list(body.get("emails") or []),
                        force=bool(body.get("force", False)),
                    )
                )
            if path == "/api/failures/retry":
                removed = clear_failures(list(body.get("emails") or []))
                return self.json_response({"ok": True, "removed": removed})
            if path == "/api/account/models/refresh":
                return self.json_response(refresh_account_models(str(body.get("email") or "")))
            if path == "/api/account/model/test":
                return self.json_response(
                    test_account_model(
                        str(body.get("email") or ""),
                        str(body.get("model") or ""),
                    )
                )
            if path == "/api/account/quota/refresh":
                return self.json_response(refresh_account_quota(str(body.get("email") or "")))
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
        except FullCredentialRefreshRequired as exc:
            return self.json_response(
                {
                    "ok": False,
                    "code": "credential_refresh_required",
                    "error": str(exc),
                },
                409,
            )
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
