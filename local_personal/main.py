"""
Smart OS — VPS backend (FastAPI)

Pipes ESP32-S3 device traffic through Google Gemini and Telegram:
    /upload_stream      Guard motion frames -> H.264 video -> Telegram
    /ask_ai             Single photo -> Gemini description -> Telegram
    /send_photo         Direct photo to Telegram
    /send_audio         Audio sample to Telegram (debug)
    /voice_command      WAV -> Gemini Audio -> command word
    /timelapse_batch    Buffer JPEG frames on disk
    /timelapse_finalize Stitch buffered frames into MP4 -> Telegram

IT-Support agent (USB-HID flow on a paired Windows PC):
    /it_register        ESP registers an active session
    /it_poll/{id}       ESP polls every 2 s for a pending command
    /it_script/{id}     Target PC fetches the dynamically-built PowerShell
    /it_data/{id}       Target PC POSTs collected data back
    /it_error/{id}      Target PC reports lifecycle / errors -> Telegram

Telegram bot polling and Gemini calls run inside asyncio.to_thread so
the long-poll / model latency never blocks FastAPI's event loop.

Setup:
    pip install fastapi uvicorn python-multipart opencv-python-headless \\
                numpy requests google-genai pillow
    apt install ffmpeg
    cp config.example.py config.py   # then fill in real values
    uvicorn main:app --host 0.0.0.0 --port 8003
"""

import os
import io
import cv2
import asyncio
import uuid
import json
import base64
import numpy as np
import subprocess
import tempfile
import logging
import requests
import shutil

from datetime import datetime
from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from google import genai
from google.genai import types

import config

# ───── Logging ─────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("smart_os")

# ───── App ─────
app = FastAPI(title="Smart OS Backend", version="3.6")

# ───── Gemini ─────
gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
logger.info(f"Gemini ready: {config.GEMINI_MODEL_VISION}")


# ───── Telegram ─────

def _tg_url(method: str) -> str:
    return f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/{method}"


def send_telegram_message(text: str) -> bool:
    """Send a plain-text Markdown message to the configured chat."""
    try:
        resp = requests.post(
            _tg_url("sendMessage"),
            data={
                "chat_id":    config.CHAT_ID,
                "text":       text,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        ok = resp.status_code == 200
        logger.info(f"Telegram mesaj: {resp.status_code}")
        return ok
    except Exception as e:
        logger.error(f"Telegram mesaj hatasi: {e}")
        return False


def send_telegram_photo(image_bytes: bytes, caption: str) -> bool:
    """Send a JPEG to the chat as a photo (with Markdown caption)."""
    try:
        resp = requests.post(
            _tg_url("sendPhoto"),
            data={
                "chat_id":    config.CHAT_ID,
                "caption":    caption,
                "parse_mode": "Markdown",
            },
            files={"photo": ("image.jpg", image_bytes, "image/jpeg")},
            timeout=30,
        )
        ok = resp.status_code == 200
        logger.info(f"Telegram fotograf: {resp.status_code}")
        return ok
    except Exception as e:
        logger.error(f"Telegram fotograf hatasi: {e}")
        return False


def send_telegram_video(video_path: str, caption: str) -> bool:
    """
    Send an MP4 to the chat as a video.
    supports_streaming=True turns on Telegram's inline player.
    """
    try:
        with open(video_path, "rb") as vf:
            resp = requests.post(
                _tg_url("sendVideo"),
                data={
                    "chat_id":            config.CHAT_ID,
                    "caption":            caption,
                    "parse_mode":         "Markdown",
                    "supports_streaming": "true",
                },
                files={"video": ("alert.mp4", vf, "video/mp4")},
                timeout=90,   # Buyuk video icin uzun timeout
            )
        ok = resp.status_code == 200
        if not ok:
            logger.warning(f"Telegram video yaniti: {resp.status_code} | {resp.text[:200]}")
        else:
            logger.info("Telegram video basariyla gonderildi.")
        return ok
    except Exception as e:
        logger.error(f"Telegram video hatasi: {e}")
        return False


# ───── Video assembly ─────

def create_h264_video(frames: list, output_path: str,
                      width: int, height: int, fps: int = 8) -> bool:
    """
    Stitch a list of OpenCV frames into an H.264 MP4 via FFmpeg.

    Why H.264 specifically:
    - Telegram's inline player only accepts H.264/AAC.
    - OpenCV's mp4v/avc1 outputs vary across builds and Telegram often
      fails to recognize them as playable.
    - libx264 + yuv420p + faststart is a known-good Telegram combo.
    """
    if not frames:
        logger.warning("No frames to encode.")
        return False

    with tempfile.TemporaryDirectory() as tmpdir:
        # Dump each frame as JPEG for FFmpeg's image2 demuxer
        for idx, frame in enumerate(frames):
            if frame.shape[1] != width or frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height))
            cv2.imwrite(
                os.path.join(tmpdir, f"frame_{idx:04d}.jpg"),
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, 88]
            )

        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", os.path.join(tmpdir, "frame_%04d.jpg"),
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "28",                   # 18=high, 28=medium, 35=low
            "-pix_fmt", "yuv420p",          # required by Telegram
            "-movflags", "+faststart",      # moves moov atom to head for streaming
            "-vf", f"scale={width}:{height}",
            output_path,
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=90)
            size_kb = os.path.getsize(output_path) / 1024
            logger.info(f"Video built: {output_path} ({size_kb:.1f} KB, {len(frames)} frames)")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg error:\nSTDOUT: {e.stdout}\nSTDERR: {e.stderr}")
            return False

        except subprocess.TimeoutExpired:
            logger.error("FFmpeg timed out.")
            return False

        except FileNotFoundError:
            logger.error("FFmpeg bulunamadi! 'apt install ffmpeg' ile yukleyin.")
            return False


def create_video_opencv_fallback(frames: list, output_path: str,
                                  width: int, height: int, fps: int = 8) -> bool:
    """
    Fallback when FFmpeg isn't available: OpenCV mp4v writer.
    Note: Telegram may refuse to play this format inline, but the file is sent.
    """
    try:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
        for frame in frames:
            if frame.shape[1] != width or frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height))
            writer.write(frame)
        writer.release()
        logger.warning(f"OpenCV fallback video built: {output_path}")
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except Exception as e:
        logger.error(f"OpenCV video error: {e}")
        return False


# ───── Endpoint: /upload_stream (Guard motion alert) ─────

@app.post("/upload_stream")
async def upload_stream(
    request: Request,
    files: list[UploadFile] = File(...)
):
    """
    Guard motion alert: ESP uploads multiple JPEG frames; we stitch them
    into an H.264 MP4 and push to Telegram.

    Custom headers from ESP:
      X-Frame-Width   frame width  (pixels)
      X-Frame-Height  frame height (pixels)
      X-Frame-FPS     playback fps (default 8)
    """
    logger.info(f"/upload_stream: {len(files)} files received.")

    if not files:
        return JSONResponse(status_code=400, content={"error": "no files"})

    # Pull frame dimensions from headers, default to VGA
    width  = int(request.headers.get("X-Frame-Width",  640))
    height = int(request.headers.get("X-Frame-Height", 480))
    fps    = int(request.headers.get("X-Frame-FPS",    8))
    logger.info(f"Expected frame size: {width}x{height} @ {fps}fps")

    frames = []
    for idx, file in enumerate(files):
        raw = await file.read()
        arr = np.frombuffer(raw, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            frames.append(img)
        else:
            logger.warning(f"Frame {idx} failed to decode, skipping.")

    logger.info(f"Decoded frames: {len(frames)}/{len(files)}")

    if not frames:
        return JSONResponse(
            status_code=400,
            content={"error": "no frames decoded"}
        )

    # Send the first frame immediately as a still — user gets the alert
    # while the video is still being assembled
    _, first_jpg = cv2.imencode(".jpg", frames[0])
    send_telegram_photo(
        first_jpg.tobytes(),
        f"🚨 *GUARD ALARM*\nMotion detected!\n`{len(frames)} frames captured, building video...`"
    )

    video_path = "/tmp/motion_alert.mp4"
    success = create_h264_video(frames, video_path, width, height, fps=fps)

    if not success:
        logger.warning("FFmpeg failed, falling back to OpenCV mp4v...")
        success = create_video_opencv_fallback(frames, video_path, width, height, fps=fps)

    if success and os.path.exists(video_path):
        size_kb = os.path.getsize(video_path) / 1024
        caption = (
            f"🎥 *Motion video*\n"
            f"`Frames: {len(frames)} | Size: {size_kb:.0f} KB`"
        )
        video_sent = send_telegram_video(video_path, caption)
        try:
            os.remove(video_path)
        except Exception:
            pass

        return {
            "status":  "success" if video_sent else "video_send_failed",
            "frames":  len(frames),
            "size_kb": round(size_kb, 1),
        }

    return JSONResponse(
        status_code=500,
        content={"error": "video build failed"}
    )


# ───── Endpoint: /ask_ai (AI Assist single-shot vision) ─────

@app.post("/ask_ai")
async def ask_ai(image: UploadFile = File(...)):
    """
    Run the device's photo through Gemini for a one-line description.
    The result is both echoed back to the ESP (for OLED display) and
    sent to Telegram as the photo's caption.
    """
    logger.info("/ask_ai request received.")

    try:
        raw = await image.read()

        prompt = (
            "What do you see in this photo? "
            "Reply with ONE short English sentence (max 15 words). "
            "It must fit on a tiny OLED screen. "
            "Example: 'A person sitting at a desk using a laptop.'"
        )

        logger.info("Sending image to Gemini...")
        response = gemini_client.models.generate_content(
            model=config.GEMINI_MODEL_VISION,
            contents=[
                prompt,
                types.Part.from_bytes(data=raw, mime_type="image/jpeg"),
            ],
        )
        result = response.text.strip()

        # Cap at 90 chars — OLED can show ~3 lines max
        if len(result) > 90:
            result = result[:87] + "..."

        logger.info(f"Gemini reply: {result}")

        send_telegram_photo(
            raw,
            f"🤖 *AI Assist*\n_{result}_"
        )

        return {"result": result}

    except Exception as e:
        logger.error(f"AI analysis error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"result": "AI Error"}
        )


# ───── Endpoint: /send_photo (Classic mode pass-through) ─────

@app.post("/send_photo")
async def send_photo(image: UploadFile = File(...)):
    """Classic mode: forward photo to Telegram, no AI processing."""
    logger.info("/send_photo request.")
    try:
        raw = await image.read()
        sent = send_telegram_photo(raw, "📷 *Classic Photo*")
        return {"status": "ok" if sent else "error"}
    except Exception as e:
        logger.error(f"send_photo error: {e}")
        return JSONResponse(status_code=500, content={"status": "error"})


# ───── Endpoint: /send_audio (mic test) ─────

@app.post("/send_audio")
async def send_audio(audio: UploadFile = File(...)):
    """
    Forward a WAV blob from the device to Telegram as a document.
    ESP32 doesn't speak HTTPS, so this endpoint is the proxy.
    """
    logger.info("/send_audio request.")
    try:
        raw = await audio.read()
        size_kb = len(raw) / 1024
        logger.info(f"WAV received: {size_kb:.1f} KB")

        resp = requests.post(
            _tg_url("sendDocument"),
            data={
                "chat_id": config.CHAT_ID,
                "caption": f"🎤 *Mic quality test*\n`WAV | 16kHz | 16-bit mono | 5s | {size_kb:.0f} KB`",
                "parse_mode": "Markdown",
            },
            files={"document": ("mic_test.wav", raw, "audio/wav")},
            timeout=60,
        )
        ok = resp.status_code == 200
        logger.info(f"Telegram audio: {resp.status_code}")
        return {"status": "ok" if ok else "error", "size_kb": round(size_kb, 1)}
    except Exception as e:
        logger.error(f"send_audio error: {e}")
        return JSONResponse(status_code=500, content={"status": "error"})


# ───── Endpoint: /voice_command (Voice mode) ─────

@app.post("/voice_command")
async def voice_command(audio: UploadFile = File(...)):
    """
    Interpret a 2-second WAV from the device as one of the menu commands.

    Flow: ESP -> HTTP POST /voice_command -> Gemini Audio -> command word.

    Supported commands (Turkish or English):
        guard, ai, photo, settings, sleep, back, next, unknown
    """
    logger.info("/voice_command request received.")
    try:
        raw = await audio.read()
        size_kb = len(raw) / 1024
        logger.info(f"Voice command received: {size_kb:.1f} KB")

        prompt = (
            "You are a voice command interpreter for a smart camera device. "
            "Listen to this short voice recording (max 2 seconds) and identify the spoken command. "
            "The user speaks Turkish or English. "
            "Map what you hear to exactly ONE of these command names:\n"
            "  guard    → 'guard', 'güvenlik', 'koruma', 'izle', 'security'\n"
            "  ai       → 'ai', 'yapay zeka', 'analiz', 'ne var', 'ne görüyorsun', 'analyze'\n"
            "  photo    → 'fotoğraf', 'foto', 'çek', 'klasik', 'photo', 'picture'\n"
            "  settings → 'ayarlar', 'ayar', 'settings', 'setting'\n"
            "  sleep    → 'uyu', 'kapat', 'uyku', 'sleep'\n"
            "  back     → 'geri', 'menü', 'iptal', 'back', 'menu', 'ana menü', 'cancel'\n"
            "  next     → 'ileri', 'sonraki', 'diğer', 'next'\n"
            "  unknown  → anything else or unclear / background noise only\n\n"
            "Respond with ONLY the single command word in lowercase, nothing else."
        )

        logger.info("Gemini voice analysis starting...")
        response = gemini_client.models.generate_content(
            model=config.GEMINI_MODEL_VISION,
            contents=[
                prompt,
                types.Part.from_bytes(data=raw, mime_type="audio/wav"),
            ],
        )
        command = response.text.strip().lower()

        # Reject anything that's not a known command word
        valid_commands = {"guard", "ai", "photo", "settings", "sleep", "back", "next", "unknown"}
        if command not in valid_commands:
            logger.warning(f"Gemini returned unrecognized word: '{command}' -> 'unknown'.")
            command = "unknown"

        logger.info(f"Voice command interpreted as: '{command}'")
        return {"command": command, "raw_text": response.text.strip()}

    except Exception as e:
        logger.error(f"voice_command error: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"command": "unknown", "error": str(e)}
        )


# ───── Endpoint: /timelapse_batch (incremental upload) ─────

TL_SESSION_DIR = "/tmp/tl_session"


@app.post("/timelapse_batch")
async def timelapse_batch(files: list[UploadFile] = File(...)):
    """
    Persist incoming JPEGs to disk without running FFmpeg.
    The device fills PSRAM, sends a batch, frees PSRAM, repeats.
    Frames accumulate as /tmp/tl_session/frame_XXXXXX.jpg until
    /timelapse_finalize stitches them in one shot.
    """
    os.makedirs(TL_SESSION_DIR, exist_ok=True)

    # Continue numbering after any frames already on disk
    existing = len([f for f in os.listdir(TL_SESSION_DIR) if f.endswith(".jpg")])
    saved = 0

    for idx, file in enumerate(files):
        raw = await file.read()
        if not raw:
            logger.warning(f"Timelapse batch: empty file at {idx}, skipping.")
            continue
        frame_path = os.path.join(TL_SESSION_DIR, f"frame_{existing + idx:06d}.jpg")
        with open(frame_path, "wb") as f:
            f.write(raw)
        saved += 1

    total = existing + saved
    logger.info(f"/timelapse_batch: +{saved} frames saved (total: {total})")
    return {"status": "ok", "saved": saved, "total": total}


# ───── Endpoint: /timelapse_finalize (stitch + send) ─────

@app.post("/timelapse_finalize")
async def timelapse_finalize():
    """
    Stitch all buffered JPEGs into a single H.264 MP4 and ship it
    to Telegram. The /tmp/tl_session directory is wiped afterwards.

    Timelapse pacing:
      - One frame every 2 minutes (real time)
      - 6 fps playback => 1 real hour ≈ 30 frames ≈ 5 sec of video
    """
    if not os.path.isdir(TL_SESSION_DIR):
        logger.warning("/timelapse_finalize: session directory missing.")
        return JSONResponse(status_code=404, content={"error": "no timelapse session"})

    frames = sorted([f for f in os.listdir(TL_SESSION_DIR) if f.endswith(".jpg")])
    if not frames:
        logger.warning("/timelapse_finalize: session directory empty.")
        return JSONResponse(status_code=404, content={"error": "no frames"})

    logger.info(f"/timelapse_finalize: {len(frames)} frames -> building video...")

    video_path = "/tmp/timelapse_output.mp4"

    # -pattern_type glob handles numerically-named frames in order
    cmd = [
        "ffmpeg", "-y",
        "-framerate", "6",
        "-pattern_type", "glob",
        "-i", os.path.join(TL_SESSION_DIR, "frame_*.jpg"),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        video_path,
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg error:\nSTDOUT: {e.stdout}\nSTDERR: {e.stderr}")
        return JSONResponse(status_code=500, content={"error": "ffmpeg failed"})
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg timed out (120s).")
        return JSONResponse(status_code=500, content={"error": "ffmpeg timeout"})
    except FileNotFoundError:
        logger.error("FFmpeg not installed.")
        return JSONResponse(status_code=500, content={"error": "ffmpeg missing"})

    if not os.path.exists(video_path):
        return JSONResponse(status_code=500, content={"error": "no output file"})

    size_kb = os.path.getsize(video_path) / 1024
    logger.info(f"Timelapse video built: {size_kb:.1f} KB, {len(frames)} frames")

    # Translate frame count to real-world elapsed time (2-min interval)
    total_minutes = len(frames) * 2
    hours   = total_minutes // 60
    minutes = total_minutes % 60
    if hours > 0:
        time_str = f"{hours}h {minutes}m" if minutes > 0 else f"{hours}h"
    else:
        time_str = f"{minutes}m"

    caption = (
        f"⏱ *Timelapse*\n"
        f"`{len(frames)} frames | {size_kb:.0f} KB`\n"
        f"🕐 Real elapsed: *{time_str}*"
    )
    sent = send_telegram_video(video_path, caption)

    # Cleanup whether send succeeded or not
    try:
        os.remove(video_path)
    except Exception:
        pass
    try:
        shutil.rmtree(TL_SESSION_DIR, ignore_errors=True)
    except Exception:
        pass

    if sent:
        logger.info("Timelapse delivered to Telegram, session cleared.")
        return {"status": "ok", "frames": len(frames), "size_kb": round(size_kb, 1)}
    else:
        logger.error("Built timelapse video but failed to send to Telegram.")
        return JSONResponse(
            status_code=500,
            content={"error": "video built but Telegram send failed",
                     "frames": len(frames), "size_kb": round(size_kb, 1)}
        )


# ───── Endpoint: /health ─────

@app.get("/health")
async def health():
    """Liveness probe."""
    return {
        "status":  "ok",
        "version": "3.6",
        "model":   config.GEMINI_MODEL_VISION,
    }


# ═════════════════════════════════════════════════════════════
#  IT-Support agent
#
#  Triggered by /e <issue> in Telegram. Gemini 2.5 Pro runs a
#  bounded diagnostic loop: requests PowerShell commands, the
#  device types them on the target PC via USB HID, results are
#  POSTed back, Gemini synthesizes a fix plan, the user approves
#  via inline buttons, the fix runs the same way.
#
#  Flow:
#    /e <issue> -> session opens -> Gemini collect_data round
#                -> ESP types bootstrap on PC -> PC POSTs data
#                -> Gemini repeats up to 4 turns -> present_plan
#                -> ✅/❌ buttons -> approved fix executes
# ═════════════════════════════════════════════════════════════
# =====================================================================

GEMINI_IT_MODEL  = "gemini-2.5-pro"          # deeper reasoning for diagnostics
# IT-Support callback host/port the target PC POSTs back to.
# Pulled from config.py (see config.example.py).
VPS_HOST_IT      = getattr(config, "VPS_HOST", "127.0.0.1")
VPS_PORT_IT      = getattr(config, "VPS_PORT", 8003)

# Active IT sessions: session_id (str) -> dict
it_sessions: dict        = {}
_tg_offset: int          = 0
_active_it_device: str   = ""        # session_id of the most recently registered device
_awaiting_feedback: dict = {}        # chat_id -> session_id (waiting for rejection feedback)

# Gemini system prompt — Turkish on purpose because the Telegram operator
# is Turkish-speaking; Gemini's "diagnosis" / "fix_steps" output is shown
# verbatim to them. Switch to English freely if you ship to an EN audience.
IT_SYSTEM_PROMPT = """\
Sen bir Windows IT teşhis ajanısın. USB bağlı bir cihaz aracılığıyla
hedef PC'de PowerShell komutları çalıştırabilir ve sonuçlarını alabilirsin.

AMAÇ: Kullanıcının bildirdiği Windows sorununu sistematik olarak teşhis et
ve çözüm planı oluştur.

ÇALIŞMA PRENSİBİ:
1. Her turda yalnızca o aşama için gerçekten gerekli komutları iste.
2. Maksimum 4 veri toplama turu — daha fazlası kullanıcıyı bekletir.
3. Teşhis komutları salt okunur ve güvenli olmalı.
4. Fix komutları net, sıralı ve tercihen geri alınabilir olmalı.
5. Risk seviyesini gerçekçi değerlendir.

YASAK KOMUTLAR (hiçbir koşulda kullanma):
- format, diskpart (disk silme/biçimlendirme)
- reg delete HKLM\\SYSTEM\\* (kritik sistem kayıt defteri silme)
- Remove-Item C:\\Windows\\System32\\* (sistem dosyaları)
- Her türlü kullanıcı verisi silme komutu

ÇIKIŞ FORMATI — Her yanıt geçerli bir JSON nesnesi olmalı.
Markdown, açıklama, kod bloğu veya JSON dışı metin YAZMA.

Veri toplarken:
{
  "action": "collect_data",
  "thought": "Hangi veriyi neden istediğimi açıklıyorum",
  "commands": [
    "Get-Service wuauserv | Select Name,Status | ConvertTo-Json -Compress",
    "Get-PSDrive C | Select Name,Used,Free | ConvertTo-Json -Compress"
  ],
  "collecting_reason": "Kullanıcıya gösterilecek kısa Türkçe açıklama"
}

Plan sunarken:
{
  "action": "present_plan",
  "thought": "Verileri analiz ettim, sorun ve çözüm şunlar...",
  "diagnosis": "Sorunun kısa, anlaşılır Türkçe açıklaması",
  "root_cause": "Sorunun teknik kök nedeni",
  "fix_steps": [
    "1. Windows Update servisleri durdurulacak",
    "2. Bozuk önbellek temizlenecek",
    "3. Servisler yeniden başlatılacak"
  ],
  "fix_commands": [
    "Stop-Service wuauserv,bits -Force -ErrorAction SilentlyContinue",
    "Remove-Item 'C:\\\\Windows\\\\SoftwareDistribution\\\\Download' -Recurse -Force -ErrorAction SilentlyContinue",
    "Start-Service bits,wuauserv"
  ],
  "risk": "low",
  "estimated_minutes": 3
}
"""


# ───── Telegram polling (started on FastAPI startup) ─────
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(telegram_poll_loop())
    logger.info("IT Support agent started.")


async def telegram_poll_loop():
    """
    Long-poll Telegram for incoming messages and callback queries.
    requests.get is blocking, so we wrap in asyncio.to_thread —
    otherwise the 8-second long-poll would freeze the FastAPI loop
    and callbacks would queue up / time out.
    """
    global _tg_offset
    logger.info("Telegram polling started.")
    while True:
        try:
            resp = await asyncio.to_thread(
                requests.get,
                _tg_url("getUpdates"),
                params={
                    "offset":           _tg_offset,
                    "timeout":          8,
                    "allowed_updates":  '["message","callback_query"]',
                },
                timeout=12,
            )
            if resp.status_code == 200:
                for update in resp.json().get("result", []):
                    _tg_offset = update["update_id"] + 1
                    asyncio.create_task(handle_telegram_update(update))
        except Exception as e:
            logger.debug(f"Telegram poll: {e}")
        await asyncio.sleep(0.8)


async def handle_telegram_update(update: dict):
    """Route an incoming Telegram update (message or button callback)."""
    global _awaiting_feedback

    # ── Inline button callbacks (✅ / ❌ on the diagnosis plan) ─────
    if "callback_query" in update:
        cq      = update["callback_query"]
        cq_id   = cq["id"]
        data    = cq.get("data", "")
        chat_id = str(cq["message"]["chat"]["id"])
        msg_id  = cq["message"]["message_id"]

        requests.post(_tg_url("answerCallbackQuery"),
                      json={"callback_query_id": cq_id}, timeout=5)

        if data.startswith("it_approve_"):
            await handle_it_approve(data.replace("it_approve_", ""), msg_id)
        elif data.startswith("it_reject_"):
            await handle_it_reject(data.replace("it_reject_", ""), msg_id, chat_id)
        return

    # ── Normal text messages ──────────────────────────────────────
    if "message" not in update:
        return

    msg     = update["message"]
    chat_id = str(msg["chat"]["id"])
    text    = msg.get("text", "").strip()

    if chat_id != config.CHAT_ID:
        return          # Ignore messages from other chats

    # /e <issue>: open a new IT support session
    if text.startswith("/e "):
        problem = text[3:].strip()
        if not problem:
            send_telegram_message("⚠️ Kullanım: `/e sorun açıklaması`")
            return
        if not _active_it_device:
            send_telegram_message(
                "⚠️ Bağlı IT cihazı yok.\n"
                "ESP32'yi PC'ye takın ve `/it_status` ile kontrol edin."
            )
            return
        await start_it_session(_active_it_device, problem)
        return

    # /test or /test_typing: ESP types a known string into Notepad on
    # the target PC so the operator can visually confirm character output
    # (used to debug keyboard-layout issues during development)
    if text == "/test_typing" or text == "/test":
        if not _active_it_device:
            send_telegram_message("⚠️ Bağlı IT cihazı yok. ESP32'yi IT moduna alın.")
            return
        sess = it_sessions.get(_active_it_device)
        if not sess:
            send_telegram_message("⚠️ Aktif oturum kayıtlı değil.")
            return
        sess["pending_cmd"] = {"type": "test_keyboard", "ready": True}
        send_telegram_message(
            f"⌨️ *Klavye Testi #{_active_it_device}*\n"
            "ESP32 birazdan PC'de Notepad açıp test stringini yazacak.\n"
            "Yaklaşık 30 saniye sürer. Çıktıyı gözle kontrol et — özel "
            "karakterler doğru yazıldı mı?"
        )
        return

    # /it_status: print active device + session state for debugging
    if text == "/it_status":
        if not _active_it_device:
            send_telegram_message("📭 Bağlı IT cihazı yok.")
        else:
            sess = it_sessions.get(_active_it_device, {})
            send_telegram_message(
                f"📡 *Aktif Cihaz:* `#{_active_it_device}`\n"
                f"State: `{sess.get('state', 'unknown')}`\n"
                f"PC script çalıştı mı: `{sess.get('pc_script_running', False)}`"
            )
        return

    # If the operator just hit ❌ on a plan, the next free-text message
    # is the rejection reason — feed it back to Gemini to revise
    if chat_id in _awaiting_feedback:
        session_id = _awaiting_feedback.pop(chat_id)
        sess = it_sessions.get(session_id)
        if sess and sess["state"] == "revising":
            sess["history"].append({
                "role":    "user",
                "content": (
                    f"Kullanıcı planı reddetti. Gerekçe: {text}\n"
                    "Lütfen bu geri bildirimi dikkate alarak planı revize et "
                    "ve yeni bir present_plan ile yanıt ver."
                ),
            })
            sess["state"] = "collecting"
            send_telegram_message(f"🔄 *#{session_id}* Gemini planı revize ediyor...")
            asyncio.create_task(run_diagnostic_loop(session_id))


# ───── Session lifecycle ─────
async def start_it_session(session_id: str, problem: str):
    sess = it_sessions.get(session_id)
    if sess is None:
        send_telegram_message(
            f"⚠️ Oturum `#{session_id}` bulunamadı. ESP32 IT moduna girip yeniden bağlanmalı."
        )
        return

    sess.update({
        "state":             "collecting",
        "problem":           problem,
        "history":           [],
        "turn":              0,
        "plan":              None,
        "data_event":        asyncio.Event(),
        "latest_data":       None,
        "pending_cmd":       None,
        "pc_script_running": False,
    })

    send_telegram_message(
        f"🔍 *IT Oturumu #{session_id}*\n"
        f"📋 Sorun: _{problem}_\n\n"
        f"⏳ Gemini analiz başlıyor..."
    )
    asyncio.create_task(run_diagnostic_loop(session_id))


# ───── Main diagnostic loop ─────
async def run_diagnostic_loop(session_id: str):
    """
    Up to 4 rounds of Gemini-driven data collection, then a fix plan.
    Each round:
      1. Ask Gemini -> get either collect_data or present_plan
      2. collect_data: queue commands for the ESP, wait for the PC's POST
      3. Data arrives -> add to Gemini history, ask again
      4. present_plan: ship to Telegram with ✅/❌ buttons, exit loop
    """
    sess = it_sessions.get(session_id)
    if not sess:
        return

    for turn in range(4):
        sess["turn"] = turn

        gemini_resp = await ask_gemini_it(session_id)
        if not gemini_resp:
            send_telegram_message(f"❌ *#{session_id}* Gemini yanıt vermedi.")
            return

        action = gemini_resp.get("action")

        # ── Round of collect_data: queue commands, wait for PC ─────
        if action == "collect_data":
            commands = gemini_resp.get("commands", [])
            reason   = gemini_resp.get("collecting_reason", "Sistem verisi toplanıyor...")

            sess["pending_cmd"]       = {"type": "collect", "commands": commands, "ready": True}
            sess["pc_script_running"] = False    # reset for this round

            send_telegram_message(
                f"📊 *#{session_id} — Adım {turn + 1}/4*\n_{reason}_\n"
                f"⌨️ ESP32 komutu PC'ye yazıyor..."
            )

            # Wait for the PC to POST its data — chunked so we can send
            # interim "still waiting" updates to Telegram
            sess["data_event"].clear()
            total_wait = 90
            chunk      = 20
            data_ok    = False
            for waited in range(0, total_wait, chunk):
                remaining = total_wait - waited
                try:
                    await asyncio.wait_for(
                        sess["data_event"].wait(), timeout=min(chunk, remaining)
                    )
                    data_ok = True
                    break
                except asyncio.TimeoutError:
                    if waited + chunk < total_wait:
                        # Different message depending on whether the
                        # script reported "started" via /it_error
                        if sess.get("pc_script_running"):
                            send_telegram_message(
                                f"⏳ *#{session_id}* PowerShell çalışıyor, sonuç bekleniyor "
                                f"({total_wait - waited - chunk}sn kaldı)..."
                            )
                        else:
                            send_telegram_message(
                                f"⏳ *#{session_id}* PC'den henüz tepki yok "
                                f"({total_wait - waited - chunk}sn kaldı)..."
                            )
            if not data_ok:
                if sess.get("pc_script_running"):
                    send_telegram_message(
                        f"⚠️ *#{session_id}* PowerShell başladı fakat veriyi gönderemedi.\n"
                        f"PC'de çalışan güvenlik yazılımı veya internet kesintisi olabilir.\n"
                        f"`%TEMP%\\it_diag_debug.json` dosyasını kontrol edin."
                    )
                else:
                    send_telegram_message(
                        f"⚠️ *#{session_id}* PC PowerShell hiç tetiklenmedi (90sn).\n"
                        f"Olası sebepler:\n"
                        f"• ESP32 PC'ye USB ile bağlı değil\n"
                        f"• Klavye odağı başka pencerede (Run dialogu açılmadı)\n"
                        f"• Windows ekran kilitli\n"
                        f"ESP32 OLED'de IT mod aktif mi kontrol edin (3x tap ile çıkıp tekrar girin)."
                    )
                return

            # Append the collected data to Gemini's conversation history
            data_str = json.dumps(sess.get("latest_data", {}),
                                  ensure_ascii=False, indent=2)
            sess["history"].append({
                "role":    "user",
                "content": f"Toplanan sistem verisi:\n{data_str}",
            })

        elif action == "present_plan":
            await present_plan_to_telegram(session_id, gemini_resp)
            return

        else:
            logger.warning(f"#{session_id}: unknown Gemini action: {action}")
            break

    # 4 rounds elapsed without a plan — force one
    logger.warning(f"#{session_id}: max rounds reached, forcing plan.")
    await force_final_plan(session_id)


# ───── Gemini turn (build context, call model, parse JSON) ─────
async def ask_gemini_it(session_id: str) -> dict | None:
    sess = it_sessions[session_id]

    # First turn: prime the conversation with the user's reported problem
    if not sess["history"]:
        sess["history"].append({
            "role":    "user",
            "content": f"Windows sorun bildirimi: {sess['problem']}",
        })
    else:
        sess["history"].append({
            "role":    "user",
            "content": "Toplanan verileri analiz et. Yeterli veri varsa plan sun, "
                       "yoksa bir sonraki adım için komut iste.",
        })

    # Translate our session history into Gemini's Content/Part shape
    contents = [
        types.Content(
            role="user" if m["role"] == "user" else "model",
            parts=[types.Part.from_text(text=m["content"])],
        )
        for m in sess["history"]
    ]

    try:
        # generate_content is blocking — keep FastAPI's loop free
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=GEMINI_IT_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=IT_SYSTEM_PROMPT,
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )

        raw = response.text.strip()
        logger.info(f"Gemini IT [{session_id}] reply: {raw[:300]}")

        result = json.loads(raw)
        sess["history"].append({"role": "model", "content": raw})
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Gemini JSON parse error: {e} | Raw: {response.text[:200]}")
        return None
    except Exception as e:
        logger.error(f"Gemini IT error: {e}", exc_info=True)
        return None


# ───── Send the plan to Telegram with ✅/❌ inline buttons ─────
async def present_plan_to_telegram(session_id: str, plan: dict):
    sess = it_sessions[session_id]
    sess["state"] = "awaiting_approval"
    sess["plan"]  = plan

    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(
        plan.get("risk", "low"), "⚪"
    )
    steps_text = "\n".join(plan.get("fix_steps", []))

    text = (
        f"🩺 *Teşhis Tamamlandı — #{session_id}*\n\n"
        f"❗ *Sorun:* {plan.get('diagnosis', '—')}\n"
        f"🔎 *Kök Neden:* {plan.get('root_cause', '—')}\n\n"
        f"🛠 *Çözüm Planı:*\n{steps_text}\n\n"
        f"{risk_emoji} Risk: `{plan.get('risk','?').upper()}` | "
        f"⏱ Süre: ~{plan.get('estimated_minutes', '?')} dk"
    )

    markup = {
        "inline_keyboard": [[
            {"text": "✅ Onayla",
             "callback_data": f"it_approve_{session_id}"},
            {"text": "❌ Reddet",
             "callback_data": f"it_reject_{session_id}"},
        ]]
    }

    resp = requests.post(
        _tg_url("sendMessage"),
        json={
            "chat_id":      config.CHAT_ID,
            "text":         text,
            "parse_mode":   "Markdown",
            "reply_markup": markup,
        },
        timeout=10,
    )
    sess["plan_message_id"] = resp.json().get("result", {}).get("message_id")


# ───── User approves the plan: queue fix commands and wait ─────
async def handle_it_approve(session_id: str, message_id: int):
    sess = it_sessions.get(session_id)
    if not sess or sess["state"] != "awaiting_approval":
        return

    sess["state"] = "executing"

    # Strip the inline buttons so they can't be re-clicked
    requests.post(_tg_url("editMessageReplyMarkup"), json={
        "chat_id":      config.CHAT_ID,
        "message_id":   message_id,
        "reply_markup": {"inline_keyboard": []},
    }, timeout=5)

    send_telegram_message(f"⚙️ *#{session_id}* Fix uygulanıyor...")

    # Hand the fix commands to the device polling loop
    fix_cmds = sess["plan"].get("fix_commands", [])
    sess["pending_cmd"] = {"type": "fix", "commands": fix_cmds, "ready": True}

    # Wait for the PC's fix-result POST (chunked, max 120 s)
    sess["data_event"].clear()
    total_wait = 120
    chunk      = 25
    fix_ok     = False
    for waited in range(0, total_wait, chunk):
        remaining = total_wait - waited
        try:
            await asyncio.wait_for(
                sess["data_event"].wait(), timeout=min(chunk, remaining)
            )
            fix_ok = True
            break
        except asyncio.TimeoutError:
            if waited + chunk < total_wait:
                send_telegram_message(
                    f"⚙️ *#{session_id}* Fix uygulanıyor "
                    f"({total_wait - waited - chunk}sn kaldı)..."
                )
    if fix_ok:
        result  = sess.get("latest_data", {})
        success = result.get("success", True)
        msg_txt = result.get("message", "İşlem tamamlandı.")
        icon    = "✅" if success else "⚠️"
        send_telegram_message(f"{icon} *#{session_id}* {msg_txt}")
    else:
        send_telegram_message(
            f"⚠️ *#{session_id}* Fix 120 saniye içinde tamamlanamadı. "
            f"PC'yi manuel kontrol edin."
        )

    sess["state"] = "done"


# ───── User rejects the plan: ask for free-text reason ─────
async def handle_it_reject(session_id: str, message_id: int, chat_id: str):
    global _awaiting_feedback
    sess = it_sessions.get(session_id)
    if not sess:
        return

    sess["state"] = "revising"
    _awaiting_feedback[chat_id] = session_id

    requests.post(_tg_url("editMessageReplyMarkup"), json={
        "chat_id":      config.CHAT_ID,
        "message_id":   message_id,
        "reply_markup": {"inline_keyboard": []},
    }, timeout=5)

    send_telegram_message(
        f"✏️ *#{session_id}* Plan reddedildi.\n"
        "Revizyon için gerekçenizi yazın:"
    )


# ───── Force a plan if the loop ran out of rounds without one ─────
async def force_final_plan(session_id: str):
    sess = it_sessions[session_id]
    sess["history"].append({
        "role":    "user",
        "content": (
            "Yeterli veri toplandı. Artık kesin teşhis ve çözüm planını sun. "
            "present_plan aksiyonu ile yanıt ver."
        ),
    })
    gemini_resp = await ask_gemini_it(session_id)
    if gemini_resp and gemini_resp.get("action") == "present_plan":
        await present_plan_to_telegram(session_id, gemini_resp)
    else:
        send_telegram_message(f"❌ *#{session_id}* Gemini plan oluşturamadı.")


# ───── Endpoint: /it_register (ESP registers an active session) ─────
_last_register_at: float = 0.0          # spam-protection for the Telegram "connected" notice

@app.post("/it_register")
async def it_register(request: Request):
    """
    The ESP calls this when entering IT mode and gets back a session_id.
    The most-recently-registered device becomes the active target.

    Spam guard: a device registering twice within 30 s won't trigger
    another Telegram "connected" notice, but the session is preserved.
    """
    global _active_it_device, _last_register_at

    try:
        body = await request.json()
        session_id = body.get("session_id") or str(uuid.uuid4())[:8].upper()
    except Exception:
        session_id = str(uuid.uuid4())[:8].upper()

    # If a non-idle session already exists for this id (e.g. ESP retry),
    # don't blow away its state.
    existing = it_sessions.get(session_id)
    if existing and existing.get("state") not in (None, "idle"):
        logger.info(f"IT device re-registering: #{session_id} (state={existing['state']})")
    else:
        it_sessions[session_id] = {
            "state":             "idle",
            "problem":           None,
            "history":           [],
            "turn":              0,
            "plan":              None,
            "pending_cmd":       None,
            "latest_data":       None,
            "data_event":        asyncio.Event(),
            "pc_script_running": False,
        }

    _active_it_device = session_id

    # One Telegram notification per 30 seconds at most
    now_ts = asyncio.get_event_loop().time()
    if now_ts - _last_register_at > 30:
        send_telegram_message(
            f"🔌 *IT Destek Cihazı Bağlandı*\n"
            f"Oturum: `#{session_id}`\n"
            f"`/e sorun açıklaması` ile teşhis başlatabilirsiniz."
        )
        _last_register_at = now_ts

    logger.info(f"IT device registered: #{session_id}")
    return {"session_id": session_id, "status": "registered"}


def _build_encoded_bootstrap(session_id: str) -> str:
    """
    Build the one-line PowerShell command the ESP types into the PC's
    open PowerShell console. All lowercase (PS is case-insensitive)
    so the device's HID typing avoids extra ALT-code escapes.

    Output: iex (iwr 'http://VPS:PORT/it_script/SESSION' -usebasicparsing).content
    """
    return (
        f"iex (iwr 'http://{VPS_HOST_IT}:{VPS_PORT_IT}/it_script/{session_id}' "
        f"-usebasicparsing).content"
    )


# ───── Endpoint: /it_poll (ESP polls every 2 s) ─────
@app.get("/it_poll/{session_id}")
async def it_poll(session_id: str):
    """
    The ESP polls this every 2 seconds. If a command is pending we
    return its type plus the bootstrap line to type. Otherwise null.

    Response shape when a command is queued:
        {
          "cmd": {"type": "collect", ...},
          "bootstrap": "iex (iwr 'http://VPS/it_script/SESSION' -usebasicparsing).content"
        }

    For test_keyboard the bootstrap is "" — the ESP runs its own
    Notepad-typing self-test instead of calling the script endpoint.
    """
    sess = it_sessions.get(session_id)
    if not sess:
        return {"cmd": None}

    cmd = sess.get("pending_cmd")
    if cmd and cmd.get("ready"):
        cmd["ready"] = False    # one-shot: don't deliver the same command twice

        cmd_type = cmd.get("type", "collect")
        if cmd_type == "test_keyboard":
            return {"cmd": cmd, "bootstrap": ""}

        # collect/fix: encoded bootstrap olustur
        bootstrap = _build_encoded_bootstrap(session_id)
        logger.info(
            f"IT poll #{session_id}: cmd={cmd_type}, "
            f"bootstrap_len={len(bootstrap)}"
        )
        return {"cmd": cmd, "bootstrap": bootstrap}

    return {"cmd": None}


# ───── Endpoint: /it_data (PC POSTs collected results back) ─────
@app.post("/it_data/{session_id}")
async def it_receive_data(session_id: str, request: Request):
    """
    The PowerShell running on the target PC POSTs its collected JSON
    here. Both diagnostic data and fix-result reports come through
    the same endpoint — distinguished by content shape.
    """
    sess = it_sessions.get(session_id)
    if not sess:
        return JSONResponse(status_code=404, content={"ok": False})

    try:
        body = await request.json()
    except Exception:
        raw = await request.body()
        body = {"raw": raw.decode("utf-8", errors="replace")}

    sess["latest_data"] = body
    sess["data_event"].set()        # wake up the diagnostic loop

    logger.info(f"IT data received: #{session_id} | {len(str(body))} bytes")
    return {"ok": True}


# ───── Endpoint: /it_script (target PC fetches the actual script) ─────
@app.get("/it_script/{session_id}")
async def it_script(session_id: str):
    """
    The target PC's PowerShell first downloads this script via iwr,
    then executes it. We dynamically build it from whatever Gemini
    queued in pending_cmd for the session.

    Robustness:
      - Each Gemini-supplied command runs inside its own try/catch,
        so one failure doesn't kill the rest.
      - Commands are embedded as Base64 — Gemini can include any
        characters ({, }, quotes, backticks) without breaking the
        outer PowerShell parser.
      - On POST failure, /it_error gets a fallback notification.
      - As a last resort the result is dumped to %TEMP%\\it_diag_debug.json
      - Metadata (PS version, user, computer, OS) is always included.
    """
    sess = it_sessions.get(session_id)
    if not sess:
        return PlainTextResponse(
            "# Session not found: " + session_id,
            media_type="text/plain"
        )

    cmd       = sess.get("pending_cmd") or {}
    cmd_type  = cmd.get("type", "collect")
    commands  = cmd.get("commands", [])
    data_url  = f"http://{VPS_HOST_IT}:{VPS_PORT_IT}/it_data/{session_id}"
    error_url = f"http://{VPS_HOST_IT}:{VPS_PORT_IT}/it_error/{session_id}"

    # Base64-encode each Gemini command so the outer PowerShell parser
    # can always tokenize the script regardless of what's inside.
    cmds_b64_array = ",".join(
        f"'{base64.b64encode(c.encode('utf-8')).decode('ascii')}'"
        for c in commands
    )

    # ── collect_data: gather, POST results back ──
    if cmd_type == "collect":
        script = f"""\
$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

# Lifecycle ping — let the VPS know the script actually started
try {{
    Invoke-RestMethod -Uri '{error_url}' -Method POST -UseBasicParsing -TimeoutSec 5 `
        -ContentType 'application/json' `
        -Body (@{{stage='script_started'; cmd_count={len(commands)}}} | ConvertTo-Json -Compress)
}} catch {{ }}

$r = @{{}}
$r['_meta'] = @{{
    started_at  = (Get-Date).ToString('o')
    ps_version  = $PSVersionTable.PSVersion.ToString()
    user        = $env:USERNAME
    computer    = $env:COMPUTERNAME
    os          = try {{ (Get-CimInstance Win32_OperatingSystem).Caption }} catch {{ 'unknown' }}
}}

# Run each Base64-embedded command in its own try/catch
$cmdsB64 = @({cmds_b64_array})
for ($i = 0; $i -lt $cmdsB64.Count; $i++) {{
    $cmd = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($cmdsB64[$i]))
    try {{
        $tmp = Invoke-Expression $cmd 2>&1 | Out-String
        $r["cmd$i"] = $tmp.Trim()
    }} catch {{
        $r["cmd$($i)_err"] = $_.ToString()
    }}
}}

$body = $r | ConvertTo-Json -Depth 6 -Compress
try {{
    Invoke-RestMethod -Uri '{data_url}' -Method POST -UseBasicParsing `
        -Body $body -ContentType 'application/json' -TimeoutSec 30
}} catch {{
    # POST basarisiz - error endpoint'e bildir
    try {{
        $errBody = @{{
            stage     = 'post_data'
            error     = $_.ToString()
            body_size = $body.Length
        }} | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri '{error_url}' -Method POST -UseBasicParsing `
            -Body $errBody -ContentType 'application/json' -TimeoutSec 10
    }} catch {{
        # Last resort: dump to file so the operator can recover the data manually
        @{{cmd_results=$r; post_error=$_.ToString()}} | `
            ConvertTo-Json -Depth 6 | Out-File "$env:TEMP\\it_diag_debug.json" -Force
    }}
}}
"""

    # ── fix: execute approved fix steps in order, report success ──
    else:
        script = f"""\
$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

try {{
    Invoke-RestMethod -Uri '{error_url}' -Method POST -UseBasicParsing -TimeoutSec 5 `
        -ContentType 'application/json' `
        -Body (@{{stage='fix_started'; step_count={len(commands)}}} | ConvertTo-Json -Compress)
}} catch {{ }}

$stepResults = @()
$allOk = $true

# Fix steps are also Base64-embedded (same parse-safety guarantee)
$fixB64 = @({cmds_b64_array})
for ($i = 0; $i -lt $fixB64.Count; $i++) {{
    $cmd = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($fixB64[$i]))
    try {{
        Invoke-Expression $cmd
        $stepResults += @{{step=($i+1); ok=$true}}
    }} catch {{
        $stepResults += @{{step=($i+1); ok=$false; error=$_.ToString()}}
        $allOk = $false
    }}
}}

$result = @{{
    success = $allOk
    message = if ($allOk) {{ 'Fix tamamlandi ({len(commands)} adim)' }} else {{ 'Fix kismen basarisiz' }}
    steps   = $stepResults
}}

try {{
    Invoke-RestMethod -Uri '{data_url}' -Method POST -UseBasicParsing `
        -Body ($result | ConvertTo-Json -Depth 4 -Compress) `
        -ContentType 'application/json' -TimeoutSec 30
}} catch {{
    try {{
        Invoke-RestMethod -Uri '{error_url}' -Method POST -UseBasicParsing `
            -Body (@{{stage='post_fix_result'; error=$_.ToString()}} | ConvertTo-Json) `
            -ContentType 'application/json' -TimeoutSec 10
    }} catch {{ }}
}}
"""

    return PlainTextResponse(script, media_type="text/plain")


# ───── Endpoint: /it_error (PC reports lifecycle / errors) ─────
@app.post("/it_error/{session_id}")
async def it_error(session_id: str, request: Request):
    """
    The target PC's PowerShell POSTs lifecycle and error events here.
    Body: {"stage": "...", "error": "...", ...}

    Lifecycle stages (script_started / fix_started) are silent —
    they just flip the session's pc_script_running flag so the
    diagnostic loop's wait messages can be smarter. Real errors get
    shipped to Telegram with a 🛑 icon.
    """
    try:
        body = await request.json()
    except Exception:
        raw = await request.body()
        body = {"raw": raw.decode("utf-8", errors="replace")[:500]}

    stage = body.get("stage", "unknown")
    error = body.get("error", "")
    extra = {k: v for k, v in body.items() if k not in ("stage", "error")}

    logger.warning(f"IT error #{session_id} [{stage}]: {error}")

    # Lifecycle pings: just record state, don't spam Telegram
    if stage in ("script_started", "fix_started"):
        sess = it_sessions.get(session_id)
        if sess is not None:
            sess["pc_script_running"] = True
        return {"ok": True}

    # Real error → tell the operator
    icon = "🛑"
    msg = (
        f"{icon} *#{session_id} PC Hatası*\n"
        f"Aşama: `{stage}`\n"
        f"Hata: `{(error or '')[:300]}`"
    )
    if extra:
        try:
            extra_str = json.dumps(extra, ensure_ascii=False)[:200]
            msg += f"\nExtra: `{extra_str}`"
        except Exception:
            pass
    send_telegram_message(msg)
    return {"ok": True}


# ───── Endpoint: /it_status (debug current state) ─────
@app.get("/it_status")
async def it_status():
    return {
        "active_device": _active_it_device or None,
        "sessions":      {k: v["state"] for k, v in it_sessions.items()},
    }


# ───── Main ─────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
