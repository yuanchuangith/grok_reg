#!/usr/bin/env bash
set -euo pipefail

ARCHIVE="${1:?usage: remote_install.sh /path/to/archive.tar.gz}"
APP_DIR="${APP_DIR:-/home/ubuntu/grok-account-studio}"
APP_USER="${APP_USER:-ubuntu}"
PORT="${PORT:-8318}"
DASHBOARD_USERNAME="${DASHBOARD_USERNAME:-admin}"
: "${DASHBOARD_PASSWORD:?DASHBOARD_PASSWORD is required}"

mkdir -p "$APP_DIR"
tar -xzf "$ARCHIVE" -C "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

python3 - "$APP_DIR/config.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
data["hotmail_accounts_file"] = "mail_credentials.txt"
data["cpa_auth_dir"] = "./cpa_auths"
data["proxy"] = "http://127.0.0.1:7897"
data["cpa_proxy"] = "http://127.0.0.1:7897"
data["cpa_headless"] = False
data["cpa_force_standalone"] = True
data["grok2api_auto_add_remote"] = False
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

python3 - "$APP_DIR" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
merged = {}
files = sorted(root.glob("accounts_*.txt"))
primary = root / "accounts_cli.txt"
if primary.is_file() and primary not in files:
    files.insert(0, primary)
for path in files:
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----", 2)
        if len(parts) < 2 or not parts[0].strip():
            continue
        email = parts[0].strip()
        password = parts[1].strip()
        sso = parts[2].strip() if len(parts) > 2 else ""
        merged[email.lower()] = "----".join((email, password, sso))
primary.write_text(("\n".join(merged.values()) + "\n") if merged else "", encoding="utf-8")
print(f"primary accounts ledger: {len(merged)}")
PY

if ! command -v uv >/dev/null 2>&1 && [ ! -x "/home/$APP_USER/.local/bin/uv" ]; then
  sudo -u "$APP_USER" bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi
UV_BIN="$(command -v uv || true)"
if [ -z "$UV_BIN" ]; then
  UV_BIN="/home/$APP_USER/.local/bin/uv"
fi

if ! command -v google-chrome >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y wget ca-certificates xvfb fonts-noto-cjk
  wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -O /tmp/google-chrome-stable_current_amd64.deb
  sudo apt-get install -y /tmp/google-chrome-stable_current_amd64.deb
else
  sudo apt-get update
  sudo apt-get install -y xvfb fonts-noto-cjk
fi

sudo -u "$APP_USER" bash -lc "cd '$APP_DIR' && '$UV_BIN' sync"

install -m 600 -o "$APP_USER" -g "$APP_USER" /dev/null "$APP_DIR/.dashboard.env"
printf 'WEB_DASHBOARD_USERNAME=%s\nWEB_DASHBOARD_PASSWORD=%s\n' "$DASHBOARD_USERNAME" "$DASHBOARD_PASSWORD" > "$APP_DIR/.dashboard.env"
chown "$APP_USER:$APP_USER" "$APP_DIR/.dashboard.env"
chmod 600 "$APP_DIR/.dashboard.env" "$APP_DIR/config.json" "$APP_DIR/mail_credentials.txt" "$APP_DIR/accounts_cli.txt"
find "$APP_DIR/cpa_auths" -type f -exec chmod 600 {} + 2>/dev/null || true

cat > /tmp/grok-account-studio.service <<EOF
[Unit]
Description=Grok Account Studio Web Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=HOME=/home/$APP_USER
Environment=PYTHONUTF8=1
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=$APP_DIR/.dashboard.env
ExecStart=/usr/bin/xvfb-run -a $UV_BIN run python -u web_dashboard.py --host 0.0.0.0 --port $PORT --quiet
Restart=always
RestartSec=4
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF

sudo install -m 644 /tmp/grok-account-studio.service /etc/systemd/system/grok-account-studio.service
sudo systemctl daemon-reload
sudo systemctl enable --now grok-account-studio.service

if command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q '^Status: active'; then
  sudo ufw allow "$PORT/tcp"
fi

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null; then
    break
  fi
  sleep 1
done

curl -fsS "http://127.0.0.1:$PORT/healthz"
echo
sudo systemctl --no-pager --full status grok-account-studio.service | sed -n '1,18p'
