#!/usr/bin/env bash
# Install or update scrape-api as a systemd service on this Linux machine.
# Run it from the repo (or a release dir) as a normal user with sudo rights. Safe to
# rerun: the API key and other settings in /etc/scrape-api/env are kept.
#
#   deploy/install.sh [--port N] [--public-url URL]
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT=""
PUBLIC_URL_ARG=""
ENV_FILE=/etc/scrape-api/env
UNIT_FILE=/etc/systemd/system/scrape-api.service
SOCKET_FILE=/etc/systemd/system/scrape-api.socket
WATCH_FILE=/etc/systemd/system/scrape-api-addresses.service
# The address watcher runs as root, so its code lives where only root can change it.
LIB_DIR=/usr/local/lib/scrape-api
# Where `systemctl set-property --runtime` (the watcher) keeps what it adds.
RUNTIME_DROPIN=/run/systemd/system.control/scrape-api.service.d
SYS_PY=/usr/bin/python3
OPT_DIR=/opt/scrape-api
UV_VERSION=0.12.23

usage() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; }
step() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) [[ $# -ge 2 ]] || die "--port needs a value"; PORT="$2"; shift 2 ;;
    --public-url) [[ $# -ge 2 ]] || die "--public-url needs a value"; PUBLIC_URL_ARG="${2%/}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done
[[ -z "$PORT" || ( "$PORT" =~ ^[0-9]{1,5}$ && "$PORT" -ge 1 && "$PORT" -le 65535 ) ]] || die "invalid --port: $PORT"
[[ -z "$PUBLIC_URL_ARG" || "$PUBLIC_URL_ARG" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] ||
  die "invalid --public-url: $PUBLIC_URL_ARG (expected https://host[:port], no path)"

[[ $EUID -ne 0 ]] || die "run as the normal user (with sudo rights), not as root"
command -v systemctl >/dev/null || die "systemd is required"
sudo -n true 2>/dev/null || sudo -v || die "sudo is required"
[[ -x "$SYS_PY" && "$(stat -L -c %u "$SYS_PY")" == 0 ]] &&
  "$SYS_PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
  die "needs the system's $SYS_PY (3.10 or newer, owned by root), e.g. apt install python3"
NETCHECK=("$SYS_PY" -I "$LIB_DIR/netcheck.py")

env_get() { sudo sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; }
env_set() {  # values are checked by the callers: digits or a plain http(s) URL
  local key="$1" value="$2"
  if sudo grep -q "^$key=" "$ENV_FILE"; then
    sudo sed -i "s|^$key=.*|$key=$value|" "$ENV_FILE"
  else
    echo "$key=$value" | sudo tee -a "$ENV_FILE" >/dev/null
  fi
}

# Python, the virtualenv's interpreter and the browser live outside the login
# user's home: the service runs as its own user and cannot see that home.
sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 755 "$OPT_DIR"
export UV_PYTHON_INSTALL_DIR="$OPT_DIR/python"
export PLAYWRIGHT_BROWSERS_PATH="$OPT_DIR/browsers"

step "uv"
if ! command -v uv >/dev/null; then
  curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv --version

step "Python environment"
cd "$APP_DIR"
uv sync --frozen --no-dev --no-editable --quiet
PY="$APP_DIR/.venv/bin/python"

step "Pre-flight import"
"$PY" -c 'import scrape_api.main' || die "the new release does not import; the service was not touched"

step "Chromium for Playwright ($PLAYWRIGHT_BROWSERS_PATH)"
"$APP_DIR/.venv/bin/playwright" install chromium
launch_ok() { "$PY" -c 'from playwright.sync_api import sync_playwright
with sync_playwright() as p: p.chromium.launch(headless=True).close()' >/dev/null 2>&1; }
if ! launch_ok; then
  command -v apt-get >/dev/null ||
    die "Chromium does not start; install its system libraries with this distribution's package manager, then rerun"
  echo "Chromium does not start yet; installing its system libraries"
  sudo "$APP_DIR/.venv/bin/playwright" install-deps chromium
  launch_ok || die "Chromium still does not start"
fi

step "Configuration ($ENV_FILE)"
if ! sudo test -f "$ENV_FILE"; then
  sudo install -d -m 755 "$(dirname "$ENV_FILE")"
  key="$("$PY" -c 'import secrets; print("sk-" + secrets.token_urlsafe(32))')"
  sudo install -m 600 /dev/null "$ENV_FILE"
  sudo tee "$ENV_FILE" >/dev/null <<EOF
# scrape-api settings (read by systemd; restart the service after editing).
# Comma-separated API keys accepted as "Authorization: Bearer <key>".
SCRAPE_API_KEYS=$key
# Optional upstream proxies, comma-separated, e.g. http://user:pass@p.webshare.io:80
# Used only when a request asks proxy=stealth, or proxy=auto and the direct fetch is blocked.
SCRAPE_PROXY_URLS=
SCRAPE_HOST=127.0.0.1
SCRAPE_PORT=${PORT:-3002}
SCRAPE_MAX_CONCURRENCY=4
SCRAPE_MAX_CRAWL_PAGES=500
EOF
  echo "created with a new API key"
else
  echo "kept existing settings and API key"
fi
[[ -n "$PORT" ]] && env_set SCRAPE_PORT "$PORT"
[[ -n "$PUBLIC_URL_ARG" ]] && env_set SCRAPE_PUBLIC_URL "$PUBLIC_URL_ARG"
PORT="$(env_get SCRAPE_PORT)"
PORT="${PORT:-3002}"
[[ "$PORT" =~ ^[0-9]{1,5}$ ]] || die "SCRAPE_PORT in $ENV_FILE is not a port: $PORT"

step "systemd units"
# Keep the units now in service, and the watcher code they run, to roll back if
# the new release does not come up.
BACKUP="$(mktemp -d)"
mkdir "$BACKUP/units"
for f in "$UNIT_FILE" "$SOCKET_FILE" "$WATCH_FILE"; do
  if sudo test -f "$f"; then sudo cp "$f" "$BACKUP/units/"; fi
done
if sudo test -f "$LIB_DIR/netcheck.py"; then sudo cp "$LIB_DIR/netcheck.py" "$BACKUP/"; fi
OLD_PORT_HOLDER=""
sudo test -f "$SOCKET_FILE" || OLD_PORT_HOLDER=1  # an older unit listened on the port itself

# The service may not reach loopback, private networks or this machine's own
# prefixes (its addresses, the /64 around each IPv6 one). DNS resolvers on a
# private network (a VPC resolver, a home router) are allowed, because allowing an
# address opens all of its ports; resolvers on loopback, link-local or this
# machine's prefixes never are. On systemd-resolved hosts the service reads
# resolved's upstream servers instead of its 127.0.0.53 stub.
NET="$("$SYS_PY" -I "$APP_DIR/deploy/netcheck.py" filter-config)" || die "could not work out the network filter"
net_get() { sed -n "s/^$1=//p" <<<"$NET"; }
RESOLV_BIND="$(net_get resolv_conf)"
ALLOW="$(net_get allow)"
DENY="$(net_get deny)"
[[ -n "$DENY" ]] || die "empty IPAddressDeny list"
[[ -z "$RESOLV_BIND" ]] || echo "the service reads DNS servers from $RESOLV_BIND"
[[ -z "$ALLOW" ]] || echo "allowing private DNS resolvers: $ALLOW"
UNIT_DNS=""
[[ -z "$RESOLV_BIND" ]] || UNIT_DNS+="BindReadOnlyPaths=$RESOLV_BIND:/etc/resolv.conf"$'\n'
[[ -z "$ALLOW" ]] || UNIT_DNS+="# DNS only: the private resolvers named in /etc/resolv.conf"$'\n'"IPAddressAllow=$ALLOW"$'\n'

sudo install -D -o root -g root -m 644 "$APP_DIR/deploy/netcheck.py" "$LIB_DIR/netcheck.py"

# The socket unit owns the port and hands it to the service (socket activation).
# Access lists of a service do not apply to sockets passed in (systemd.resource-
# control), so the IPAddressDeny below leaves inbound requests alone while every
# connection the service or its browser opens is checked, loopback included.
sudo tee "$SOCKET_FILE" >/dev/null <<EOF
[Unit]
Description=scrape-api listening socket

[Socket]
ListenStream=127.0.0.1:$PORT
Backlog=2048
NoDelay=true

[Install]
WantedBy=sockets.target
EOF
sudo tee "$UNIT_FILE" >/dev/null <<EOF
[Unit]
Description=scrape-api (Firecrawl-compatible scraping API + MCP)
Requires=scrape-api.socket
# Never runs without the watcher that keeps this machine's new addresses denied.
BindsTo=scrape-api-addresses.service
After=scrape-api.socket scrape-api-addresses.service network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/scrape-api
Restart=always
RestartSec=3

# The browser renders untrusted pages. Run it sandboxed, as a throwaway user
# that cannot read the login user's home (agent credentials), see other users'
# processes, reach Docker, or connect to loopback or private networks.
Environment=CRAWL4AI_CHROMIUM_SANDBOX=true
DynamicUser=yes
StateDirectory=scrape-api
CacheDirectory=scrape-api
Environment=HOME=/var/lib/scrape-api
Environment=TMPDIR=/var/cache/scrape-api
Environment=PLAYWRIGHT_BROWSERS_PATH=$PLAYWRIGHT_BROWSERS_PATH
TemporaryFileSystem=$HOME:ro
BindReadOnlyPaths=$APP_DIR
InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock
ProtectProc=invisible
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
LockPersonality=yes
IPAddressDeny=$DENY
${UNIT_DNS}MemoryMax=60%
TasksMax=2048

[Install]
WantedBy=multi-user.target
EOF
# The deny list above is fixed at install time, but addresses change: a VM may get
# a new IPv6 when it resumes, without any restart. The watcher adds each new prefix
# to the running service's filter, through the kernel netlink address events.
sudo tee "$WATCH_FILE" >/dev/null <<EOF
[Unit]
Description=scrape-api: deny the service this machine's addresses as they change
Before=scrape-api.service

[Service]
Type=notify
ExecStart=$SYS_PY -I $LIB_DIR/netcheck.py watch scrape-api.service
Restart=on-failure
RestartSec=5
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
NoNewPrivileges=yes
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX AF_NETLINK
IPAddressDeny=any
MemoryMax=64M

[Install]
WantedBy=multi-user.target
EOF
# What the watcher added repeats the whole list, so it would hide the list written
# above; when it restarts, the watcher adds back whatever is still missing. Stop
# the units first, so that no watcher writes it again before the reload.
sudo systemctl stop scrape-api.socket scrape-api-addresses.service scrape-api.service 2>/dev/null || true
sudo rm -rf "$RUNTIME_DROPIN"
sudo systemctl daemon-reload
sudo systemctl enable --quiet scrape-api-addresses.service scrape-api.socket scrape-api.service

healthy() {
  for _ in $(seq 1 90); do
    curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}
# The private-network protection rests on the kernel filter. systemd starts the
# service even where it cannot install it (no BPF, cgroup v1), so check it: from
# inside the service's cgroup, a listener that answers outside it must be out of
# reach on loopback and on this machine's own addresses (see deploy/netcheck.py).
filter_active() {
  local cg
  cg="$(systemctl show -p ControlGroup --value scrape-api.service)"
  [[ -n "$cg" ]] || return 1
  sudo "${NETCHECK[@]}" probe "/sys/fs/cgroup$cg"
}
# Name resolution as the service sees it: its cgroup and its mount namespace,
# where its own resolv.conf may be bound.
resolves_inside() {
  local cg pid
  cg="$(systemctl show -p ControlGroup --value scrape-api.service)"
  pid="$(systemctl show -p MainPID --value scrape-api.service)"
  [[ -n "$cg" && "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  sudo CG="/sys/fs/cgroup$cg" PID="$pid" bash -c \
    'echo $$ > "$CG/cgroup.procs" && exec nsenter -t "$PID" -m getent hosts example.com' >/dev/null 2>&1
}
remove_units() {
  sudo systemctl disable --quiet scrape-api.service scrape-api.socket scrape-api-addresses.service 2>/dev/null || true
  sudo rm -f "$UNIT_FILE" "$SOCKET_FILE" "$WATCH_FILE"
  sudo rm -rf "$RUNTIME_DROPIN"
  sudo systemctl daemon-reload
}
watcher_active() { systemctl is-active --quiet scrape-api-addresses.service; }
# Put the previous units back if they still pass every check, the address watcher
# included. Otherwise, and on a first install, remove the units: a later run must
# not restore a unit that never proved its filter. errexit still applies in here,
# so the steps that may fail only feed the checks.
rollback() {
  sudo journalctl -u scrape-api -u scrape-api-addresses -n 40 --no-pager >&2 || true
  sudo systemctl stop scrape-api.service scrape-api.socket scrape-api-addresses.service || true
  if ls "$BACKUP/units/"* >/dev/null 2>&1; then
    echo "rolling back to the previous units" >&2
    sudo rm -f "$SOCKET_FILE" "$WATCH_FILE"
    sudo cp "$BACKUP/units/"* /etc/systemd/system/
    if sudo test -f "$BACKUP/netcheck.py"; then sudo cp "$BACKUP/netcheck.py" "$LIB_DIR/netcheck.py"; fi
    sudo rm -rf "$RUNTIME_DROPIN"
    sudo systemctl daemon-reload
    if sudo test -f "$WATCH_FILE"; then sudo systemctl start scrape-api-addresses.service || true; fi
    if sudo test -f "$SOCKET_FILE"; then sudo systemctl start scrape-api.socket || true; fi
    sudo systemctl start scrape-api.service || true
    if healthy && filter_active && watcher_active; then die "$1; the previous release is back in service"; fi
    echo "the previous units fail the checks too; removing them" >&2
    sudo systemctl stop scrape-api.service scrape-api.socket scrape-api-addresses.service || true
  else
    echo "first install: removing the new units" >&2
  fi
  remove_units
  die "$1"
}
# Free the port from an older unit, then bind it through the socket unit. The
# watcher goes first: the service is bound to it and starts after its first check.
if [[ -n "$OLD_PORT_HOLDER" ]]; then sudo systemctl stop scrape-api.service; fi
{ sudo systemctl restart scrape-api-addresses.service && sudo systemctl restart scrape-api.socket &&
  sudo systemctl restart scrape-api.service; } || rollback "the new units did not start"

step "Health check"
healthy || rollback "the new release did not become healthy on port $PORT"
curl -fsS "http://127.0.0.1:$PORT/health"; echo

step "Private-network filter"
filter_active || rollback "the kernel IP filter does not hold for scrape-api (needs cgroup v2 with BPF); refusing to run without it"
echo "a process in the service's cgroup cannot reach loopback or this machine's addresses: ok"
watcher_active || rollback "the address watcher is not running"
resolves_inside || echo "warning: name resolution fails inside the service's cgroup; check /etc/resolv.conf" >&2
rm -rf "$BACKUP"

PUBLIC_URL="$(env_get SCRAPE_PUBLIC_URL)"
BASE="${PUBLIC_URL:-http://127.0.0.1:$PORT}"
cat <<EOF

scrape-api is running from $APP_DIR
  Local:   http://127.0.0.1:$PORT
  Public:  ${PUBLIC_URL:-(none: it listens on 127.0.0.1 only; put an HTTPS proxy or tunnel in front, then rerun with --public-url)}
  API key: sudo sed -n 's/^SCRAPE_API_KEYS=//p' $ENV_FILE
  Logs:    journalctl -u scrape-api -u scrape-api-addresses -f

Add it to Claude Code on any machine:
  claude mcp add --scope user --transport http scrape $BASE/mcp --header "Authorization: Bearer <key>"
EOF
