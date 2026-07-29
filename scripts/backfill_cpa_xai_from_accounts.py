#!/usr/bin/env python3
"""Batch mint CPA xai-*.json from register accounts_cli.txt.

Default: headed Chromium + turnstilePatch (headless is Cloudflare-blocked on
accounts.x.ai). Token poll is source of truth; consent Allow uses real click.

Example (from grok_reg project root):
  export DISPLAY=:0
  uv run python -u scripts/backfill_cpa_xai_from_accounts.py \\
    --limit 1 --probe
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import shutil
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cpa_xai import (  # noqa: E402
    credential_file_name,
    existing_cpa_emails,
    mint_and_export,
    parse_accounts_file,
    refresh_cpa_auth,
    write_cpa_xai_auth,
)
import proxy_pool  # noqa: E402


PERMANENT_INVALID_FILE = ".account_permanent_invalid.json"


def permanent_access_denied(result: dict[str, object]) -> str:
    error = str(result.get("error") or "").strip()
    lowered = error.lower()
    if (
        lowered.startswith("device auth token error:")
        and "invalid_grant" in lowered
        and "access denied" in lowered
    ):
        return error[:500]
    return ""


def mark_permanent_invalid(auth_dir: str | Path, email: str, reason: str) -> Path:
    path = Path(auth_dir) / PERMANENT_INVALID_FILE
    payload: dict[str, object] = {"version": 1, "results": {}}
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(current, dict):
                payload.update(current)
        except Exception:
            pass
    rows = payload.get("results")
    if not isinstance(rows, dict):
        rows = {}
        payload["results"] = rows
    marked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    normalized = email.strip().lower()
    rows[normalized] = {
        "email": normalized,
        "reason": "xAI 已完成授权页面，但拒绝签发 OIDC 凭证",
        "error_code": "invalid_grant",
        "error_message": reason,
        "marked_at": marked_at,
    }
    payload["updated_at"] = marked_at
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def find_auth_path(auth_dir: str | Path, email: str) -> Path | None:
    root = Path(auth_dir)
    direct = root / credential_file_name(email)
    if direct.is_file():
        return direct
    target = email.strip().lower()
    for path in root.glob("xai-*.json") if root.is_dir() else ():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(data.get("email") or "").strip().lower() == target:
            return path
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--accounts",
        default=str(_ROOT / "accounts_cli.txt"),
    )
    ap.add_argument(
        "--emails-file",
        default="",
        help="Optional newline-delimited email allow-list",
    )
    ap.add_argument(
        "--out-dir",
        default=str(_ROOT / "cpa_auths"),
        help="Primary output under register machine",
    )
    ap.add_argument(
        "--cpa-dir",
        default="",
        help="Optional CPA hot-load auth-dir; files are copied here after success",
    )
    ap.add_argument("--limit", type=int, default=0, help="0 = all missing")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--email", default="", help="Only this email")
    ap.add_argument("--skip-existing", action="store_true", default=True)
    ap.add_argument("--no-skip-existing", action="store_false", dest="skip_existing")
    ap.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Refresh an existing credential first; fall back to full OIDC mint on failure",
    )
    ap.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Headless Chromium (usually blocked by Cloudflare on accounts.x.ai)",
    )
    ap.add_argument(
        "--headed",
        action="store_true",
        default=True,
        help="Show browser (default; required for stable device consent)",
    )
    ap.add_argument("--probe", action="store_true", default=True)
    ap.add_argument("--no-probe", action="store_false", dest="probe")
    ap.add_argument("--probe-chat", action="store_true", default=False)
    ap.add_argument(
        "--proxy",
        default="",
        help="Outbound proxy. Empty → read register config.json cpa_proxy/proxy, else env",
    )
    ap.add_argument(
        "--config",
        default=str(_ROOT / "config.json"),
        help="register config.json for cpa_proxy/proxy defaults",
    )
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--sleep", type=float, default=3.0, help="Sleep between accounts")
    ap.add_argument(
        "--fail-log",
        default=str(_ROOT / "cpa_auths" / "backfill_failed.jsonl"),
        help="Append failures JSONL",
    )
    ap.add_argument(
        "--force-standalone",
        action="store_true",
        default=True,
        help="Always open fresh Chromium (default)",
    )
    args = ap.parse_args()

    if args.refresh_existing:
        args.skip_existing = False

    if args.headless:
        args.headed = False
    else:
        args.headless = False

    # Resolve proxy: CLI > config cpa_proxy/proxy > env
    cfg = {}
    explicit_proxy_control = False
    if not args.proxy:
        try:
            cfg_path = Path(args.config)
            if cfg_path.is_file():
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    cfg = {
                        k: v
                        for k, v in cfg.items()
                        if not (isinstance(k, str) and (k.startswith("//") or k.startswith("#")))
                    }
                    args.proxy = proxy_pool.effective_proxy(cfg, purpose="cpa")
                    explicit_proxy_control = "proxy_enabled" in cfg
                    if not args.cpa_dir:
                        args.cpa_dir = (cfg.get("cpa_hotload_dir") or "").strip()
        except Exception as e:  # noqa: BLE001
            print(f"warn: read config proxy failed: {e}", flush=True)
    if not args.proxy and not explicit_proxy_control:
        args.proxy = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or ""
        ).strip()
    print(f"proxy={args.proxy or '(none)'}", flush=True)

    accounts = parse_accounts_file(args.accounts)
    if args.emails_file:
        selected_emails = {
            line.strip().lower()
            for line in Path(args.emails_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        accounts = [a for a in accounts if a.email.lower() in selected_emails]
    if args.email:
        accounts = [a for a in accounts if a.email.lower() == args.email.lower()]
    accounts = accounts[args.offset :]

    have = set()
    if args.skip_existing:
        have |= {e.lower() for e in existing_cpa_emails(args.out_dir)}
        if args.cpa_dir:
            have |= {e.lower() for e in existing_cpa_emails(args.cpa_dir)}

    todo = []
    for a in accounts:
        if args.skip_existing and a.email.lower() in have:
            continue
        todo.append(a)
        if args.limit and len(todo) >= args.limit:
            break

    print(f"accounts total={len(parse_accounts_file(args.accounts))} todo={len(todo)} out={args.out_dir}")
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    if args.cpa_dir:
        Path(args.cpa_dir).mkdir(parents=True, exist_ok=True)

    ok_n = fail_n = 0
    results = []
    for i, acc in enumerate(todo, 1):
        print(f"\n=== [{i}/{len(todo)}] {acc.email} ===", flush=True)

        def log(msg: str, _email=acc.email) -> None:
            print(f"[{time.strftime('%H:%M:%S')}] [{_email}] {msg}", flush=True)

        if isinstance(cfg, dict) and proxy_pool.proxy_is_enabled(cfg, purpose="cpa"):
            selected = proxy_pool.prepare_proxy(cfg, purpose="cpa", log=lambda msg: log(f"[proxy] {msg}"))
            args.proxy = str(selected.get("proxy") or proxy_pool.effective_proxy(cfg, purpose="cpa"))

        r = None
        existing_path = None
        previous_payload = None
        if args.refresh_existing:
            existing_path = find_auth_path(args.out_dir, acc.email)
            if existing_path:
                try:
                    previous_payload = json.loads(existing_path.read_text(encoding="utf-8"))
                except Exception:
                    previous_payload = None
                try:
                    log("[CPA-REFRESH-100] 发现已有凭证，优先执行 refresh_token 刷新")
                    r = refresh_cpa_auth(
                        existing_path,
                        proxy=args.proxy,
                        probe=args.probe,
                        probe_chat=args.probe_chat,
                        timeout=min(args.timeout, 90.0),
                        log=log,
                    )
                except Exception as exc:  # noqa: BLE001
                    log(f"[CPA-REFRESH-120] refresh_token 刷新失败，转入完整 OIDC 重取: {exc}")
            else:
                log("[CPA-REFRESH-110] 未找到旧凭证，直接执行完整 OIDC 获取")

        if r is None:
            r = mint_and_export(
                email=acc.email,
                password=acc.password,
                auth_dir=args.out_dir,
                page=None,
                proxy=args.proxy or None,
                headless=args.headless,
                probe=args.probe,
                probe_chat=args.probe_chat,
                browser_timeout_sec=args.timeout,
                force_standalone=args.force_standalone,
                sso=acc.sso or None,
                prefer_protocol=True,
                log=log,
            )
            if (
                args.refresh_existing
                and not r.get("ok")
                and existing_path is not None
                and isinstance(previous_payload, dict)
            ):
                write_cpa_xai_auth(
                    existing_path.parent,
                    previous_payload,
                    filename=existing_path.name,
                )
                log("[CPA-REFRESH-130] 新凭证验证失败，已恢复旧凭证文件")
        results.append(r)
        if r.get("ok") and r.get("path"):
            ok_n += 1
            # mirror into CPA auth-dir
            if args.cpa_dir:
                src = Path(r["path"])
                dst = Path(args.cpa_dir) / src.name
                shutil.copy2(src, dst)
                os.chmod(dst, 0o600)
                print(f"copied -> {dst}", flush=True)
        else:
            fail_n += 1
            permanent_reason = permanent_access_denied(r)
            if permanent_reason:
                marker_path = mark_permanent_invalid(
                    args.out_dir,
                    acc.email,
                    permanent_reason,
                )
                log(
                    "[CPA-REAUTH-410] xAI 拒绝签发 OIDC 凭证，已标记永久失效并跳过；"
                    f"marker={marker_path.name}"
                )
            if args.fail_log:
                Path(args.fail_log).parent.mkdir(parents=True, exist_ok=True)
                with open(args.fail_log, "a", encoding="utf-8") as f:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if args.sleep and i < len(todo):
            time.sleep(args.sleep)

    print(f"\n=== done ok={ok_n} fail={fail_n} ===", flush=True)
    summary = Path(args.out_dir) / f"backfill_summary_{int(time.time())}.json"
    summary.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"summary {summary}")
    return 0 if ok_n > 0 or not todo else 1


if __name__ == "__main__":
    raise SystemExit(main())
