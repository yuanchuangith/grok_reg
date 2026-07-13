"""Persist per-account model discovery and probe results outside CPA auth files."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CACHE_DIR_NAME = ".model_capabilities"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _cache_path(auth_dir: str | Path, email: str) -> Path:
    key = hashlib.sha1(email.strip().lower().encode("utf-8", errors="replace")).hexdigest()
    return Path(auth_dir) / CACHE_DIR_NAME / f"{key}.json"


def read_model_capability(auth_dir: str | Path, email: str) -> dict[str, Any]:
    path = _cache_path(auth_dir, email)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def delete_model_capability(auth_dir: str | Path, email: str) -> bool:
    path = _cache_path(auth_dir, email)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def write_model_capability(auth_dir: str | Path, email: str, data: dict[str, Any]) -> dict[str, Any]:
    path = _cache_path(auth_dir, email)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["email"] = email.strip()
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return payload


def record_model_list(
    auth_dir: str | Path,
    email: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    current = read_model_capability(auth_dir, email)
    discovered = [str(item) for item in (result.get("model_ids") or []) if str(item).strip()]
    models = discovered if result.get("ok") else list(current.get("models") or [])
    current.update(
        {
            "models": models,
            "models_ok": bool(result.get("ok")),
            "models_status": int(result.get("status") or 0),
            "models_error": str(result.get("error") or "")[:500],
            "checked_at": _now_iso(),
        }
    )
    return write_model_capability(auth_dir, email, current)


def record_model_test(
    auth_dir: str | Path,
    email: str,
    model: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    current = read_model_capability(auth_dir, email)
    tests = current.get("tests")
    if not isinstance(tests, dict):
        tests = {}
    tests[model] = {
        "ok": bool(result.get("ok")),
        "status": int(result.get("status") or 0),
        "error": str(result.get("error") or "")[:500],
        "rate_limits": {
            str(key): str(value)
            for key, value in (result.get("rate_limits") or {}).items()
        },
        "tested_at": _now_iso(),
    }
    current["tests"] = tests
    return write_model_capability(auth_dir, email, current)
