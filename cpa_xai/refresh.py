"""Refresh an existing CPA xAI credential without replacing it until verified."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .model_capabilities import record_model_list
from .oauth_device import refresh_access_token
from .probe import probe_models
from .schema import DEFAULT_BASE_URL, DEFAULT_TOKEN_ENDPOINT, expired_from_access_token
from .writer import write_cpa_xai_auth


LogFn = Callable[[str], None]


class CredentialRefreshError(RuntimeError):
    pass


def _expiry(access_token: str, expires_in: int) -> str:
    try:
        expired, _, _ = expired_from_access_token(access_token)
        if expired:
            return expired
    except Exception:
        pass
    return (datetime.now(tz=timezone.utc) + timedelta(seconds=max(expires_in, 60))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def refresh_cpa_auth(
    path: str | Path,
    *,
    proxy: str,
    probe: bool = True,
    timeout: float = 30.0,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Refresh, verify, then atomically replace one existing CPA auth file."""
    log = log or (lambda _: None)
    auth_path = Path(path).expanduser().resolve()
    try:
        current = json.loads(auth_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CredentialRefreshError("CPA credential file not found") from exc
    except json.JSONDecodeError as exc:
        raise CredentialRefreshError("CPA credential file is invalid JSON") from exc
    if not isinstance(current, dict):
        raise CredentialRefreshError("CPA credential content is invalid")
    email = str(current.get("email") or "").strip()
    refresh_token = str(current.get("refresh_token") or "").strip()
    if not email:
        raise CredentialRefreshError("CPA credential is missing email")
    if not refresh_token:
        raise CredentialRefreshError("CPA credential is missing refresh_token")
    if not str(proxy or "").strip():
        raise CredentialRefreshError("credential refresh requires a proxy")

    log("[CPA-REFRESH-101] 使用 refresh_token 请求新 access_token")
    try:
        tokens = refresh_access_token(
            refresh_token,
            token_endpoint=str(current.get("token_endpoint") or DEFAULT_TOKEN_ENDPOINT),
            timeout=timeout,
            proxy=proxy,
        )
    except Exception as exc:
        raise CredentialRefreshError(str(exc)) from exc

    updated = dict(current)
    updated.update(
        {
            "type": "xai",
            "auth_kind": "oauth",
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "token_type": tokens.token_type or "Bearer",
            "expires_in": int(tokens.expires_in or 21600),
            "expired": _expiry(tokens.access_token, int(tokens.expires_in or 21600)),
            "last_refresh": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "disabled": False,
        }
    )
    if tokens.id_token:
        updated["id_token"] = tokens.id_token

    probe_result: dict[str, Any] | None = None
    if probe:
        log("[CPA-REFRESH-102] 验证新 access_token 的模型权限")
        probe_result = probe_models(
            tokens.access_token,
            base_url=str(updated.get("base_url") or DEFAULT_BASE_URL),
            proxy=proxy,
        )
        if not probe_result.get("ok") or not probe_result.get("has_grok_45"):
            status = int(probe_result.get("status") or 0)
            reason = str(probe_result.get("error") or "grok-4.5 not available")[:200]
            raise CredentialRefreshError(f"refreshed token verification failed HTTP {status}: {reason}")

    written = write_cpa_xai_auth(auth_path.parent, updated, filename=auth_path.name)
    if probe_result is not None:
        record_model_list(auth_path.parent, email, probe_result)
    log("[CPA-REFRESH-103] 新凭证验证成功并已原子替换旧文件")
    return {
        "ok": True,
        "email": email,
        "path": str(written),
        "refresh_method": "refresh_token",
        "probe_models": probe_result,
    }
