# ITP-73 Print Server

**A minimal web UI for the Premier ITP-73 thermal receipt printer on macOS.**

Open `http://localhost:8080` in any browser on the same network, pick an image, and tap **PRINT**. Works from a phone, tablet, or desktop without installing anything on the client.

![macOS](https://img.shields.io/badge/macOS-000000?logo=apple&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.x-000000?logo=flask&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

```
  ┌──────────────────┐
  │    macOS host    │
  │                  │
  │  ┌────────────┐  │        localhost:8080
  │  │ server.py  ├──┼──────────► browser (UI)
  │  │ (launchd)  │  │
  │  └────────────┘  │
  │       USB        │
  └────────┬─────────┘
           │
     ITP-73 Printer
```

---

## Features

- One-tap print from any browser on the local network (desktop or mobile)
- Drag-and-drop image upload
- Live dithered preview shows exactly what will come out of the printer
- Settings panel for restart, re-detect printer, and live log tail — no terminal needed
- Recent print history
- Mock mode for development without a printer (saves PNG output to `mock_output/`)
- Optional in-app update button (pulls latest `server.py` from a public GitHub repo)

---

## Quick start

**Requirements:** macOS, the ITP-73 plugged in via USB and powered on.

```bash
git clone https://github.com/LanternaSolutions/itp73-print-server
cd itp73-print-server
bash install-macos.sh
```

The installer:
1. Installs Homebrew, Python 3.12, and libusb if missing
2. Lists connected USB devices and prompts you for the printer's Vendor ID and Product ID
3. Installs a launchd service so the server starts automatically at every login
4. Drops a **Printer** shortcut on the Desktop

Once done, open the shortcut or go to `http://localhost:8080`.

> **Remote setup over SSH:** Copy the repo with `scp -r` and run the installer over SSH. To upgrade, pull the latest repo on the Mac and re-run `bash install-macos.sh` — it is idempotent and updates files in place.

---

## Dev / mock mode

No printer required. Every "print" is saved as a PNG to `mock_output/`.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
MOCK_PRINTER=1 python server.py
```

Open `http://localhost:8080`. The status pill shows **MOCK**.

To test USB detection on Linux:

```bash
python server.py --list-usb
PRINTER_VENDOR_ID=0x... PRINTER_PRODUCT_ID=0x... sudo python server.py
```

---

## Configuration

The installer writes `config.json` next to `server.py`. Edit it manually and restart the service to apply changes.

| Key | Default | Description |
|---|---|---|
| `printer_vendor_id` | `0x0416` | USB Vendor ID (hex string or integer) |
| `printer_product_id` | `0x5011` | USB Product ID (hex string or integer) |
| `print_width_dots` | `576` | Print head width in dots (80 mm paper) |
| `port` | `8080` | HTTP port |
| `github_repo` | unset | `owner/repo` — enables the in-app update button |
| `github_branch` | `main` | Branch to pull updates from |
| `github_file` | `server.py` | Path to `server.py` within the repo |

Environment variables (same keys, uppercased) override `config.json`.

### In-app update button

Set `github_repo` in `config.json` and an **Update app** button appears in the UI. Tapping it fetches the latest `server.py` from GitHub and restarts the server via launchd KeepAlive. Requires a public repository.

---

## File layout

```
itp73-print-server/           (repo)
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
├── config.json              # written by installer; holds VID/PID
├── .venv/
├── images/                  # drop print-ready images here
└── mock_output/             # only written when MOCK_PRINTER=1

~/Library/LaunchAgents/com.itp73.printserver.plist   # auto-start service
~/Library/Logs/itp73.log                             # server output
~/Desktop/Printer.webloc                             # Desktop shortcut
```

---

## Operating the server

All of these operations are available from the **Settings panel** in the web UI. For SSH access or scripting:

| Task | Command |
|---|---|
| Watch logs live | `tail -f ~/Library/Logs/itp73.log` |
| Restart | `launchctl unload ~/Library/LaunchAgents/com.itp73.printserver.plist && launchctl load ~/Library/LaunchAgents/com.itp73.printserver.plist` |
| Stop until next reboot | `launchctl unload ~/Library/LaunchAgents/com.itp73.printserver.plist` |
| Check it is running | `launchctl list \| grep com.itp73.printserver` |
| Re-detect printer IDs | `rm ~/Library/Application\ Support/itp73-print-server/config.json && bash install-macos.sh` |
| Remove everything | `launchctl unload ~/Library/LaunchAgents/com.itp73.printserver.plist && rm -rf ~/Library/Application\ Support/itp73-print-server ~/Library/LaunchAgents/com.itp73.printserver.plist ~/Desktop/Printer.webloc` |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Browser shows "can't connect" | Service not running. Check `tail -f ~/Library/Logs/itp73.log` |
| "Printer not found on USB" | Check power and cable, then re-run installer to re-detect IDs |
| Image prints at partial width | Source image is narrower than 576 dots. Normal behaviour; prints left-aligned |
| Image prints with garbage / Errno 5 | Unplug USB, wait 5 seconds, plug back in |
| Want a different port | Edit `config.json` and set `"port": 8081`, restart service, update `Printer.webloc` |
| Phone can't reach the Mac | Confirm same Wi-Fi. Try the IP from `ipconfig getifaddr en0` instead of `hostname.local` |

---

## Image prep

- Print width is **576 dots** (80 mm paper). The server scales images down automatically; narrower images print left-aligned.
- High contrast (black on white) prints sharpest. Gradients dither into noise — fine for photos, bad for text.
- For sharp text in images, use 24 px+ bold in the source file.

---

## License

MIT. See [LICENSE](LICENSE).
