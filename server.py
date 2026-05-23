"""
ITP-73 Print Server
-------------------
A Flask server that bridges a web UI to the Premier ITP-73 thermal printer
over USB via ESC/POS.

Runs the same on macOS (production) and Linux (development without a printer).

Config priority:    env vars  >  config.json  >  built-in defaults
Find your printer:  python server.py --list-usb
Dev without one:    MOCK_PRINTER=1 python server.py

Endpoints:
  GET  /                       The UI
  POST /print                  Print the current image
  POST /test-print             Print a built-in test pattern
  POST /upload                 Upload one or more images (multipart)
  POST /select                 Set the current image
  GET  /images-list            JSON: list of images + current selection
  GET  /stats                  JSON: print counter, recent history
  GET  /preview/<name>         Original image
  GET  /preview-dithered/<n>   Image as it would print (1-bit, resized)
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    render_template_string,
    request,
    send_file,
    send_from_directory,
)
from PIL import Image, ImageDraw, ImageFont
from werkzeug.utils import secure_filename

# ── PATHS & CONFIG ──────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
IMAGES_DIR = BASE_DIR / "images"
CURRENT_FILE = BASE_DIR / ".current_image"
MOCK_OUTPUT_DIR = BASE_DIR / "mock_output"
LOG_FILE = Path.home() / "Library" / "Logs" / "itp73.log"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.itp73.printserver.plist"

IMAGES_DIR.mkdir(exist_ok=True)
ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
MAX_HISTORY = 20

DEFAULTS = {
    "printer_vendor_id": 0x0416,
    "printer_product_id": 0x5011,
    "print_width_dots": 576,
    "port": 8080,
}


def _coerce_int(val, fallback):
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        v = val.strip()
        try:
            return int(v, 16) if v.lower().startswith("0x") else int(v)
        except ValueError:
            return fallback
    return fallback


_GITHUB_STRING_KEYS = ("github_repo", "github_branch", "github_file")
_GITHUB_DEFAULTS = {
    "github_repo":   None,
    "github_branch": "main",
    "github_file":   "server.py",
}


def _load_config():
    cfg = dict(DEFAULTS)
    cfg.update(_GITHUB_DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            file_cfg = json.loads(CONFIG_FILE.read_text())
            for key in DEFAULTS:
                if key in file_cfg:
                    cfg[key] = _coerce_int(file_cfg[key], cfg[key])
            for key in _GITHUB_STRING_KEYS:
                if key in file_cfg and file_cfg[key]:
                    cfg[key] = str(file_cfg[key])
        except Exception as e:
            print(f"⚠ Couldn't read {CONFIG_FILE.name}: {e}", file=sys.stderr)
    for key in DEFAULTS:
        if key.upper() in os.environ:
            cfg[key] = _coerce_int(os.environ[key.upper()], cfg[key])
    for key in _GITHUB_STRING_KEYS:
        if key.upper() in os.environ:
            cfg[key] = os.environ[key.upper()]
    return cfg


_cfg = _load_config()
PRINTER_VENDOR_ID = _cfg["printer_vendor_id"]
PRINTER_PRODUCT_ID = _cfg["printer_product_id"]
PRINT_WIDTH_DOTS = _cfg["print_width_dots"]
PORT = _cfg["port"]
GITHUB_REPO   = _cfg["github_repo"]
GITHUB_BRANCH = _cfg["github_branch"]
GITHUB_FILE   = _cfg["github_file"]
MOCK_PRINTER = os.environ.get("MOCK_PRINTER", "").lower() in ("1", "true", "yes")

print_lock = threading.Lock()
state_lock = threading.Lock()
app = Flask(__name__)


# ── STATE PERSISTENCE ──────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"counter": 0, "last_print": None, "history": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def record_print(label: str, kind: str = "image"):
    """Bump the counter and push an entry to the history."""
    with state_lock:
        state = load_state()
        state["counter"] = state.get("counter", 0) + 1
        state["last_print"] = datetime.now().isoformat()
        state.setdefault("history", []).insert(0, {
            "label": label,
            "kind": kind,                # "image" or "test"
            "at": state["last_print"],
            "mock": MOCK_PRINTER,
        })
        state["history"] = state["history"][:MAX_HISTORY]
        save_state(state)


# ── IMAGE STATE ─────────────────────────────────────────────────────────────

def list_images():
    return [
        p.name for p in sorted(IMAGES_DIR.iterdir())
        if p.is_file() and p.suffix.lower() in ALLOWED_EXTS
    ]


def current_image():
    if CURRENT_FILE.exists():
        name = CURRENT_FILE.read_text().strip()
        if (IMAGES_DIR / name).exists():
            return name
    imgs = list_images()
    return imgs[0] if imgs else None


def set_current_image(name: str):
    CURRENT_FILE.write_text(name)


def image_dimensions(name: str):
    """Returns (width, height, bytes) for an image, or None on error."""
    try:
        p = IMAGES_DIR / name
        with Image.open(p) as im:
            return (im.width, im.height, p.stat().st_size)
    except Exception:
        return None


# ── PRINTER ─────────────────────────────────────────────────────────────────

def open_printer():
    """Open the ITP-73 over USB. Lazy import so the server still boots when
    libusb isn't yet installed."""
    from escpos.printer import Usb
    return Usb(PRINTER_VENDOR_ID, PRINTER_PRODUCT_ID, profile="default")


def prepare_image_for_print(path: Path) -> Image.Image:
    """Resize/pad to PRINT_WIDTH_DOTS wide and dither to 1-bit.
    Images wider than the paper are scaled down; narrower images are
    left-padded on white so the printer always receives full-width rows."""
    img = Image.open(path)
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    if img.width > PRINT_WIDTH_DOTS:
        ratio = PRINT_WIDTH_DOTS / img.width
        img = img.resize((PRINT_WIDTH_DOTS, int(img.height * ratio)), Image.LANCZOS)
    if img.width < PRINT_WIDTH_DOTS:
        canvas = Image.new("RGB", (PRINT_WIDTH_DOTS, img.height), (255, 255, 255))
        canvas.paste(img.convert("RGB"), (0, 0))
        img = canvas
    return img.convert("L").convert("1")


def _send(image: Image.Image, label: str):
    """Inner sender, expects an already-dithered image."""
    if MOCK_PRINTER:
        MOCK_OUTPUT_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = secure_filename(label) or "print"
        out = MOCK_OUTPUT_DIR / f"print_{ts}_{safe}.png"
        image.save(out)
        print(f"[MOCK] Would print {label} → {out}")
        return
    p = open_printer()
    try:
        p.hw("INIT")          # ESC @ — clears any stale buffer in the printer
        time.sleep(0.1)       # let the printer process the reset before data arrives
        p.image(image)
        p.text("\n")
        p.cut(mode="PART")   # ITP-73 only does partial cuts
        time.sleep(0.4)       # partial cut is mechanical; wait before releasing USB
    finally:
        try:
            p.close()
        except Exception:
            pass


def send_to_printer(image_path: Path):
    with print_lock:
        prepared = prepare_image_for_print(image_path)
        _send(prepared, image_path.stem)


# ── TEST PATTERN ────────────────────────────────────────────────────────────

_FONT_CANDIDATES_BOLD = [
    # macOS
    "/System/Library/Fonts/SFNSDisplay-Bold.otf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]
_FONT_CANDIDATES_MONO = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.dfont",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
]


def _find_font(size: int, paths):
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def generate_test_pattern() -> Image.Image:
    """Build a one-off test pattern image at PRINT_WIDTH_DOTS wide.
    Includes timestamp, grayscale ramp, line-weight check, and an alphabet."""
    W = PRINT_WIDTH_DOTS
    margin = 24
    img = Image.new("L", (W, 1200), 255)
    d = ImageDraw.Draw(img)

    f_big   = _find_font(44, _FONT_CANDIDATES_BOLD)
    f_mid   = _find_font(22, _FONT_CANDIDATES_BOLD)
    f_mono  = _find_font(18, _FONT_CANDIDATES_MONO)
    f_small = _find_font(14, _FONT_CANDIDATES_MONO)

    y = margin

    # Bold header bar
    d.rectangle([0, y, W, y + 70], fill=0)
    d.text((W // 2, y + 12), "PRINT TEST", fill=255, font=f_big, anchor="mt")
    y += 80

    # Timestamp & device line
    d.text((W // 2, y), datetime.now().strftime("%Y-%m-%d  %H:%M:%S"),
           fill=0, font=f_mid, anchor="mt")
    y += 30
    d.text((W // 2, y), f"ITP-73 · {W} dots · 80 mm",
           fill=0, font=f_small, anchor="mt")
    y += 30

    d.line([(margin, y), (W - margin, y)], fill=0, width=2)
    y += 20

    # Grayscale ramp
    cells = 16
    cell_w = (W - 2 * margin) // cells
    for i in range(cells):
        v = int(255 * i / (cells - 1))
        d.rectangle(
            [margin + i * cell_w, y, margin + (i + 1) * cell_w, y + 36],
            fill=v,
        )
    y += 50

    # Line weights
    for w in [1, 2, 3, 4, 5]:
        d.line([(margin, y + w / 2), (W - margin - 60, y + w / 2)],
               fill=0, width=w)
        d.text((W - margin, y), f"{w}px", fill=0, font=f_small, anchor="rt")
        y += max(w, 10) + 6

    y += 10
    # Diagonals — verifies the printer's dot pitch
    diag_w = (W - 2 * margin) // 10
    for i in range(10):
        x = margin + i * diag_w
        d.line([(x, y), (x + diag_w, y + 50)], fill=0, width=1)
    y += 60

    # Type ladder
    for label, font in [("abcdefghijklmnopqrstuvwxyz", f_mono),
                        ("ABCDEFGHIJKLMNOPQRSTUVWXYZ", f_mono),
                        ("0123456789  !@#$%^&*()-+=", f_mono)]:
        d.text((margin, y), label, fill=0, font=font)
        y += 28

    y += 10
    d.line([(margin, y), (W - margin, y)], fill=0, width=2)
    y += 20

    # Footer
    d.rectangle([0, y, W, y + 50], fill=0)
    d.text((W // 2, y + 8), "OK · TEST PASS", fill=255, font=f_mid, anchor="mt")
    y += 70

    # Crop and dither
    img = img.crop((0, 0, W, y))
    return img.convert("1")


# ── ROUTES ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(
        UI_HTML,
        current=current_image(),
        mock=MOCK_PRINTER,
        github_repo=GITHUB_REPO,
        port=PORT,
        printer_vid=hex(PRINTER_VENDOR_ID),
        printer_pid=hex(PRINTER_PRODUCT_ID),
    )


@app.route("/print", methods=["POST"])
def do_print():
    name = current_image()
    if not name:
        return jsonify(ok=False, error="Nothing to print yet! Tap ‘Change image’ to pick one, or drop an image file onto the page."), 400
    try:
        send_to_printer(IMAGES_DIR / name)
        record_print(name, kind="image")
        return jsonify(ok=True, printed=name, mock=MOCK_PRINTER)
    except Exception as e:
        return jsonify(ok=False, error=_friendly_error(str(e))), 500


@app.route("/test-print", methods=["POST"])
def do_test_print():
    try:
        with print_lock:
            pattern = generate_test_pattern()
            _send(pattern, "test-pattern")
        record_print("Test pattern", kind="test")
        return jsonify(ok=True, mock=MOCK_PRINTER)
    except Exception as e:
        return jsonify(ok=False, error=_friendly_error(str(e))), 500


@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("file")
    if not files:
        return jsonify(ok=False, error="No image file came through — try uploading again."), 400
    uploaded = []
    rejected = []
    for f in files:
        if not f or not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXTS:
            rejected.append(f.filename)
            continue
        # secure_filename strips any path components
        safe = secure_filename(f.filename) or f"upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
        # Avoid clobbering existing files
        dest = IMAGES_DIR / safe
        i = 1
        while dest.exists():
            stem = Path(safe).stem
            dest = IMAGES_DIR / f"{stem}_{i}{ext}"
            i += 1
        f.save(dest)
        uploaded.append(dest.name)
    if uploaded:
        set_current_image(uploaded[0])
    return jsonify(
        ok=bool(uploaded),
        uploaded=uploaded,
        rejected=rejected,
        current=current_image(),
    )


@app.route("/select", methods=["POST"])
def select():
    payload = request.get_json(silent=True) or request.form
    name = payload.get("name") if hasattr(payload, "get") else None
    if not name or name not in list_images():
        return jsonify(ok=False, error="Couldn't find that image. Try reloading the page."), 400
    set_current_image(name)
    return jsonify(ok=True, current=name)


@app.route("/images-list")
def images_list():
    names = list_images()
    images = []
    for n in names:
        dims = image_dimensions(n)
        images.append({
            "name": n,
            "width": dims[0] if dims else None,
            "height": dims[1] if dims else None,
            "size": dims[2] if dims else None,
        })
    return jsonify(images=images, current=current_image())


@app.route("/stats")
def stats():
    state = load_state()
    return jsonify({
        "counter": state.get("counter", 0),
        "last_print": state.get("last_print"),
        "history": state.get("history", [])[:MAX_HISTORY],
        "mock": MOCK_PRINTER,
    })


@app.route("/preview/<path:name>")
def preview(name):
    if name not in list_images():
        abort(404)
    return send_from_directory(IMAGES_DIR, name)


@app.route("/preview-dithered/<path:name>")
def preview_dithered(name):
    if name not in list_images():
        abort(404)
    img = prepare_image_for_print(IMAGES_DIR / name)
    buf = BytesIO()
    img.convert("L").save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=0)


_FAVICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    b'<rect x="10" y="5" width="12" height="7" rx="2" fill="#e5e2db"/>'
    b'<rect x="4" y="10" width="24" height="14" rx="3" fill="#c45208"/>'
    b'<rect x="10" y="19" width="12" height="7" rx="1" fill="#faf7f0"/>'
    b'<rect x="9" y="17" width="14" height="2.5" rx="1" fill="#8b3704"/>'
    b'<circle cx="23.5" cy="15" r="2" fill="rgba(255,255,255,.45)"/>'
    b'</svg>'
)


@app.route("/update", methods=["POST"])
def update():
    import urllib.request
    if not GITHUB_REPO:
        return jsonify(ok=False, error="No GitHub repo configured — add github_repo to config.json."), 400

    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{GITHUB_FILE}"
    req = urllib.request.Request(url, headers={"User-Agent": "itp73-print-server"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            new_content = resp.read()
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't reach GitHub — check the internet connection and try again. ({e})"), 500

    server_path = Path(__file__).resolve()
    if server_path.read_bytes() == new_content:
        return jsonify(ok=True, updated=False, message="Already up to date — you're running the latest version!")

    try:
        server_path.write_bytes(new_content)
    except Exception as e:
        return jsonify(ok=False, error=f"Downloaded the update but couldn't save it — check file permissions. ({e})"), 500

    def _restart():
        time.sleep(2)
        os._exit(0)   # launchd KeepAlive restarts the process with the new file

    threading.Thread(target=_restart, daemon=True).start()
    return jsonify(ok=True, updated=True, message="Update downloaded! Restarting now…")


@app.route("/favicon.ico")
def favicon():
    return _FAVICON_SVG, 200, {"Content-Type": "image/svg+xml", "Cache-Control": "max-age=86400"}


@app.route("/images/<path:name>", methods=["DELETE"])
def delete_image(name):
    if name not in list_images():
        return jsonify(ok=False, error="Image not found."), 404
    try:
        (IMAGES_DIR / name).unlink()
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't delete: {e}"), 500
    if CURRENT_FILE.exists() and CURRENT_FILE.read_text().strip() == name:
        CURRENT_FILE.write_text("")
    return jsonify(ok=True)


@app.route("/history/remove", methods=["POST"])
def history_remove():
    payload = request.get_json(silent=True) or {}
    at = payload.get("at")
    if not at:
        return jsonify(ok=False, error="Missing 'at' field."), 400
    with state_lock:
        state = load_state()
        before = len(state.get("history", []))
        state["history"] = [h for h in state.get("history", []) if h.get("at") != at]
        removed = before - len(state["history"])
        save_state(state)
    return jsonify(ok=True, removed=removed)


@app.route("/preview-test")
def preview_test():
    """Live preview of what the test pattern looks like."""
    img = generate_test_pattern()
    buf = BytesIO()
    img.convert("L").save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=0)


# ── SETTINGS API ─────────────────────────────────────────────────────────────

def _list_usb_devices():
    try:
        import usb.core
        import usb.util
    except ImportError:
        return None, "pyusb not installed — run: pip install pyusb"
    devices = []
    for dev in usb.core.find(find_all=True):
        try:
            product = usb.util.get_string(dev, dev.iProduct) or ""
        except Exception:
            product = ""
        devices.append({
            "vendor_id": hex(dev.idVendor),
            "product_id": hex(dev.idProduct),
            "product": product,
        })
    return devices, None


@app.route("/api/service/restart", methods=["POST"])
def api_restart():
    def _do():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_do, daemon=True).start()
    return jsonify(ok=True, message="Restarting…")


@app.route("/api/service/stop", methods=["POST"])
def api_stop():
    plist = str(LAUNCHD_PLIST)
    def _do():
        time.sleep(0.5)
        subprocess.run(["launchctl", "unload", plist], capture_output=True)
    threading.Thread(target=_do, daemon=True).start()
    return jsonify(ok=True, message="Stopping service…")


@app.route("/api/usb-devices")
def api_usb_devices():
    devices, err = _list_usb_devices()
    if err:
        return jsonify(ok=False, error=err), 500
    return jsonify(ok=True, devices=devices)


@app.route("/api/set-printer", methods=["POST"])
def api_set_printer():
    payload = request.get_json(silent=True) or {}
    vid = str(payload.get("vendor_id", "")).strip()
    pid = str(payload.get("product_id", "")).strip()
    if not vid or not pid:
        return jsonify(ok=False, error="Missing vendor_id or product_id"), 400
    try:
        cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    except Exception:
        cfg = {}
    cfg["printer_vendor_id"] = vid
    cfg["printer_product_id"] = pid
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't save config: {e}"), 500
    def _restart():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_restart, daemon=True).start()
    return jsonify(ok=True, message="Printer updated — restarting…")


@app.route("/api/logs")
def api_logs():
    if not LOG_FILE.exists():
        return jsonify(ok=True, lines=[], available=False)
    try:
        lines = LOG_FILE.read_text(errors="replace").splitlines()[-100:]
        return jsonify(ok=True, lines=lines, available=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


def _friendly_error(msg: str) -> str:
    m = msg.lower()
    if "no backend available" in m or "libusb" in m:
        return (
            "Hmm, a required USB library (libusb) seems to be missing. "
            "Try running the installer again. "
            "(libusb not found)"
        )
    if "no such device" in m or "could not find" in m or "not found" in m:
        return (
            f"Can't find the printer — is it plugged in and switched on? "
            f"Give the cable a wiggle and try again. "
            f"(VID={hex(PRINTER_VENDOR_ID)} PID={hex(PRINTER_PRODUCT_ID)})"
        )
    if "errno 5" in m or "input/output error" in m:
        return (
            "The printer had a little hiccup. "
            "Unplug the USB cable, count to five, plug it back in, then try again."
        )
    if "permission" in m or "access denied" in m or "errno 13" in m:
        return (
            "The system is blocking access to the printer — try re-running the installer. "
            "(Permission denied)"
        )
    if "timeout" in m or "timed out" in m:
        return (
            "The printer isn't responding. "
            "Check it has paper, isn't jammed, and is switched on."
        )
    if "pipe" in m:
        return (
            "Lost the connection to the printer mid-job. "
            "Unplug and re-plug the USB cable, then try again."
        )
    return (
        f"Something went wrong with the printer ({msg}). "
        "Make sure it's plugged in and switched on, then try again."
    )


# ── UI ──────────────────────────────────────────────────────────────────────

UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="ITP-73">
<title>ITP-73 · Print Module</title>
<link rel="icon" type="image/svg+xml" href="/favicon.ico">
<link rel="apple-touch-icon" href="/favicon.ico">
<style>
/* ── tokens ────────────────────────────────────────────────────────────── */
:root {
  --bg:    #f7f6f3;
  --bg-1:  #ffffff;
  --bg-2:  #efece7;
  --rule:  #e5e2db;
  --rule-strong: #d4cfc7;

  --ink:       #1a1714;
  --ink-dim:   #726b62;
  --ink-faint: #a8a19a;

  --paper:       #faf7f0;
  --paper-shade: #ece6d4;

  --accent:      #c45208;
  --accent-glow: rgba(196,82,8,0.14);
  --accent-dark: #993f05;
  --accent-soft: #d4620f;

  --live:  #dc2626;
  --armed: #d97706;
  --ok:    #16a34a;

  --display: system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  --mono:    ui-monospace, "SFMono-Regular", Menlo, Monaco, monospace;

  --rad-sm: 6px;
  --rad-md: 10px;
  --rad-lg: 16px;
  --rad-xl: 22px;
}

/* ── reset ─────────────────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; }
* { -webkit-tap-highlight-color: transparent; }
html, body {
  margin: 0; padding: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: var(--display);
  min-height: 100%;
  overscroll-behavior: none;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
button { font: inherit; color: inherit; }

/* ── frame ─────────────────────────────────────────────────────────────── */
.frame {
  position: relative;
  max-width: 1080px;
  margin: 0 auto;
  padding: max(env(safe-area-inset-top), 28px) 24px max(env(safe-area-inset-bottom), 40px);
  display: flex; flex-direction: column; gap: 16px;
}

/* ── header ────────────────────────────────────────────────────────────── */
.prod-header {
  display: flex; align-items: center; justify-content: space-between;
  gap: 18px;
  padding: 0 2px 20px;
  border-bottom: 1px solid var(--rule);
}
.brand-block { min-width: 0; }
.brand {
  display: flex; align-items: baseline; gap: 8px;
  font-family: var(--display);
  font-weight: 700;
  font-size: clamp(20px, 4vw, 26px);
  letter-spacing: -0.02em;
  line-height: 1;
  color: var(--ink);
}
.brand em {
  font-style: normal;
  font-size: 0.55em;
  color: var(--ink-faint);
  font-weight: 400;
  letter-spacing: 0;
  transform: translateY(-0.2em);
}
.brand-mark {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--accent);
  display: inline-block;
  align-self: center;
  flex: 0 0 auto;
}
.subtitle {
  margin-top: 5px;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink-faint);
}

.header-readouts {
  display: flex; align-items: center; gap: 12px;
  flex: 0 0 auto;
}

.status-pill {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 7px 12px;
  border: 1px solid var(--rule-strong);
  border-radius: 999px;
  background: var(--bg-1);
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--ink-dim);
  transition: border-color 200ms, color 200ms, background 200ms;
}
.status-pill .pill-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--ok);
}
.status-pill[data-state="printing"] {
  color: var(--live); border-color: rgba(220,38,38,0.35);
  background: rgba(220,38,38,0.05);
}
.status-pill[data-state="printing"] .pill-dot {
  background: var(--live);
  animation: pulse 1s ease-in-out infinite;
}
.status-pill[data-state="armed"] {
  color: var(--armed); border-color: rgba(217,119,6,0.3);
}
.status-pill[data-state="armed"] .pill-dot { background: var(--armed); }
.status-pill[data-state="mock"] .pill-dot   { background: var(--armed); }
.status-pill[data-state="error"] {
  color: var(--live); border-color: rgba(220,38,38,0.3);
  background: rgba(220,38,38,0.05);
}
.status-pill[data-state="error"] .pill-dot { background: var(--live); }

@keyframes pulse {
  0%, 100% { transform: scale(1);    opacity: 1; }
  50%       { transform: scale(0.6); opacity: 0.35; }
}

.counter {
  display: flex; flex-direction: column; align-items: flex-end;
  padding-left: 14px;
  border-left: 1px solid var(--rule);
  min-width: 64px;
}
.counter-num {
  font-family: var(--display);
  font-weight: 800;
  font-size: 24px;
  line-height: 1;
  font-variant-numeric: tabular-nums;
  color: var(--ink);
}
.counter-label {
  font-size: 9px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink-faint);
  margin-top: 3px;
}

/* ── stagger reveal ────────────────────────────────────────────────────── */
[data-stagger] {
  opacity: 0;
  transform: translateY(8px);
  animation: rise 500ms cubic-bezier(.2,.7,.2,1) forwards;
}
[data-stagger="0"] { animation-delay: 60ms; }
[data-stagger="1"] { animation-delay: 120ms; }
[data-stagger="2"] { animation-delay: 180ms; }
[data-stagger="3"] { animation-delay: 240ms; }
@keyframes rise {
  to { opacity: 1; transform: translateY(0); }
}

/* ── panels ────────────────────────────────────────────────────────────── */
.panel {
  background: var(--bg-1);
  border: 1px solid var(--rule);
  border-radius: var(--rad-lg);
  box-shadow: 0 1px 3px rgba(0,0,0,0.05), 0 4px 16px rgba(0,0,0,0.04);
  padding: 18px;
}
.panel-head {
  display: flex; align-items: baseline; gap: 10px;
  padding-bottom: 14px; margin-bottom: 16px;
  border-bottom: 1px solid var(--rule);
}
.panel-idx {
  font-family: var(--display);
  font-weight: 700;
  font-size: 16px;
  color: var(--ink-faint);
  line-height: 1;
}
.panel-name {
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--ink);
  font-weight: 600;
}
.panel-meta {
  margin-left: auto;
  font-size: 10px;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  color: var(--ink-faint);
  text-align: right;
  min-width: 0; max-width: 55%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* ── deck (controls + preview, side by side on wide) ───────────────────── */
.deck {
  display: grid;
  grid-template-columns: 1fr;
  gap: 16px;
}
@media (min-width: 880px) {
  .deck {
    grid-template-columns: minmax(0, 1fr) 340px;
    gap: 20px;
  }
}

/* ── current image card ────────────────────────────────────────────────── */
.current {
  display: flex; align-items: center; gap: 12px;
  padding: 12px;
  background: var(--bg);
  border: 1px solid var(--rule);
  border-radius: var(--rad-md);
}
.thumb {
  width: 60px; height: 60px;
  border-radius: var(--rad-sm);
  background:
    repeating-conic-gradient(var(--rule) 0% 25%, var(--bg-1) 0% 50%)
    0 0 / 12px 12px;
  border: 1px solid var(--rule);
  flex: 0 0 auto;
  transition: background-image 250ms;
}
.thumb[data-loaded="1"] {
  background-color: #fff;
  background-size: contain;
  background-repeat: no-repeat;
  background-position: center;
}
.meta { min-width: 0; flex: 1; }
.filename {
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 500;
  color: var(--ink);
  word-break: break-all;
  line-height: 1.4;
}
.filename.empty { color: var(--ink-dim); font-weight: 400; }
.filemeta {
  margin-top: 4px;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--ink-faint);
  font-variant-numeric: tabular-nums;
}

/* ── big PRINT button ─────────────────────────────────────────────────── */
.btn-print {
  position: relative;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 5px;
  width: 100%;
  margin-top: 14px;
  padding: 28px 18px;
  border: 0;
  border-radius: var(--rad-xl);
  cursor: pointer;
  background: var(--accent);
  color: #fff;
  box-shadow:
    0 1px 0 rgba(255,255,255,0.2) inset,
    0 6px 20px var(--accent-glow);
  transition: transform 80ms ease, box-shadow 120ms ease, background 120ms;
}
.btn-print:hover {
  background: var(--accent-soft);
  box-shadow: 0 1px 0 rgba(255,255,255,0.2) inset, 0 8px 28px var(--accent-glow);
}
.btn-print:active { transform: translateY(2px); }
.btn-print[disabled] {
  cursor: not-allowed;
  background: var(--rule-strong);
  box-shadow: none;
  color: var(--ink-faint);
}
.btn-print-label {
  font-family: var(--display);
  font-weight: 900;
  font-size: clamp(28px, 5vw, 38px);
  letter-spacing: 0.04em;
  line-height: 0.9;
}
.btn-print-hint {
  font-weight: 400;
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: rgba(255,255,255,0.6);
  margin-top: 2px;
}
.btn-print-key {
  position: absolute;
  top: 12px; right: 16px;
  font-family: var(--mono);
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.12em;
  padding: 3px 6px;
  border-radius: 4px;
  background: rgba(255,255,255,0.18);
  color: rgba(255,255,255,0.75);
}

/* ── secondary actions ─────────────────────────────────────────────────── */
.secondary-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 10px;
}
.btn-secondary {
  display: flex; align-items: center; justify-content: space-between;
  gap: 8px;
  padding: 12px 14px;
  border: 1px solid var(--rule-strong);
  background: var(--bg-1);
  color: var(--ink);
  border-radius: var(--rad-md);
  font-size: 13px;
  cursor: pointer;
  transition: border-color 150ms, background 150ms;
}
.btn-secondary:hover { border-color: var(--accent); background: var(--bg); }
.btn-secondary:active { background: var(--bg-2); }
.btn-secondary kbd {
  display: inline-block;
  font-family: var(--mono);
  font-size: 9.5px;
  font-weight: 500;
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid var(--rule-strong);
  color: var(--ink-faint);
  background: var(--bg);
}

/* ── update button ─────────────────────────────────────────────────────── */
.btn-update {
  display: flex; align-items: center; justify-content: space-between;
  width: 100%;
  margin-top: 8px;
  padding: 10px 14px;
  border: 1px dashed var(--rule-strong);
  background: transparent;
  color: var(--ink-dim);
  border-radius: var(--rad-md);
  font-size: 13px;
  cursor: pointer;
  transition: border-color 150ms, color 150ms, background 150ms;
}
.btn-update:hover { border-color: var(--accent); color: var(--ink); background: var(--bg); }
.btn-update:active { background: var(--bg-2); }
.btn-update[disabled] { cursor: not-allowed; opacity: 0.5; }
.update-label { font-weight: 500; }
.update-sub {
  font-size: 11px;
  color: var(--ink-faint);
}

/* ── status line ───────────────────────────────────────────────────────── */
.status-line {
  margin-top: 12px;
  min-height: 20px;
  text-align: center;
  font-size: 13px;
  color: var(--ink-dim);
}
.status-line.ok  { color: var(--ok); }
.status-line.err { color: var(--live); }
.status-line .spinner {
  display: inline-block; width: 10px; height: 10px;
  border: 2px solid var(--rule-strong); border-top-color: var(--accent);
  border-radius: 50%; animation: spin 0.7s linear infinite;
  vertical-align: -1px; margin-right: 6px;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── preview / paper ───────────────────────────────────────────────────── */
.preview .printer-slot {
  position: relative;
  height: 16px;
  margin: -2px -18px 0;
  background: var(--bg-2);
  border-bottom: 1px solid var(--rule);
}
.preview .slot-mouth {
  position: absolute; left: 50%; top: 50%;
  transform: translate(-50%, -50%);
  width: 60%; max-width: 260px;
  height: 2px;
  background: var(--rule-strong);
  border-radius: 2px;
}
.paper-stage {
  display: flex; justify-content: center;
  padding: 0 0 4px;
}
.paper {
  position: relative;
  width: 100%;
  max-width: 280px;
  background: var(--paper);
  padding: 14px 16px 24px;
  margin-top: -1px;
  border-radius: 0 0 3px 3px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.09), 0 1px 4px rgba(0,0,0,0.05);
  transform-origin: top center;
  animation: paper-settle 600ms cubic-bezier(.2,.7,.2,1) backwards;
  animation-delay: 300ms;
}
@keyframes paper-settle {
  0%   { transform: translateY(-8px); opacity: 0; }
  100% { transform: translateY(0);    opacity: 1; }
}
.paper-img {
  display: block;
  max-width: 100%;
  height: auto;
  filter: contrast(1.05);
  mix-blend-mode: multiply;
  opacity: 0.9;
  transition: opacity 200ms;
}
.paper-img[src=""] { display: none; }
.paper-empty {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  padding: 44px 12px;
  text-align: center;
  font-size: 11px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--ink-faint);
}
.paper-empty[hidden] { display: none; }
.paper-cut {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--rule-strong);
  font-size: 9px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  text-align: center;
  color: var(--ink-faint);
}

/* ── upload dropzone ───────────────────────────────────────────────────── */
.upload {
  position: relative;
  padding: 24px 18px;
  border: 1px dashed var(--rule-strong);
  background: var(--bg-1);
  transition: border-color 200ms, background 200ms, transform 150ms;
  cursor: pointer;
  text-align: center;
}
.upload:hover { border-color: var(--accent); background: var(--bg); }
.upload.is-dragging {
  border-color: var(--accent);
  background: rgba(196,82,8,0.04);
  transform: translateY(-2px);
}
.upload-inner {
  display: flex; flex-direction: column; align-items: center; gap: 3px;
  pointer-events: none;
}
.upload-icon {
  font-size: 24px;
  line-height: 1;
  color: var(--accent);
  margin-bottom: 6px;
  transition: transform 200ms;
}
.upload.is-dragging .upload-icon { transform: translateY(4px); }
.upload-label {
  font-weight: 600;
  font-size: 14px;
  letter-spacing: 0.02em;
  color: var(--ink);
}
.upload-sub {
  font-size: 11px;
  color: var(--ink-faint);
}

/* ── history strip ─────────────────────────────────────────────────────── */
.history-strip {
  display: flex; gap: 10px; overflow-x: auto;
  margin: -2px; padding: 2px;
  scrollbar-width: thin;
  scrollbar-color: var(--rule-strong) transparent;
}
.history-empty {
  width: 100%;
  text-align: center;
  font-size: 12px;
  letter-spacing: 0.02em;
  color: var(--ink-faint);
  padding: 18px 0;
}
.history-card {
  flex: 0 0 auto;
  width: 70px;
  display: flex; flex-direction: column; gap: 5px;
}
.history-thumb {
  width: 70px; height: 70px;
  border-radius: var(--rad-sm);
  background: var(--paper);
  border: 1px solid var(--rule);
  background-size: contain;
  background-repeat: no-repeat;
  background-position: center;
  box-shadow: 0 2px 8px rgba(0,0,0,0.07);
  position: relative;
  overflow: hidden;
}
.history-thumb[data-kind="test"] {
  background: var(--bg-2);
  display: flex; align-items: center; justify-content: center;
  color: var(--accent);
  font-weight: 700;
  font-size: 10px;
  letter-spacing: 0.04em;
}
.history-time {
  font-family: var(--mono);
  font-size: 9px;
  color: var(--ink-faint);
  text-align: center;
  font-variant-numeric: tabular-nums;
}

/* ── drawer ────────────────────────────────────────────────────────────── */
.drawer-bg {
  position: fixed; inset: 0;
  background: rgba(26,23,20,0.3);
  backdrop-filter: blur(6px);
  opacity: 0; pointer-events: none;
  transition: opacity 200ms ease;
  z-index: 50;
}
.drawer {
  position: fixed; left: 0; right: 0; bottom: 0;
  background: var(--bg-1);
  border-top: 1px solid var(--rule);
  border-radius: var(--rad-xl) var(--rad-xl) 0 0;
  padding: 14px 16px max(env(safe-area-inset-bottom), 22px);
  max-height: 80vh;
  transform: translateY(100%);
  transition: transform 260ms cubic-bezier(.2,.7,.2,1);
  z-index: 51;
  display: flex; flex-direction: column;
  max-width: 720px;
  margin: 0 auto;
  box-shadow: 0 -8px 40px rgba(0,0,0,0.1);
}
body.open .drawer-bg { opacity: 1; pointer-events: auto; }
body.open .drawer    { transform: translateY(0); }
.grabber {
  width: 40px; height: 4px; border-radius: 2px;
  background: var(--rule-strong); margin: 2px auto 14px;
}
.drawer h2 {
  margin: 0 0 12px;
  font-size: 12px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--ink-dim);
  font-weight: 600;
}
.drawer-images {
  overflow-y: auto;
  display: grid;
  grid-template-columns: 1fr;
  gap: 6px;
}
.imgrow {
  display: flex; align-items: center; gap: 12px;
  padding: 10px;
  border: 1px solid var(--rule);
  border-radius: var(--rad-md);
  background: var(--bg-1);
  cursor: pointer;
  transition: border-color 150ms, background 150ms;
}
.imgrow:hover { border-color: var(--accent); background: var(--bg); }
.imgrow.active {
  border-color: var(--accent);
  background: rgba(196,82,8,0.04);
}
.imgrow .thumb { width: 48px; height: 48px; }
.imgrow .name {
  font-family: var(--mono);
  font-size: 11.5px;
  word-break: break-all;
  color: var(--ink);
}
.imgrow .name .dim {
  display: block;
  font-size: 10px;
  color: var(--ink-faint);
  margin-top: 2px;
}

/* ── remove buttons ────────────────────────────────────────────────────── */
.history-card { position: relative; }
.card-remove-btn {
  position: absolute;
  top: 3px; right: 3px;
  width: 18px; height: 18px;
  border-radius: 50%;
  border: 1px solid var(--rule-strong);
  background: var(--bg-1);
  color: var(--ink-dim);
  font-size: 11px;
  line-height: 1;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  opacity: 0;
  transition: opacity 150ms, background 150ms, color 150ms, border-color 150ms;
  z-index: 10;
  padding: 0;
}
.history-card:hover .card-remove-btn { opacity: 1; }
.card-remove-btn:hover { background: var(--live); color: #fff; border-color: var(--live); }

.imgrow { position: relative; }
.row-remove-btn {
  position: absolute;
  top: 50%; right: 10px;
  transform: translateY(-50%);
  width: 22px; height: 22px;
  border-radius: 50%;
  border: 1px solid var(--rule-strong);
  background: var(--bg-1);
  color: var(--ink-dim);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  opacity: 0;
  transition: opacity 150ms, background 150ms, color 150ms, border-color 150ms;
  z-index: 10;
  padding: 0;
  flex-shrink: 0;
}
.imgrow:hover .row-remove-btn { opacity: 1; }
.row-remove-btn:hover { background: var(--live); color: #fff; border-color: var(--live); }

/* ── responsive ────────────────────────────────────────────────────────── */
@media (max-width: 560px) {
  .header-readouts .counter { display: none; }
  .secondary-row { grid-template-columns: 1fr; }
}
@media (min-width: 880px) {
  .preview { grid-column: 2; grid-row: 1 / 3; align-self: start; }
  .controls { grid-column: 1; grid-row: 1; }
}

/* ── settings gear button ───────────────────────────────────────────────── */
.settings-gear-btn {
  width: 38px; height: 38px;
  border-radius: 50%;
  border: 1px solid var(--rule-strong);
  background: var(--bg-2);
  color: var(--ink-dim);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  transition: background 150ms, color 150ms, border-color 150ms;
  padding: 0;
}
.settings-gear-btn:hover { background: var(--bg-1); color: var(--ink); border-color: var(--ink-dim); }

/* ── settings panel ─────────────────────────────────────────────────────── */
.settings-panel {
  position: fixed; top: 0; right: 0; bottom: 0;
  width: min(380px, 100vw);
  background: var(--bg-1);
  border-left: 1px solid var(--rule);
  padding: max(env(safe-area-inset-top), 24px) 20px max(env(safe-area-inset-bottom), 28px);
  transform: translateX(100%);
  transition: transform 260ms cubic-bezier(.2,.7,.2,1);
  z-index: 51;
  overflow-y: auto;
  display: flex; flex-direction: column;
  box-shadow: -8px 0 40px rgba(0,0,0,0.1);
}
body.settings-open .settings-panel { transform: translateX(0); }
body.settings-open .drawer-bg { opacity: 1; pointer-events: auto; }

.settings-panel-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 20px;
}
.settings-panel-head h2 {
  margin: 0;
  font-size: 13px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--ink-dim);
  font-weight: 600;
}
.settings-close-btn {
  width: 32px; height: 32px;
  border-radius: 50%;
  border: 1px solid var(--rule-strong);
  background: var(--bg-2);
  color: var(--ink-dim);
  font-size: 16px;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  padding: 0;
  transition: background 150ms, color 150ms;
}
.settings-close-btn:hover { background: var(--rule); color: var(--ink); }

.settings-section {
  border-top: 1px solid var(--rule);
  padding: 16px 0;
}
.settings-section:first-of-type { border-top: none; padding-top: 0; }

.settings-section-head {
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--ink-faint);
  font-weight: 600;
  margin-bottom: 10px;
  display: flex; align-items: center; justify-content: space-between;
}

.sbtn {
  width: 100%;
  min-height: 52px;
  border-radius: var(--rad-md);
  border: 1px solid var(--rule-strong);
  background: var(--bg-2);
  color: var(--ink);
  cursor: pointer;
  padding: 10px 14px;
  text-align: left;
  display: flex; flex-direction: column; justify-content: center; gap: 2px;
  transition: background 150ms, border-color 150ms;
  margin-bottom: 8px;
}
.sbtn:last-child { margin-bottom: 0; }
.sbtn:hover:not(:disabled) { background: var(--bg-1); border-color: var(--ink-dim); }
.sbtn:disabled { opacity: 0.55; cursor: not-allowed; }
.sbtn-label { font-size: 14px; font-weight: 600; }
.sbtn-sub { font-size: 11px; color: var(--ink-dim); margin-top: 1px; }

.sbtn-accent {
  border-color: var(--accent);
  background: rgba(196,82,8,0.06);
  color: var(--accent-dark);
}
.sbtn-accent:hover:not(:disabled) { background: rgba(196,82,8,0.12); border-color: var(--accent-dark); }
.sbtn-accent .sbtn-sub { color: var(--accent); }

.sbtn-danger {
  border-color: var(--rule-strong);
}
.sbtn-danger .sbtn-label { color: var(--live); }
.sbtn-danger:hover:not(:disabled) { border-color: var(--live); background: rgba(220,38,38,0.05); }

.settings-confirm {
  border: 1px solid var(--live);
  border-radius: var(--rad-md);
  padding: 12px 14px;
  background: rgba(220,38,38,0.04);
  margin-bottom: 8px;
}
.confirm-msg {
  font-size: 12px;
  color: var(--ink-dim);
  margin-bottom: 10px;
  line-height: 1.5;
}
.confirm-btns { display: flex; gap: 8px; }
.confirm-btns .sbtn { margin-bottom: 0; flex: 1; min-height: 40px; }
.confirm-btns .sbtn-danger-solid {
  border-color: var(--live); background: var(--live); color: #fff;
}
.confirm-btns .sbtn-danger-solid:hover:not(:disabled) { background: #b91c1c; border-color: #b91c1c; }

.settings-info-grid {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 6px 12px;
  font-size: 12px;
}
.settings-info-key { color: var(--ink-faint); }
.settings-info-val { font-family: var(--mono); color: var(--ink); word-break: break-all; }

.settings-printer-info {
  font-size: 12px;
  font-family: var(--mono);
  color: var(--ink-dim);
  margin-bottom: 10px;
  padding: 8px 10px;
  background: var(--bg-2);
  border-radius: var(--rad-sm);
  border: 1px solid var(--rule);
}

.usb-list { display: flex; flex-direction: column; gap: 6px; margin-top: 10px; }
.usb-row {
  display: flex; align-items: center; gap: 10px;
  padding: 10px;
  border: 1px solid var(--rule);
  border-radius: var(--rad-md);
  background: var(--bg-1);
}
.usb-ids { font-family: var(--mono); font-size: 11px; color: var(--ink-dim); flex-shrink: 0; }
.usb-name { font-size: 12px; color: var(--ink); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.usb-select-btn {
  flex-shrink: 0;
  padding: 5px 10px;
  border-radius: var(--rad-sm);
  border: 1px solid var(--rule-strong);
  background: var(--bg-2);
  font-size: 12px; font: inherit;
  cursor: pointer;
  transition: background 150ms, border-color 150ms;
}
.usb-select-btn:hover { background: var(--accent); color: #fff; border-color: var(--accent); }

.settings-refresh-btn {
  font: inherit;
  font-size: 10px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--accent);
  background: none;
  border: none;
  cursor: pointer;
  padding: 0;
}
.settings-refresh-btn:hover { text-decoration: underline; }

.log-box {
  background: var(--bg);
  border: 1px solid var(--rule);
  border-radius: var(--rad-sm);
  padding: 10px;
  font-family: var(--mono);
  font-size: 10px;
  line-height: 1.5;
  color: var(--ink-dim);
  height: 200px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

.settings-note {
  font-size: 12px;
  color: var(--ink-faint);
  line-height: 1.5;
  padding: 8px 0;
}
.settings-note.ok { color: var(--ok); }
.settings-note.err { color: var(--live); }
</style>
</head>
<body>

<div class="frame">

  <header class="prod-header">
    <div class="brand-block">
      <h1 class="brand">
        <span class="brand-mark"></span>
        <span class="brand-text">Premier <em>×</em> ITP-73</span>
      </h1>
      <div class="subtitle">Thermal printer module · 80 mm · 576 dots</div>
    </div>
    <div class="header-readouts">
      <div class="status-pill" id="statusPill"
           data-state="{% if mock %}mock{% else %}ready{% endif %}">
        <span class="pill-dot"></span>
        <span class="pill-label">{% if mock %}MOCK{% else %}READY{% endif %}</span>
      </div>
      <div class="counter">
        <div class="counter-num" id="counter">000</div>
        <div class="counter-label">prints</div>
      </div>
      <button class="settings-gear-btn" id="settingsBtn" aria-label="Open settings">⚙</button>
    </div>
  </header>

  <div class="deck">
    <section class="panel controls" data-stagger="0">
      <header class="panel-head">
        <span class="panel-idx">01</span>
        <span class="panel-name">Control</span>
        <span class="panel-meta" id="dimsMeta">—</span>
      </header>

      <div class="current">
        <div class="thumb" id="thumb"></div>
        <div class="meta">
          <div class="filename {% if not current %}empty{% endif %}" id="filename">
            {{ current or "No image selected" }}
          </div>
          <div class="filemeta" id="filemeta">
            {% if current %}—{% else %}Drop an image below to get started{% endif %}
          </div>
        </div>
      </div>

      <button class="btn-print" id="printBtn" {% if not current %}disabled{% endif %}>
        <span class="btn-print-label">PRINT</span>
        <span class="btn-print-hint">Cut a receipt</span>
        <span class="btn-print-key">P</span>
      </button>

      <div class="secondary-row">
        <button class="btn-secondary" id="testBtn">
          <span>Test print</span><kbd>T</kbd>
        </button>
        <button class="btn-secondary" id="changeBtn">
          <span>Change image</span><kbd>I</kbd>
        </button>
      </div>
      {% if github_repo %}
      <button class="btn-update" id="updateBtn">
        <span class="update-label">Update app</span>
        <span class="update-sub">pull latest from GitHub</span>
      </button>
      {% endif %}

      <div class="status-line" id="status">Ready.</div>
    </section>

    <section class="panel preview" data-stagger="1">
      <header class="panel-head">
        <span class="panel-idx">02</span>
        <span class="panel-name">Live preview</span>
        <span class="panel-meta">as it would print</span>
      </header>

      <div class="printer-slot">
        <div class="slot-mouth"></div>
      </div>

      <div class="paper-stage">
        <div class="paper" id="paper">
          <img class="paper-img" id="paperImg" alt="" src="" />
          <div class="paper-empty" id="paperEmpty" {% if current %}hidden{% endif %}>
            No image selected yet
          </div>
          <div class="paper-cut">✂ &nbsp; cut</div>
        </div>
      </div>
    </section>
  </div>

  <section class="panel upload" id="dropzone" data-stagger="2"
           role="button" tabindex="0" aria-label="Upload images">
    <input type="file" id="fileInput" multiple accept="image/*" hidden />
    <div class="upload-inner">
      <div class="upload-icon">↓</div>
      <div class="upload-label">Drop images here</div>
      <div class="upload-sub">or click to browse  ·  PNG, JPG, GIF, WEBP</div>
    </div>
  </section>

  <section class="panel history" data-stagger="3">
    <header class="panel-head">
      <span class="panel-idx">03</span>
      <span class="panel-name">Recent</span>
      <span class="panel-meta" id="lastPrintMeta">—</span>
    </header>
    <div class="history-strip" id="historyStrip">
      <div class="history-empty">No prints yet — go make something!</div>
    </div>
  </section>

</div>

<!-- Image picker drawer -->
<div class="drawer-bg" id="drawerBg"></div>
<aside class="drawer" id="drawer" aria-label="Select image">
  <div class="grabber"></div>
  <h2>Select an image</h2>
  <div class="drawer-images" id="drawerImages"></div>
</aside>

<!-- Settings panel -->
<aside class="settings-panel" id="settingsPanel" aria-label="Settings">
  <div class="settings-panel-head">
    <h2>Settings</h2>
    <button class="settings-close-btn" id="settingsCloseBtn" aria-label="Close settings">×</button>
  </div>

  <!-- Service -->
  <div class="settings-section">
    <div class="settings-section-head">Service</div>
    {% if mock %}
    <div class="settings-note">Service controls are only available when running under launchd.</div>
    {% else %}
    <button class="sbtn sbtn-accent" id="restartBtn">
      <span class="sbtn-label">Restart service</span>
      <span class="sbtn-sub" id="restartSub">Server restarts automatically — page reloads when ready</span>
    </button>
    <button class="sbtn sbtn-danger" id="stopBtn">
      <span class="sbtn-label">Stop service</span>
      <span class="sbtn-sub">Shuts down until next Mac restart or manual reload</span>
    </button>
    <div class="settings-confirm" id="stopConfirm" hidden>
      <div class="confirm-msg">This will shut down the server. The web UI will be unreachable until the Mac restarts or you manually run <code>launchctl load ~/Library/LaunchAgents/com.itp73.printserver.plist</code>.</div>
      <div class="confirm-btns">
        <button class="sbtn confirm-btns sbtn-danger-solid" id="stopConfirmYes">
          <span class="sbtn-label">Yes, stop it</span>
        </button>
        <button class="sbtn" id="stopConfirmNo">
          <span class="sbtn-label">Cancel</span>
        </button>
      </div>
    </div>
    {% endif %}
  </div>

  <!-- Printer -->
  <div class="settings-section">
    <div class="settings-section-head">Printer</div>
    <div class="settings-printer-info">{{ printer_vid }} : {{ printer_pid }}</div>
    <button class="sbtn" id="redetectBtn">
      <span class="sbtn-label">Re-detect printer</span>
      <span class="sbtn-sub" id="redetectSub">Scan USB and pick a device</span>
    </button>
    <div class="usb-list" id="usbList" hidden></div>
  </div>

  <!-- Logs -->
  <div class="settings-section">
    <div class="settings-section-head">
      Live logs
      <button class="settings-refresh-btn" id="logsRefreshBtn">Refresh</button>
    </div>
    {% if mock %}
    <div class="settings-note">Logs go to stdout in mock mode — check your terminal.</div>
    {% else %}
    <div class="log-box" id="logBox">Loading…</div>
    {% endif %}
  </div>

  <!-- App info -->
  <div class="settings-section">
    <div class="settings-section-head">App info</div>
    <div class="settings-info-grid">
      <span class="settings-info-key">Port</span>
      <span class="settings-info-val">{{ port }}</span>
      <span class="settings-info-key">Mode</span>
      <span class="settings-info-val">{% if mock %}Mock{% else %}Live{% endif %}</span>
      {% if github_repo %}
      <span class="settings-info-key">GitHub</span>
      <span class="settings-info-val">{{ github_repo }}</span>
      {% endif %}
    </div>
  </div>
</aside>

<script>
const $ = (id) => document.getElementById(id);
const els = {
  printBtn:   $("printBtn"),
  testBtn:    $("testBtn"),
  changeBtn:  $("changeBtn"),
  statusPill: $("statusPill"),
  pillLabel:  document.querySelector("#statusPill .pill-label"),
  counter:    $("counter"),
  filename:   $("filename"),
  filemeta:   $("filemeta"),
  dimsMeta:   $("dimsMeta"),
  thumb:      $("thumb"),
  paper:      $("paper"),
  paperImg:   $("paperImg"),
  paperEmpty: $("paperEmpty"),
  status:     $("status"),
  drawer:     $("drawer"),
  drawerBg:   $("drawerBg"),
  drawerImages: $("drawerImages"),
  dropzone:   $("dropzone"),
  fileInput:  $("fileInput"),
  history:    $("historyStrip"),
  lastPrintMeta: $("lastPrintMeta"),
  updateBtn:  $("updateBtn"),
  settingsBtn: $("settingsBtn"),
  settingsPanel: $("settingsPanel"),
  settingsCloseBtn: $("settingsCloseBtn"),
  restartBtn: $("restartBtn"),
  restartSub: $("restartSub"),
  stopBtn:    $("stopBtn"),
  stopConfirm: $("stopConfirm"),
  stopConfirmYes: $("stopConfirmYes"),
  stopConfirmNo: $("stopConfirmNo"),
  redetectBtn: $("redetectBtn"),
  redetectSub: $("redetectSub"),
  usbList:    $("usbList"),
  logBox:     $("logBox"),
  logsRefreshBtn: $("logsRefreshBtn"),
};

let current = {{ current|tojson }};
const isMock = {{ "true" if mock else "false" }};

// ── state helpers ─────────────────────────────────────────────────────────
function setPillState(state, label) {
  els.statusPill.dataset.state = state;
  if (label) els.pillLabel.textContent = label;
}

function setStatus(text, cls="") {
  els.status.className = "status-line " + cls;
  els.status.innerHTML = text;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleTimeString(undefined, {hour: "2-digit", minute: "2-digit"});
}

function fmtBytes(b) {
  if (!b) return "";
  if (b < 1024) return b + " B";
  if (b < 1024*1024) return (b/1024).toFixed(0) + " KB";
  return (b/1024/1024).toFixed(1) + " MB";
}

// ── current-image rendering ──────────────────────────────────────────────
async function refreshCurrent() {
  const r = await fetch("/images-list");
  const j = await r.json();
  current = j.current;
  const meta = (j.images || []).find(i => i.name === current);

  if (current) {
    els.filename.textContent = current;
    els.filename.classList.remove("empty");
    if (meta && meta.width) {
      els.filemeta.textContent =
        `${meta.width} × ${meta.height}  ·  ${fmtBytes(meta.size)}`;
      els.dimsMeta.textContent = `${meta.width}×${meta.height}`;
    } else {
      els.filemeta.textContent = "—";
      els.dimsMeta.textContent = "—";
    }
    els.thumb.dataset.loaded = "1";
    els.thumb.style.backgroundImage = `url('/preview/${encodeURIComponent(current)}')`;
    els.printBtn.disabled = false;
    setPillState(isMock ? "mock" : "armed", isMock ? "MOCK" : "ARMED");

    // dithered preview into paper
    const cacheBust = "?v=" + Date.now();
    els.paperImg.src = "/preview-dithered/" + encodeURIComponent(current) + cacheBust;
    els.paperImg.style.display = "";
    if (meta && meta.width) {
      els.paperImg.style.width = Math.min(meta.width / 576, 1) * 100 + '%';
    }
    els.paperEmpty.hidden = true;
  } else {
    els.filename.textContent = "No image selected";
    els.filename.classList.add("empty");
    els.filemeta.textContent = "Drop an image below to get started";
    els.dimsMeta.textContent = "—";
    els.thumb.dataset.loaded = "0";
    els.thumb.style.backgroundImage = "";
    els.printBtn.disabled = true;
    setPillState(isMock ? "mock" : "ready", isMock ? "MOCK" : "READY");
    els.paperImg.src = "";
    els.paperImg.style.display = "none";
    els.paperImg.style.width = '';
    els.paperEmpty.hidden = false;
  }
}

// ── stats / counter / history ────────────────────────────────────────────
async function refreshStats() {
  const r = await fetch("/stats");
  const j = await r.json();
  els.counter.textContent = String(j.counter || 0).padStart(3, "0");
  els.lastPrintMeta.textContent = j.last_print
    ? "last " + fmtTime(j.last_print)
    : "no prints yet";
  renderHistory(j.history || []);
}

function renderHistory(history) {
  els.history.innerHTML = "";
  if (!history.length) {
    const e = document.createElement("div");
    e.className = "history-empty";
    e.textContent = "No prints yet — go make something!";
    els.history.appendChild(e);
    return;
  }
  for (const h of history) {
    const card = document.createElement("div");
    card.className = "history-card";
    const t = document.createElement("div");
    t.className = "history-thumb";
    if (h.kind === "test") {
      t.dataset.kind = "test";
      t.textContent = "TEST";
    } else {
      t.style.backgroundImage = `url('/preview/${encodeURIComponent(h.label)}')`;
    }
    const time = document.createElement("div");
    time.className = "history-time";
    time.textContent = fmtTime(h.at);
    const removeBtn = document.createElement("button");
    removeBtn.className = "card-remove-btn";
    removeBtn.title = "Remove from history";
    removeBtn.textContent = "×";
    removeBtn.onclick = (e) => { e.stopPropagation(); removeHistoryEntry(h.at); };
    card.appendChild(t); card.appendChild(time); card.appendChild(removeBtn);
    els.history.appendChild(card);
  }
}

// ── print actions ────────────────────────────────────────────────────────
async function doPrint() {
  if (!current) return;
  els.printBtn.disabled = true;
  setPillState("printing", "PRINTING");
  setStatus('<span class="spinner"></span>Sending to printer…');
  try {
    const r = await fetch("/print", { method: "POST" });
    const j = await r.json();
    if (j.ok) {
      setStatus(j.mock ? "✓ Mock print — saved to mock_output/" : "✓ Off it goes!", "ok");
      await refreshStats();
    } else {
      setStatus("✗ " + (j.error || "Couldn't print — is the printer on and plugged in?"), "err");
      setPillState("error", "ERROR");
    }
  } catch (e) {
    setStatus("✗ " + e.message, "err");
    setPillState("error", "ERROR");
  } finally {
    els.printBtn.disabled = !current;
    setTimeout(() => {
      if (els.status.classList.contains("ok")) {
        setStatus("Ready.");
        setPillState(current ? (isMock ? "mock" : "armed") : (isMock ? "mock" : "ready"),
                     current ? (isMock ? "MOCK" : "ARMED") : (isMock ? "MOCK" : "READY"));
      }
    }, 2800);
  }
}

async function doTestPrint() {
  els.testBtn.disabled = true;
  setPillState("printing", "TESTING");
  setStatus('<span class="spinner"></span>Printing a test page…');
  try {
    const r = await fetch("/test-print", { method: "POST" });
    const j = await r.json();
    if (j.ok) {
      setStatus(j.mock ? "✓ Test page saved to mock_output/" : "✓ Test page done — printer is happy!", "ok");
      await refreshStats();
    } else {
      setStatus("✗ " + (j.error || "Couldn't print the test page — check the printer is on and plugged in."), "err");
      setPillState("error", "ERROR");
    }
  } catch (e) {
    setStatus("✗ " + e.message, "err");
    setPillState("error", "ERROR");
  } finally {
    els.testBtn.disabled = false;
    setTimeout(() => {
      if (els.status.classList.contains("ok")) {
        setStatus("Ready.");
        setPillState(current ? (isMock ? "mock" : "armed") : (isMock ? "mock" : "ready"),
                     current ? (isMock ? "MOCK" : "ARMED") : (isMock ? "MOCK" : "READY"));
      }
    }, 2800);
  }
}

// ── upload ──────────────────────────────────────────────────────────────
async function uploadFiles(fileList) {
  const files = Array.from(fileList).filter(f => f.type.startsWith("image/"));
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("file", f);
  setStatus('<span class="spinner"></span>Adding image…');
  try {
    const r = await fetch("/upload", { method: "POST", body: fd });
    const j = await r.json();
    if (j.ok) {
      setStatus(j.uploaded.length === 1 ? "✓ Image added — ready to print!" : `✓ ${j.uploaded.length} images added`, "ok");
      await refreshCurrent();
    } else {
      setStatus("✗ " + (j.error || "Hmm, that file didn't make it. Make sure it's a PNG, JPG, or similar image."), "err");
    }
  } catch (e) {
    setStatus("✗ " + e.message, "err");
  }
  setTimeout(() => {
    if (els.status.classList.contains("ok")) setStatus("Ready.");
  }, 2200);
}

// drag-and-drop wiring (page-wide so any drop counts)
let dragDepth = 0;
function setDragging(on) {
  els.dropzone.classList.toggle("is-dragging", on);
}
window.addEventListener("dragenter", e => {
  if (!e.dataTransfer?.types?.includes("Files")) return;
  e.preventDefault();
  dragDepth++;
  setDragging(true);
});
window.addEventListener("dragover", e => {
  if (!e.dataTransfer?.types?.includes("Files")) return;
  e.preventDefault();
});
window.addEventListener("dragleave", e => {
  if (!e.dataTransfer?.types?.includes("Files")) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) setDragging(false);
});
window.addEventListener("drop", e => {
  if (!e.dataTransfer?.files?.length) return;
  e.preventDefault();
  dragDepth = 0;
  setDragging(false);
  uploadFiles(e.dataTransfer.files);
});

// click to browse
els.dropzone.addEventListener("click", () => els.fileInput.click());
els.dropzone.addEventListener("keydown", e => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); els.fileInput.click(); }
});
els.fileInput.addEventListener("change", e => {
  if (e.target.files.length) uploadFiles(e.target.files);
  e.target.value = "";
});

// ── drawer ───────────────────────────────────────────────────────────────
async function openDrawer() {
  document.body.classList.add("open");
  const r = await fetch("/images-list");
  const j = await r.json();
  els.drawerImages.innerHTML = "";
  if (!j.images.length) {
    const e = document.createElement("div");
    e.className = "history-empty";
    e.textContent = "No images yet — drop one onto the page to get started";
    els.drawerImages.appendChild(e);
    return;
  }
  for (const img of j.images) {
    const row = document.createElement("div");
    row.className = "imgrow" + (img.name === j.current ? " active" : "");
    const thumb = document.createElement("div");
    thumb.className = "thumb";
    thumb.dataset.loaded = "1";
    thumb.style.backgroundImage = `url('/preview/${encodeURIComponent(img.name)}')`;
    const nameDiv = document.createElement("div");
    nameDiv.className = "name";
    nameDiv.textContent = img.name;
    const dim = document.createElement("span");
    dim.className = "dim";
    dim.textContent = `${img.width||"?"} × ${img.height||"?"} · ${fmtBytes(img.size)}`;
    nameDiv.appendChild(dim);
    const removeBtn = document.createElement("button");
    removeBtn.className = "row-remove-btn";
    removeBtn.title = "Delete image";
    removeBtn.textContent = "×";
    removeBtn.onclick = (e) => { e.stopPropagation(); deleteImage(img.name); };
    row.appendChild(thumb); row.appendChild(nameDiv); row.appendChild(removeBtn);
    row.onclick = () => selectImage(img.name);
    els.drawerImages.appendChild(row);
  }
}
function closeDrawer() { document.body.classList.remove("open"); }

async function selectImage(name) {
  const r = await fetch("/select", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name })
  });
  const j = await r.json();
  if (j.ok) {
    await refreshCurrent();
    closeDrawer();
    setStatus("✓ All set — ready to print!", "ok");
    setTimeout(() => {
      if (els.status.classList.contains("ok")) setStatus("Ready.");
    }, 1800);
  }
}

async function deleteImage(name) {
  const r = await fetch("/images/" + encodeURIComponent(name), { method: "DELETE" });
  const j = await r.json();
  if (j.ok) {
    await refreshCurrent();
    await openDrawer();
  }
}

async function removeHistoryEntry(at) {
  const r = await fetch("/history/remove", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ at })
  });
  const j = await r.json();
  if (j.ok) await refreshStats();
}

// ── update ───────────────────────────────────────────────────────────────
async function doUpdate() {
  if (!els.updateBtn) return;
  els.updateBtn.disabled = true;
  els.updateBtn.querySelector(".update-sub").textContent = "checking GitHub…";
  setStatus('<span class="spinner"></span>Checking for updates…');
  try {
    const r = await fetch("/update", { method: "POST" });
    const j = await r.json();
    if (!j.ok) {
      setStatus("✗ " + j.error, "err");
      els.updateBtn.querySelector(".update-sub").textContent = "pull latest from GitHub";
      els.updateBtn.disabled = false;
      return;
    }
    if (!j.updated) {
      setStatus("✓ " + j.message, "ok");
      els.updateBtn.querySelector(".update-sub").textContent = "pull latest from GitHub";
      els.updateBtn.disabled = false;
      return;
    }
    // Update downloaded — server is restarting
    setStatus("✓ " + j.message, "ok");
    els.updateBtn.querySelector(".update-label").textContent = "Restarting…";
    els.updateBtn.querySelector(".update-sub").textContent = "page will reload automatically";
    let countdown = 5;
    const tick = setInterval(() => {
      countdown--;
      els.updateBtn.querySelector(".update-sub").textContent = `reloading in ${countdown}s…`;
      if (countdown <= 0) { clearInterval(tick); location.reload(); }
    }, 1000);
  } catch (e) {
    setStatus("✗ Couldn't reach the server. Try again.", "err");
    els.updateBtn.querySelector(".update-sub").textContent = "pull latest from GitHub";
    els.updateBtn.disabled = false;
  }
}

// ── settings panel ──────────────────────────────────────────────────────
let logsInterval = null;

function openSettings() {
  document.body.classList.add("settings-open");
  loadLogs();
  if (els.logBox) logsInterval = setInterval(loadLogs, 5000);
}

function closeSettings() {
  document.body.classList.remove("settings-open");
  if (logsInterval) { clearInterval(logsInterval); logsInterval = null; }
}

async function loadLogs() {
  if (!els.logBox) return;
  try {
    const r = await fetch("/api/logs");
    const j = await r.json();
    if (!j.ok || !j.available) {
      els.logBox.textContent = "Log file not available in this mode.";
      return;
    }
    els.logBox.textContent = j.lines.join("\n");
    els.logBox.scrollTop = els.logBox.scrollHeight;
  } catch (e) {
    els.logBox.textContent = "Couldn't fetch logs: " + e.message;
  }
}

async function doRestart() {
  if (!els.restartBtn) return;
  els.restartBtn.disabled = true;
  if (els.stopBtn) els.stopBtn.disabled = true;
  if (els.restartSub) els.restartSub.textContent = "Restarting…";
  try {
    await fetch("/api/service/restart", { method: "POST" });
  } catch (_) {}
  let n = 5;
  const tick = setInterval(() => {
    n--;
    if (els.restartSub) els.restartSub.textContent = `Reloading in ${n}s…`;
    if (n <= 0) { clearInterval(tick); location.reload(); }
  }, 1000);
}

function showStopConfirm() {
  if (els.stopConfirm) els.stopConfirm.hidden = false;
  if (els.stopBtn) els.stopBtn.hidden = true;
}

function hideStopConfirm() {
  if (els.stopConfirm) els.stopConfirm.hidden = true;
  if (els.stopBtn) els.stopBtn.hidden = false;
}

async function doStop() {
  if (!els.stopConfirmYes) return;
  els.stopConfirmYes.disabled = true;
  if (els.stopConfirmYes.querySelector(".sbtn-label"))
    els.stopConfirmYes.querySelector(".sbtn-label").textContent = "Stopping…";
  try {
    await fetch("/api/service/stop", { method: "POST" });
  } catch (_) {}
  if (els.stopConfirm) {
    els.stopConfirm.querySelector(".confirm-msg").textContent =
      "Server stopped. This page is no longer reachable — close this tab.";
  }
}

async function doRedetect() {
  if (!els.redetectBtn || !els.usbList) return;
  els.redetectBtn.disabled = true;
  if (els.redetectSub) els.redetectSub.textContent = "Scanning USB…";
  els.usbList.hidden = false;
  els.usbList.innerHTML = '<div class="settings-note"><span class="spinner"></span> Scanning USB devices…</div>';
  try {
    const r = await fetch("/api/usb-devices");
    const j = await r.json();
    els.redetectBtn.disabled = false;
    if (els.redetectSub) els.redetectSub.textContent = "Scan USB and pick a device";
    if (!j.ok) {
      els.usbList.innerHTML = `<div class="settings-note err">Error: ${j.error}</div>`;
      return;
    }
    if (!j.devices.length) {
      els.usbList.innerHTML = '<div class="settings-note">No USB devices found. Check the cable and power.</div>';
      return;
    }
    els.usbList.innerHTML = "";
    for (const dev of j.devices) {
      const row = document.createElement("div");
      row.className = "usb-row";
      const ids = document.createElement("div");
      ids.className = "usb-ids";
      ids.textContent = dev.vendor_id + ":" + dev.product_id;
      const name = document.createElement("div");
      name.className = "usb-name";
      name.textContent = dev.product || "Unknown device";
      const btn = document.createElement("button");
      btn.className = "usb-select-btn";
      btn.textContent = "Select";
      btn.onclick = () => selectPrinter(dev.vendor_id, dev.product_id, dev.product);
      row.appendChild(ids); row.appendChild(name); row.appendChild(btn);
      els.usbList.appendChild(row);
    }
  } catch (e) {
    els.redetectBtn.disabled = false;
    if (els.redetectSub) els.redetectSub.textContent = "Scan USB and pick a device";
    els.usbList.innerHTML = `<div class="settings-note err">Couldn't scan USB: ${e.message}</div>`;
  }
}

async function selectPrinter(vid, pid, name) {
  els.usbList.innerHTML = '<div class="settings-note"><span class="spinner"></span> Saving and restarting…</div>';
  try {
    const r = await fetch("/api/set-printer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vendor_id: vid, product_id: pid }),
    });
    const j = await r.json();
    if (j.ok) {
      els.usbList.innerHTML = `<div class="settings-note ok">Printer set to ${name || vid + ":" + pid}. Reloading…</div>`;
      let n = 5;
      const tick = setInterval(() => {
        n--;
        if (n <= 0) { clearInterval(tick); location.reload(); }
      }, 1000);
    } else {
      els.usbList.innerHTML = `<div class="settings-note err">Error: ${j.error}</div>`;
    }
  } catch (_) {
    els.usbList.innerHTML = '<div class="settings-note ok">Printer updated. Reloading…</div>';
    setTimeout(() => location.reload(), 3000);
  }
}

// ── keyboard ─────────────────────────────────────────────────────────────
window.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  switch (e.key.toLowerCase()) {
    case "p": if (!els.printBtn.disabled) doPrint(); break;
    case "t": doTestPrint(); break;
    case "i": openDrawer(); break;
    case "escape": closeDrawer(); closeSettings(); break;
  }
});

// ── wiring ───────────────────────────────────────────────────────────────
els.printBtn.addEventListener("click", doPrint);
els.testBtn.addEventListener("click", doTestPrint);
els.changeBtn.addEventListener("click", openDrawer);
els.drawerBg.addEventListener("click", () => { closeDrawer(); closeSettings(); });
if (els.updateBtn) els.updateBtn.addEventListener("click", doUpdate);
els.settingsBtn.addEventListener("click", openSettings);
els.settingsCloseBtn.addEventListener("click", closeSettings);
if (els.restartBtn) els.restartBtn.addEventListener("click", doRestart);
if (els.stopBtn) els.stopBtn.addEventListener("click", showStopConfirm);
if (els.stopConfirmYes) els.stopConfirmYes.addEventListener("click", doStop);
if (els.stopConfirmNo) els.stopConfirmNo.addEventListener("click", hideStopConfirm);
if (els.redetectBtn) els.redetectBtn.addEventListener("click", doRedetect);
if (els.logsRefreshBtn) els.logsRefreshBtn.addEventListener("click", loadLogs);

// touch swipe-down to close drawer
let startY = null;
els.drawer.addEventListener("touchstart", e => { startY = e.touches[0].clientY; });
els.drawer.addEventListener("touchmove", e => {
  if (startY === null) return;
  if (e.touches[0].clientY - startY > 60) { closeDrawer(); startY = null; }
});

// ── init ─────────────────────────────────────────────────────────────────
refreshCurrent();
refreshStats();
</script>
</body>
</html>
"""


# ── BOOT ────────────────────────────────────────────────────────────────────

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def banner():
    ip = get_local_ip()
    host = socket.gethostname()
    if not host.endswith(".local"):
        host = host + ".local"
    mode = "MOCK MODE" if MOCK_PRINTER else f"PRINTER {hex(PRINTER_VENDOR_ID)}:{hex(PRINTER_PRODUCT_ID)}"
    print("=" * 64)
    print(f"  ITP-73 print server — {mode}")
    print(f"  On this machine:   http://localhost:{PORT}")
    print(f"  Same Wi-Fi:        http://{host}:{PORT}")
    print(f"                     http://{ip}:{PORT}")
    print(f"  Images:            {IMAGES_DIR}")
    print(f"  Stop with Ctrl-C.")
    print("=" * 64)


if __name__ == "__main__":
    if "--list-usb" in sys.argv:
        devices, err = _list_usb_devices()
        if err:
            print(err)
            sys.exit(1)
        print(f"{'VID':>6}  {'PID':>6}  Product")
        print("-" * 50)
        for dev in devices:
            print(f"{dev['vendor_id']:>6}  {dev['product_id']:>6}  {dev['product'] or '?'}")
        sys.exit(0)

    banner()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
