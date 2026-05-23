# ITP-73 Print Server

A minimal web-based print UI for the **Premier ITP-73** thermal receipt printer on macOS.

Open `http://localhost:8080` in any browser on the Mac itself (or any device on the same network), pick an image, view the preview, and tap **PRINT**.

```
  ┌──────────────────┐
  │    macOS host    │
  │                  │
  │  ┌────────────┐  │            localhost:8080
  │  │ server.py  ├──┼──────────► browser (UI)
  │  │ (launchd)  │  │
  │  └────────────┘  │
  │       USB        │
  └────────┬─────────┘
           │
     ITP-73 Printer
```

## Features

- One-tap print from any browser (desktop or mobile)
- Drag-and-drop image upload
- Live dithered preview shows exactly what will come out of the printer
- Recent print history with remove buttons
- Mock mode for development without a printer (prints saved as PNG files)
- Optional in-app update button (pulls latest `server.py` from GitHub)

---

## Quick start — macOS

1. **Clone this repo** and plug the ITP-73 into a USB port.

2. **Run the installer:**
   ```bash
   bash install-macos.sh
   ```
   The installer will list all connected USB devices and prompt you for your printer's Vendor ID and Product ID. Look for the ITP-73 in the list and enter its two hex values.

3. **Open the UI** — double-click the **Printer** shortcut on the Desktop, or go to `http://localhost:8080`.

The server runs as a background launchd service and restarts automatically at login. No terminal window needed after install.

> **Remote install over SSH:** If you're setting this up on someone else's Mac, copy the repo with `scp -r` and run the installer over SSH. To upgrade an existing install, pull the latest repo on the Mac and re-run `bash install-macos.sh` — it's idempotent and will update the files in place.

---

## Dev setup (mock mode)

No printer required — every print is saved as a PNG to `mock_output/` showing exactly what would have come out.

```bash
# macOS / Linux
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

MOCK_PRINTER=1 python server.py
```

Open `http://localhost:8080`. The status pill will show **MOCK**.

To test with the real printer on Linux (macOS doesn't need sudo):

```bash
python server.py --list-usb
PRINTER_VENDOR_ID=0x... PRINTER_PRODUCT_ID=0x... sudo python server.py
```

---

## Configuration

All config lives in `config.json` in the same directory as `server.py`. The installer creates it; you can edit it manually and restart the service.

| Key | Default | Description |
|---|---|---|
| `printer_vendor_id` | `0x0416` | USB Vendor ID (hex string or integer) |
| `printer_product_id` | `0x5011` | USB Product ID (hex string or integer) |
| `print_width_dots` | `576` | Print head width in dots (80 mm paper) |
| `port` | `8080` | HTTP port |
| `github_repo` | — | `owner/repo` — enables the in-app update button |
| `github_branch` | `main` | Branch to pull updates from |
| `github_file` | `server.py` | Path to `server.py` within the repo |

Environment variables (uppercased) override `config.json`.

### In-app update button

Set `github_repo` in `config.json` and an **Update app** button appears. Tapping it fetches the latest `server.py` from GitHub and restarts the server automatically via launchd KeepAlive. Requires a public repository.

---

## File layout

```
itp73-print-server/        (repo)
├── server.py
├── requirements.txt
├── install-macos.sh
└── README.md
```

After install on macOS:

```
~/Library/Application Support/itp73-print-server/
├── server.py
├── requirements.txt
├── config.json             # written by installer; holds VID/PID
├── .venv/
├── images/                 # drop print-ready images here
├── mock_output/            # only written when MOCK_PRINTER=1
└── .current_image

~/Library/LaunchAgents/com.itp73.printserver.plist   # auto-start
~/Library/Logs/itp73.log                             # server output
~/Desktop/Printer.webloc                             # Desktop shortcut
```

---

## Operating the server

All of these operations are available from the **Settings panel** (⚙) in the web UI — no terminal needed.

For SSH access or scripting, the equivalent terminal commands:

| Task | Command |
|---|---|
| Watch logs live | `tail -f ~/Library/Logs/itp73.log` |
| Restart | `launchctl unload ~/Library/LaunchAgents/com.itp73.printserver.plist && launchctl load ~/Library/LaunchAgents/com.itp73.printserver.plist` |
| Stop until next reboot | `launchctl unload ~/Library/LaunchAgents/com.itp73.printserver.plist` |
| Check it's running | `launchctl list \| grep com.itp73.printserver` |
| Re-detect printer IDs | `rm ~/Library/Application\ Support/itp73-print-server/config.json && bash install-macos.sh` |
| Remove everything | `launchctl unload ~/Library/LaunchAgents/com.itp73.printserver.plist && rm -rf ~/Library/Application\ Support/itp73-print-server ~/Library/LaunchAgents/com.itp73.printserver.plist ~/Desktop/Printer.webloc` |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Browser shows "can't connect" | Service not running — check `tail -f ~/Library/Logs/itp73.log` |
| "Printer not found on USB" | Check power and cable; re-run installer to re-detect IDs |
| Image prints only partial width | Source image is narrower than 576 dots — this is normal; it prints left-aligned |
| Image prints with garbage/Errno 5 | Unplug USB, wait 5 seconds, plug back in |
| Want a different port | Edit `config.json` → `"port": 8081`, restart service, update `Printer.webloc` |
| Phone can't reach the Mac | Same Wi-Fi? Try the IP from `ipconfig getifaddr en0` instead of `<hostname>.local` |

---

## Notes on image prep

- Print width is **576 dots** (80 mm paper). The server scales images down automatically; narrower images print left-aligned.
- High contrast — black on white — prints sharpest. Gradients dither into noise (fine for photos, bad for text).
- For sharp text in images, use 24 px+ bold in the source file.
