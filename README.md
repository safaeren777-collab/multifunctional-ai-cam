# Smart OS — ESP32-S3 Multi-Mode AI Assistant

Tap-controlled, 0.91" OLED-driven smart device built on a Seeed XIAO ESP32-S3 Sense, with a Python/FastAPI VPS backend that integrates Google Gemini AI and Telegram. Six operational modes accessible from a tiny menu, no buttons — input is via PDM-microphone tap detection.

> **Status:** v3.32 final. Built as a portfolio project to explore real-world tradeoffs in embedded AI integrations, USB HID automation, and remote agent design.

---

## Hardware

| Component | Role |
|---|---|
| Seeed Studio XIAO ESP32-S3 Sense | Main MCU (8MB Flash, 8MB PSRAM, dual-core 240 MHz) |
| OV2640 camera module (Sense add-on) | Image capture for vision modes |
| PDM microphone (built into Sense) | Voice commands + tap detection |
| 0.91" SSD1306 OLED (128×32, I²C) | UI display, status messages |
| USB-OTG (TinyUSB) | Acts as USB HID Keyboard for IT Support mode |

---

## Architecture

```
┌─────────────────────┐         ┌─────────────────────┐
│  ESP32-S3 (device)  │  WiFi   │   VPS (FastAPI)     │
│                     │ ◄─────► │                     │
│ • OLED UI           │  HTTP   │ • Gemini 2.5 Flash  │
│ • PDM mic (tap +    │         │   (vision + audio)  │
│   voice commands)   │         │ • Gemini 2.5 Pro    │
│ • OV2640 camera     │         │   (IT diagnostics)  │
│ • USB HID keyboard  │         │ • Telegram bot      │
│ • 6 app modes       │         │   (alerts + chat)   │
└─────────────────────┘         └─────────────────────┘
       │                                 ▲
       │ USB HID                         │ Telegram
       ▼                                 │ messages
┌─────────────────────┐                  │
│  Target PC (IT      │ ─────────────────┘
│  Support mode only) │   HTTP POST
└─────────────────────┘
```

---

## Six Modes (tap-navigated)

Tap detection runs on the PDM microphone via amplitude-delta thresholds — no physical buttons. **3× tap** unlocks the device, **1× tap** scrolls, **2× tap** confirms. Each mode supports **3× tap to exit**.

### 1. **Guard** (motion-triggered alerts)
Live camera frames, OpenCV-based motion detection on VPS, multi-frame H.264 video sent to Telegram with a snapshot.

### 2. **AI Assist** (vision Q&A)
Take a photo → Gemini 2.5 Flash describes what it sees in one sentence (≤90 chars to fit OLED). Result paged on display, also sent to Telegram with the image.

### 3. **Classic** (camera shutter)
Photo capture with optional 3-second countdown (toggleable in Settings). Direct send to Telegram.

### 4. **Voice** (spoken commands)
2-second WAV recording → VPS forwards to Gemini Audio → returns one of `guard | ai | photo | settings | sleep | back | next | unknown`. Whichever command comes back gets executed (mode switch, photo capture, etc.).

### 5. **Timelapse** (long-running capture)
Captures a 1080p frame every 2 minutes. Frames buffer in PSRAM until full (≈20 frames), then batch-uploaded to VPS. On stop, FFmpeg stitches frames at 6 fps with H.264 encoding into a single MP4 streamed to Telegram. WiFi is suspended between captures for power savings.

### 6. **IT Support** (remote PC diagnostics — experimental)
Acts as a USB HID keyboard plugged into a Windows PC, registers a session with VPS, polls every 2 s for commands. When the user types `/e <issue>` to the Telegram bot, Gemini 2.5 Pro is given a 4-turn diagnostic loop: it requests PowerShell commands, the device types them on the target PC via Run dialog, results are POSTed back to VPS, and Gemini synthesizes a fix plan. The plan is sent to Telegram with ✅/❌ buttons; on approval the fix runs the same way.

This mode is documented separately below — it works in proof-of-concept form, but production hardening on real-world IT environments requires more work (see "Honest limitations").

### Settings
Photo timer toggle, Guard loop mode, back navigation.

---

## VPS Backend (FastAPI)

`vps_backend/main.py` exposes:

| Endpoint | Purpose |
|---|---|
| `POST /upload_stream` | Guard motion frames → H.264 video → Telegram |
| `POST /ask_ai` | Photo + Gemini description → Telegram |
| `POST /send_photo` | Direct photo to Telegram |
| `POST /send_audio` | Audio sample to Telegram |
| `POST /voice_command` | WAV → Gemini → command name |
| `POST /timelapse_batch` | Buffer frames |
| `POST /timelapse_finalize` | Stitch + send MP4 |
| `POST /it_register` | IT Support session registration |
| `GET /it_poll/{id}` | ESP polls for pending commands |
| `GET /it_script/{id}` | PC fetches the dynamically-generated PowerShell script |
| `POST /it_data/{id}` | PC submits collected data |
| `POST /it_error/{id}` | PC reports execution errors → Telegram |

**Key design decisions:**

- All blocking calls (Gemini, Telegram, requests) wrapped in `asyncio.to_thread` — a single FastAPI process handles polling, websocket-like long polls, and background tasks without blocking.
- Inline MD5/auth handled via Telegram's bot API.
- Generated PowerShell scripts encode each user-supplied command as Base64 so untrusted strings can't break the outer script's parse-time integrity.
- All errors that originate from the PC get a dedicated lifecycle (`script_started` → `post_data` → `post_fix_result`) so the operator sees *which stage* failed in Telegram.

**Setup:**
```bash
cd /opt/smart_os
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn python-multipart opencv-python-headless \
            numpy requests google-genai pillow
apt install ffmpeg
nohup uvicorn main:app --host 0.0.0.0 --port 8003 > /tmp/vps_8003.log 2>&1 &
```

Configure `vps_backend/config.py`:
```python
TELEGRAM_TOKEN      = "..."
CHAT_ID             = "..."
GEMINI_API_KEY      = "..."
GEMINI_MODEL_VISION = "gemini-2.5-flash"
```

---

## Build & Flash

Arduino IDE settings for **Seeed XIAO ESP32-S3**:

| Setting | Value |
|---|---|
| Board | XIAO_ESP32S3 |
| USB Mode | USB-OTG (TinyUSB) |
| USB CDC On Boot | Enabled |
| CPU Frequency | 240MHz (WiFi) |
| Flash Mode | QIO 80MHz |
| Flash Size | 8MB (64Mb) |
| Partition Scheme | Default with spiffs (3MB APP / 1.5MB SPIFFS) |
| PSRAM | OPI PSRAM |
| Upload Speed | 921600 |

Flash via boot+reset combination (USB only, no OTA in this version):
1. Hold **B (BOOT)** button on the XIAO
2. Press **R (RESET)** briefly
3. Release **B**
4. The board is now in download mode; click **Upload** in Arduino IDE

Edit at the top of `esp32_smart_os.ino`:
```cpp
const char* ssid     = "YOUR_WIFI_SSID";
const char* password = "YOUR_WIFI_PASSWORD";
const String vps_host = "YOUR_VPS_IP";
const int    vps_port = 8003;
```

---

## Honest limitations & engineering notes

This project hit several real-world walls; documenting them is the most useful thing for portfolio purposes.

**USB HID keyboard layout dependency.** A USB HID device sends *scancodes* — Windows interprets them via the active keyboard layout. Because most of the target PCs run Turkish Q layout, the same scancode for `:` produces `Ş`, `'` produces `ı`, `-` produces `*`, etc. Several mitigation paths were tried:

- **`Keyboard.print(c)`** — fast (~2 s for the bootstrap) but layout-dependent. Works only on US English layout.
- **ALT+Numpad code** (`ALT+0058` for `:`) — layout-independent through Windows' ANSI codepage entry, works in Notepad/edit controls, but does **not** work in the PowerShell console (conhost handles ALT differently). Also slow (~250 ms per character).
- **Switching layout via Win+Space** before typing — fast and correct, but requires the operator to have added US English to their Windows language list (one-time setup per target PC).
- **Notepad → clipboard → paste** — type into Notepad (where ALT+code works), Ctrl+A/Ctrl+C, close Notepad, open PowerShell, Ctrl+V. Works but visually intrusive (~12 s of windows opening/closing).

The shipping version uses the hybrid typing approach with a Win+Space hint. **For productionizing on arbitrary PCs**, the cleanest solution is composite USB (HID + Mass Storage) — the device exposes a tiny FAT image with the bootstrap script and types only its drive letter. That was scoped out of the portfolio version.

**ESP32-S3 ArduinoOTA + USB-OTG conflict.** Wireless firmware updates worked individually but produced `BEGIN HATA` (Update.begin failure) once USB-OTG HID was active and other resources held heap. Reproducible enough that wireless updates were removed in v3.32 — flashing is via boot+reset only.

**AV detection of bootstrap pattern.** Windows Defender ASR rules pattern-match `iex (iwr 'http://...')` from Run dialog and silently kill PowerShell on launch. Resolved by Base64 `-EncodedCommand` wrapping the inner script. The decoded payload still triggers AMSI scans at runtime, so the *actual* downloader pattern is no more bypassed than before — only the command-line signature is hidden. For real-world use, signing or a trusted launcher would be required.

**Run dialog character limit (≈260).** Combined with Base64 encoding, a typical Gemini-issued PowerShell command is right at the edge. Long scripts must be downloaded over HTTPS and executed via `-File`, which is what the production flow does.

**Telegram polling race.** Two `uvicorn main:app` instances both polling the same bot token will *race* for messages — each `getUpdates` consumes some, leaving randomly-distributed delivery. The deployment script kills any existing instance before starting; it's worth being explicit about because the symptom looks like "the bot ignores my commands sometimes."

**The IT Support mode is a proof of concept.** The full pipeline — `/e bilgisayar yavaş` → Gemini diagnostic → PowerShell on target → Gemini analysis → Telegram approval → fix execution — has been observed end-to-end, but the keyboard-layout dependency keeps it from being plug-and-play across target PCs.

---

## What I'd build next

- **Composite USB device**: HID Keyboard + Mass Storage. The device hosts a tiny FAT image with the bootstrap; the keyboard side only needs to type the drive letter. Layout-independent by construction.
- **Live ESP-side AMSI / Defender bypass detection**: rather than the device deciding the bootstrap format, ask the target PC to identify its security stack via a single benign probe and switch strategy.
- **Sign the inner PowerShell with a self-signed cert** + execution-policy AllSigned on managed devices — turns Gemini-generated scripts into trusted ones for that org.
- **Replace USB HID with a USB CDC + companion service** — a tiny installable service on the PC that listens for commands over a USB serial channel. No layout issues, no AV friction, but requires the user to install something once.
- **Multi-target support**: VPS keeps a registry of devices; one operator can `/e laptop bilgisayar yavaş` to a specific target.

---

## Repository layout

```
esp32_smart_os/
  esp32_smart_os.ino     # main firmware (~3,000 lines, 6 modes + UI)
vps_backend/
  main.py                # FastAPI app
  config.py              # API keys (gitignored)
README.md                # this file
```

---

## License

MIT — feel free to fork. If you build something interesting from this, I'd love to hear about it.
