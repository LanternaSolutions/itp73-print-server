#!/bin/bash
# install-macos.sh — Set up the ITP-73 print server on a Mac.
#
# Can be run locally or remotely over SSH. Idempotent — safe to re-run if
# something fails partway through. Re-running also picks up updates to
# server.py / requirements.txt.
#
# What it does:
#   1. Installs Homebrew (with Xcode CLI tools) if missing
#   2. Installs python@3.12 and libusb via Homebrew
#   3. Copies server files to ~/Library/Application Support/itp73-print-server
#   4. Creates a venv and installs Python deps
#   5. Detects the printer's USB IDs (interactive prompt the first time)
#   6. Installs a launchd agent so the server auto-starts at every login
#   7. Drops a Printer.webloc shortcut on the Desktop

set -euo pipefail

INSTALL_DIR="${HOME}/Library/Application Support/itp73-print-server"
LAUNCHD_PLIST="${HOME}/Library/LaunchAgents/com.itp73.printserver.plist"
LOG_FILE="${HOME}/Library/Logs/itp73.log"
SERVICE_LABEL="com.itp73.printserver"
DESKTOP_WEBLOC="${HOME}/Desktop/Printer.webloc"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
dim()   { printf "\033[2m%s\033[0m\n" "$*"; }
ok()    { printf "\033[32m✓\033[0m %s\n" "$*"; }
warn()  { printf "\033[33m⚠\033[0m %s\n" "$*"; }
die()   { printf "\033[31m✗\033[0m %s\n" "$*"; exit 1; }

echo
bold "── ITP-73 print server installer ──────────────────────────────"
dim  "  Install dir:    $INSTALL_DIR"
dim  "  LaunchAgent:    $LAUNCHD_PLIST"
dim  "  Logs:           $LOG_FILE"
echo

# ── 1. Sanity ──────────────────────────────────────────────────────────────
[[ "$(uname)" == "Darwin" ]] || die "This installer is for macOS only. (Got $(uname).)"

# ── 2. Xcode CLI tools ─────────────────────────────────────────────────────
if ! xcode-select -p &>/dev/null; then
    bold "→ Installing Xcode Command Line Tools..."
    warn "A GUI installer will pop up on the Mac. Wait for it to finish, then re-run this script."
    xcode-select --install || true
    exit 0
fi
ok "Xcode CLI tools present"

# ── 3. Homebrew ────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
    bold "→ Installing Homebrew..."
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
# Make brew available in this shell whether on Apple Silicon or Intel.
if   [[ -x /opt/homebrew/bin/brew ]]; then eval "$(/opt/homebrew/bin/brew shellenv)"
elif [[ -x /usr/local/bin/brew    ]]; then eval "$(/usr/local/bin/brew shellenv)"
else die "Homebrew install seemed to succeed but brew isn't on PATH."
fi
ok "Homebrew at $(which brew)"

# ── 4. Python & libusb ─────────────────────────────────────────────────────
bold "→ Installing python@3.12 and libusb..."
brew install python@3.12 libusb >/dev/null
PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
[[ -x "$PYTHON" ]] || PYTHON="$(brew --prefix)/bin/python3.12"
[[ -x "$PYTHON" ]] || die "Couldn't find python3.12 after install."
ok "Python at $PYTHON ($($PYTHON --version))"

# ── 5. Copy files into place ───────────────────────────────────────────────
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/images"
cp -f "$SCRIPT_DIR/server.py"        "$INSTALL_DIR/server.py"
cp -f "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
ok "Files copied to $INSTALL_DIR"

# ── 6. Virtualenv + deps ───────────────────────────────────────────────────
bold "→ Creating venv and installing Python deps..."
if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
    "$PYTHON" -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
ok "Deps installed"

# ── 7. Detect the printer ──────────────────────────────────────────────────
if [[ ! -f "$INSTALL_DIR/config.json" ]]; then
    echo
    bold "→ Detecting the printer on USB..."
    dim  "  Make sure the ITP-73 is powered on and plugged in to the Mac."
    echo
    "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/server.py" --list-usb || true
    echo
    read -r -p "  Printer Vendor ID  (e.g. 0x0416): " VID
    read -r -p "  Printer Product ID (e.g. 0x5011): " PID
    cat > "$INSTALL_DIR/config.json" <<EOF
{
  "printer_vendor_id":  "$VID",
  "printer_product_id": "$PID"
}
EOF
    ok "Wrote $INSTALL_DIR/config.json"
else
    ok "Found existing config.json (keeping it; delete it to re-detect)"
fi

# ── 8. launchd agent (auto-start at login) ─────────────────────────────────
bold "→ Installing launchd agent..."
mkdir -p "${HOME}/Library/LaunchAgents" "${HOME}/Library/Logs"
cat > "$LAUNCHD_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$SERVICE_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$INSTALL_DIR/.venv/bin/python</string>
    <string>$INSTALL_DIR/server.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$INSTALL_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$(brew --prefix)/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>5</integer>
  <key>StandardOutPath</key>
  <string>$LOG_FILE</string>
  <key>StandardErrorPath</key>
  <string>$LOG_FILE</string>
</dict>
</plist>
EOF

launchctl unload "$LAUNCHD_PLIST" 2>/dev/null || true
launchctl load "$LAUNCHD_PLIST"
ok "Service loaded — will auto-start at every login"

# Wait a beat, then check it's actually up.
sleep 2
if launchctl list | grep -q "$SERVICE_LABEL"; then
    if curl -sf -o /dev/null --max-time 3 "http://localhost:8080/images-list"; then
        ok "Server is responding on http://localhost:8080"
    else
        warn "Service is loaded but the HTTP port isn't responding yet. Check: tail -f $LOG_FILE"
    fi
else
    warn "Service didn't register. Check: launchctl list | grep $SERVICE_LABEL"
fi

# ── 9. Desktop shortcut ────────────────────────────────────────────────────
cat > "$DESKTOP_WEBLOC" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>URL</key>
  <string>http://localhost:8080</string>
</dict>
</plist>
EOF
ok "Desktop shortcut: Printer.webloc"

echo
bold "── Done ───────────────────────────────────────────────────────"
echo "  • Double-click 'Printer' on the Desktop to open the print UI."
echo "  • Add images to:  $INSTALL_DIR/images/"
echo "  • Tail logs with: tail -f $LOG_FILE"
echo "  • Restart with:   launchctl unload $LAUNCHD_PLIST && launchctl load $LAUNCHD_PLIST"
echo "  • Re-detect IDs:  rm $INSTALL_DIR/config.json && bash $(basename "$0")"
echo
