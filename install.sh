#!/bin/sh
# ace-autofeed installer for Rinkhals
#
# One-liner install from your dev machine:
#   ssh root@<printer-ip> 'curl -fsSL https://raw.githubusercontent.com/sharpmonk/ace-autofeed-rinkhals/main/install.sh | sh'
#
# What this does:
#   1. Fetches ace-autofeed.py to /useremain/home/rinkhals/ace-autofeed/
#   2. Migrates from any legacy wrong-path install (/useremain/rinkhals/apps/...)
#   3. Sets up a proper Rinkhals user app at /useremain/home/rinkhals/apps/99-ace-autofeed/
#      with app.sh / app.json / .enabled — survives reboots and Rinkhals upgrades
#   4. Stops any existing instance
#   5. Starts the daemon via the new app.sh
#   6. Verifies it's running

set -e

REPO_URL="https://raw.githubusercontent.com/sharpmonk/ace-autofeed-rinkhals/main"
INSTALL_DIR="/useremain/home/rinkhals/ace-autofeed"
APP_DIR="/useremain/home/rinkhals/apps/99-ace-autofeed"
LEGACY_APP_DIR="/useremain/rinkhals/apps/99-ace-autofeed"
LOG_FILE="$INSTALL_DIR/ace-autofeed.log"

echo "=== ace-autofeed installer ==="
echo

# 1) Sanity checks
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must run as root (Rinkhals default: user root, password rockchip)"
    exit 1
fi
if [ ! -d /useremain ]; then
    echo "ERROR: /useremain not found — is this a Rinkhals-equipped Kobra?"
    exit 1
fi
if [ ! -d /useremain/home/rinkhals ]; then
    echo "ERROR: /useremain/home/rinkhals not found — Rinkhals is not installed"
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not available on this printer"
    exit 1
fi
if ! curl -s -m 4 -o /dev/null -w '%{http_code}' http://127.0.0.1:7125/server/info | grep -q '^200$'; then
    echo "WARNING: Moonraker not reachable on :7125 — is Rinkhals running?"
    echo "         The daemon will still install, but won't be useful until Rinkhals is up."
fi

# 2) Fetch the daemon
echo "[1/5] downloading ace-autofeed.py..."
mkdir -p "$INSTALL_DIR"
if ! curl -fsSL "$REPO_URL/ace-autofeed.py" -o "$INSTALL_DIR/ace-autofeed.py"; then
    echo "ERROR: failed to download ace-autofeed.py from $REPO_URL"
    exit 1
fi
chmod +x "$INSTALL_DIR/ace-autofeed.py"
echo "       installed at $INSTALL_DIR/ace-autofeed.py"

# 3) Stop any running instance (idempotent re-install).
# /proc/<pid>/cmdline scan is the reliable way on busybox (no pgrep/pkill/pidof).
echo "[2/5] stopping any existing daemon..."
killed=0
for cmdline in /proc/[0-9]*/cmdline; do
    if grep -q "ace-autofeed.py" "$cmdline" 2>/dev/null; then
        pid=$(basename "$(dirname "$cmdline")")
        kill -9 "$pid" 2>/dev/null && killed=$((killed + 1))
    fi
done
[ "$killed" -gt 0 ] && echo "       killed $killed existing instance(s)"
sleep 2

# 4) Migrate from any legacy wrong-path install
if [ -d "$LEGACY_APP_DIR" ]; then
    echo "[3/5] migrating from legacy install at $LEGACY_APP_DIR..."
    rm -rf "$LEGACY_APP_DIR"
    rmdir "$(dirname "$LEGACY_APP_DIR")" 2>/dev/null || true
    echo "       removed legacy app dir"
else
    echo "[3/5] no legacy install to migrate"
fi

# 5) Install proper Rinkhals app for auto-start
echo "[4/5] installing Rinkhals app for auto-start at $APP_DIR..."
mkdir -p "$APP_DIR"

cat > "$APP_DIR/app.sh" <<'APPSH'
. /useremain/rinkhals/.current/tools.sh

APP_ROOT=$(dirname $(realpath $0))
DAEMON=/useremain/home/rinkhals/ace-autofeed/ace-autofeed.py
LOG=/useremain/home/rinkhals/ace-autofeed/ace-autofeed.log
PIDFILE=/tmp/rinkhals/ace-autofeed.pid

status() {
    PID=$(cat $PIDFILE 2> /dev/null)
    if [ "$PID" == "" ]; then
        report_status $APP_STATUS_STOPPED
        return
    fi
    PS=$(ps | grep $PID | grep -v grep)
    if [ "$PS" == "" ]; then
        report_status $APP_STATUS_STOPPED
        return
    fi
    report_status $APP_STATUS_STARTED $PID
}

start() {
    stop
    mkdir -p /tmp/rinkhals
    nohup python3 $DAEMON --log-file $LOG > /dev/null 2>&1 &
    PID=$!
    echo $PID > $PIDFILE
}

stop() {
    PID=$(cat $PIDFILE 2> /dev/null)
    if [ -n "$PID" ]; then
        kill_by_id $PID 2> /dev/null
    fi
    # Belt and braces: also kill by command line in case PID file is stale
    for cmdline in /proc/[0-9]*/cmdline; do
        if grep -q "ace-autofeed.py" "$cmdline" 2>/dev/null; then
            pid=$(basename "$(dirname "$cmdline")")
            kill "$pid" 2>/dev/null || true
        fi
    done
    rm $PIDFILE 2> /dev/null
}

case "$1" in
    status) status ;;
    start) start ;;
    stop) stop ;;
    *) echo "Usage: $0 {status|start|stop}" >&2; exit 1 ;;
esac
APPSH

cat > "$APP_DIR/app.json" <<'APPJSON'
{
    "$version": "1",
    "name": "ACE Autofeed",
    "description": "Auto-trigger FEED_FILAMENT and inject LEVIQ when prints start via Mainsail/Fluidd/Orca on Anycubic + ACE Pro setups.",
    "version": "0.8"
}
APPJSON

touch "$APP_DIR/.enabled"
chmod +x "$APP_DIR/app.sh"
echo "       app installed (app.sh, app.json, .enabled)"

# 6) Start it now via the new app.sh
echo "[5/5] starting daemon..."
sh "$APP_DIR/app.sh" start
sleep 2

# /proc-scan: reliable way to find the daemon's PID on busybox
PID=""
for cmdline in /proc/[0-9]*/cmdline; do
    if grep -q "ace-autofeed.py" "$cmdline" 2>/dev/null; then
        PID=$(basename "$(dirname "$cmdline")")
        break
    fi
done

if [ -n "$PID" ]; then
    echo "       running (PID $PID)"
    echo
    echo "=== install complete ==="
    echo "Log:       $LOG_FILE"
    echo "Status:    sh $APP_DIR/app.sh status"
    echo "Stop:      sh $APP_DIR/app.sh stop"
    echo "Restart:   sh $APP_DIR/app.sh start"
    echo "Tail log:  tail -f $LOG_FILE"
    echo "Uninstall: rm -rf $INSTALL_DIR $APP_DIR"
    echo
    echo "Daemon will now auto-start when Rinkhals boots."
else
    echo "       WARNING: daemon doesn't appear to be running. Check log:"
    echo "         cat $LOG_FILE"
    exit 1
fi
