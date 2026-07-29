"""High-level: mint CPA xai-*.json for one free registered account."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .browser_confirm import mint_with_browser
from .model_capabilities import record_model_list
from .probe import probe_mini_response, probe_models
from .protocol_mint import ProtocolMintError, extract_sso_from_cookies, mint_with_sso_protocol
from .proxyutil import proxy_log_label, resolve_proxy, set_runtime_proxy
from .schema import DEFAULT_BASE_URL, build_cpa_xai_auth
from .writer import write_cpa_xai_auth

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def mint_and_export(
    *,
    email: str,
    password: str,
    auth_dir: str | Path,
    page: Any | None = None,
    proxy: str | None = None,
    headless: bool = False,
    base_url: str = DEFAULT_BASE_URL,
    probe: bool = True,
    probe_chat: bool = False,
    browser_timeout_sec: float = 240.0,
    force_standalone: bool = True,
    cookies: Any | None = None,
    sso: str | None = None,
    reuse_browser: bool = True,
    recycle_every: int = 15,
    prefer_protocol: bool = True,
    protocol_only: bool = False,
    protocol_poll_timeout_sec: float = 90.0,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Full pipeline: (protocol SSO device-flow |) browser device-auth → write CPA → probe.

    Protocol path (curl_cffi + sso cookie) is tried first when prefer_protocol
    and an sso cookie is available. On failure, falls back to browser mint unless
    protocol_only=True.

    Returns dict with keys: ok, path, email, probe, error?, mint_method?
    """
    log = log or _noop
    email = (email or "").strip()
    if not email or not password:
        # Protocol can work with sso alone; password only required for browser fallback
        if not email:
            return {"ok": False, "email": email, "error": "missing email"}
        if not (sso or extract_sso_from_cookies(cookies)):
            return {"ok": False, "email": email, "error": "missing email/password"}

    # Config/explicit proxy wins over shell https_proxy (common 7890 trap).
    # Thread-local pin — safe under concurrent mint workers.
    resolved = resolve_proxy(proxy)
    set_runtime_proxy(resolved or None)
    log(f"mint start: {email} proxy={proxy_log_label(resolved) or '(none)'}")

    sso_val = (sso or "").strip() or extract_sso_from_cookies(cookies)
    tokens: dict[str, Any] | None = None
    protocol_err: str | None = None

    if prefer_protocol and sso_val:
        log("mint try protocol (SSO HTTP device flow)")
        try:
            tokens = mint_with_sso_protocol(
                sso_cookie=sso_val,
                email=email,
                proxy=resolved or None,
                poll_timeout_sec=protocol_poll_timeout_sec,
                log=log,
                cancel=cancel,
            )
            log("mint protocol SUCCESS")
        except ProtocolMintError as e:
            protocol_err = str(e)
            log(f"mint protocol failed: {e}")
            if protocol_only:
                return {
                    "ok": False,
                    "email": email,
                    "error": f"protocol_only: {e}",
                    "mint_method": "protocol",
                }
            log("mint fallback → browser")
        except Exception as e:  # noqa: BLE001
            protocol_err = str(e)
            log(f"mint protocol exception: {e}")
            if protocol_only:
                return {
                    "ok": False,
                    "email": email,
                    "error": f"protocol_only: {e}",
                    "mint_method": "protocol",
                }
            log("mint fallback → browser")
    elif prefer_protocol and not sso_val:
        log("mint protocol skipped (no sso cookie) → browser")
        if protocol_only:
            return {
                "ok": False,
                "email": email,
                "error": "protocol_only but no sso cookie",
                "mint_method": "protocol",
            }
    elif not prefer_protocol:
        log("mint protocol disabled → browser")

    if tokens is None:
        if not password:
            return {
                "ok": False,
                "email": email,
                "error": protocol_err or "protocol failed and no password for browser fallback",
                "protocol_error": protocol_err,
            }
        browser_cookies = cookies
        if sso_val and not extract_sso_from_cookies(browser_cookies):
            # The protocol path may validate the SSO session but still fail the
            # device approval. Preserve that authenticated session for the
            # browser fallback instead of forcing a second email/password login.
            if isinstance(browser_cookies, dict):
                browser_cookies = dict(browser_cookies)
                browser_cookies.setdefault("sso", sso_val)
                browser_cookies.setdefault("sso-rw", sso_val)
            elif isinstance(browser_cookies, (list, tuple)):
                browser_cookies = list(browser_cookies) + [
                    {"name": "sso", "value": sso_val, "domain": ".x.ai", "path": "/"},
                    {"name": "sso-rw", "value": sso_val, "domain": ".x.ai", "path": "/"},
                ]
            else:
                browser_cookies = {"sso": sso_val, "sso-rw": sso_val}
            log("browser fallback will inject existing SSO session")
        try:
            tokens = mint_with_browser(
                email=email,
                password=password,
                page=None if force_standalone else page,
                proxy=resolved or None,
                headless=headless,
                browser_timeout_sec=browser_timeout_sec,
                force_standalone=force_standalone,
                cookies=browser_cookies,
                reuse_browser=reuse_browser,
                recycle_every=recycle_every,
                poll_log=log,
                cancel=cancel,
            )
            tokens["mint_method"] = "browser"
            if protocol_err:
                tokens["protocol_error"] = protocol_err
        except Exception as e:  # noqa: BLE001
            log(f"mint failed: {e}")
            err = str(e)
            if protocol_err:
                err = f"{err} (protocol: {protocol_err})"
            return {
                "ok": False,
                "email": email,
                "error": err,
                "protocol_error": protocol_err,
            }

    payload = build_cpa_xai_auth(
        email=email,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        id_token=tokens.get("id_token"),
        expires_in=tokens.get("expires_in"),
        base_url=base_url,
    )
    path = write_cpa_xai_auth(auth_dir, payload)
    log(f"wrote {path}")

    result: dict[str, Any] = {
        "ok": True,
        "email": email,
        "path": str(path),
        "user_code": tokens.get("user_code"),
        "base_url": base_url,
        "proxy": proxy_log_label(resolved),
        "mint_method": tokens.get("mint_method") or "browser",
    }
    if protocol_err and result["mint_method"] != "protocol":
        result["protocol_error"] = protocol_err

    if probe:
        pr = probe_models(tokens["access_token"], base_url=base_url, proxy=resolved or None)
        result["probe_models"] = pr
        record_model_list(auth_dir, email, pr)
        probe_status = pr.get("status")
        probe_models_list = pr.get("model_ids") or []
        if pr.get("ok") and pr.get("has_grok_45"):
            log(
                f"[CPA-PROBE-200] 凭证验证成功: HTTP {probe_status}，"
                f"支持 grok-4.5，可用模型={probe_models_list}"
            )
        elif pr.get("ok"):
            log(
                f"[CPA-PROBE-404] 凭证可访问模型接口，但不支持 grok-4.5: "
                f"HTTP {probe_status}，可用模型={probe_models_list}"
            )
        else:
            probe_error = str(pr.get("error") or "未知错误")[:200]
            log(
                f"[CPA-PROBE-500] 凭证验证失败: HTTP {probe_status}，"
                f"原因={probe_error}"
            )
        if not pr.get("has_grok_45"):
            result["ok"] = False
            result["error"] = "token ok but grok-4.5 not listed"
        if probe_chat and pr.get("has_grok_45"):
            ch = probe_mini_response(
                tokens["access_token"], base_url=base_url, proxy=resolved or None
            )
            result["probe_chat"] = ch
            log(f"probe chat: ok={ch.get('ok')} model={ch.get('model')} text={ch.get('text')!r}")
            if not ch.get("ok"):
                result["ok"] = False
                result["error"] = f"chat probe failed: {ch.get('error') or ch.get('status')}"
    return result
