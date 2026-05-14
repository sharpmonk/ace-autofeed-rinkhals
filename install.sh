#!/bin/sh
# ace-autofeed installer for Rinkhals
#
# One-liner install from your dev machine:
#   ssh root@<printer-ip> 'curl -fsSL https://raw.githubusercontent.com/sharpmonk/ace-autofeed-rinkhals/main/install.sh | sh'
#
# What this does:
#   1. Fetches ace-autofeed.py to /useremain/home/rinkhals/ace-autofeed/
#   2. Sets up a Rinkhals "app" at /useremain/rinkhals/apps/99-ace-autofeed/
#      so the daemon auto-starts at boot and survives Rinkhals upgrades
#   3. Stops any existing instance
#   4. Starts the daemon
#   5. Verifies it's running

set -e

REPO_URL="https://raw.githubusercontent.com/sharpmonk/ace-autofeed-rinkhals/main"
INSTALL_DIR="/useremain/home/rinkhals/ace-autofeed"
APP_DIR="/useremain/rinkhals/apps/99-ace-autofeed"
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
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not available on this printer"
    exit 1
fi
if ! curl -s -m 4 -o /dev/null -w '%{http_code}' http://127.0.0.1:7125/server/info | grep -q '^200$'; then
    echo "WARNING: Moonraker not reachable on :7125 — is Rinkhals running?"
    echo "         The daemon will still install, but may not be useful until Rinkhals is up."
fi

# 2) Fetch the daemon
echo "[1/4] downloading ace-autofeed.py..."
mkdir -p "$INSTALL_DIR"
if ! curl -fsSL "$REPO_URL/ace-autofeed.py" -o "$INSTALL_DIR/ace-autofeed.py"; then
    echo "ERROR: failed to download ace-autofeed.py from $REPO_URL"
    exit 1
fi
chmod +x "$INSTALL_DIR/ace-autofeed.py"
echo "       installed at $INSTALL_DIR/ace-autofeed.py"

# 3) Stop any running instance (idempotent re-install)
echo "[2/4] stopping any existing daemon..."
for pid in $(ps | awk '/ace-autofeed.py/ && !/grep/ && !/awk/ {print $1}'); do
    kill -9 "$pid" 2>/dev/null || true
done
sleep 1

# 4) Install Rinkhals app for auto-start
echo "[3/4] installing Rinkhals app for auto-start..."
mkdir -p "$APP_DIR"
cat > "$APP_DIR/start.sh" <<EOF
#!/bin/sh
# ace-autofeed auto-start hook (managed by Rinkhals apps system)
nohup python3 $INSTALL_DIR/ace-autofeed.py \\
  --log-file $LOG_FILE \\
  > /dev/null 2>&1 &
EOF
cat > "$APP_DIR/stop.sh" <<'EOF'
#!/bin/sh
for pid in $(ps | awk '/ace-autofeed.py/ && !/grep/ && !/awk/ {print $1}'); do
    kill "$pid" 2>/dev/null || true
done
EOF
chmod +x "$APP_DIR/start.sh" "$APP_DIR/stop.sh"
echo "       app installed at $APP_DIR"

# 5) Start it now
echo "[4/4] starting daemon..."
sh "$APP_DIR/start.sh"
sleep 2
if ps | grep -v grep | grep -q ace-autofeed.py; then
    PID=$(ps | awk '/ace-autofeed.py/ && !/grep/ {print $1; exit}')
    echo "       running (PID $PID)"
    echo
    echo "=== install complete ==="
    echo "Log: $LOG_FILE"
    echo "Status: tail -f $LOG_FILE"
    echo "Stop:   sh $APP_DIR/stop.sh"
    echo "Uninstall: rm -rf $INSTALL_DIR $APP_DIR"
else
    echo "       WARNING: daemon doesn't appear to be running. Check log:"
    echo "         cat $LOG_FILE"
    exit 1
fi
