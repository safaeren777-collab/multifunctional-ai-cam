"""
Smart OS VPS Backend — Configuration Template

1) Copy this file to config.py:
       cp config.example.py config.py

2) Fill in your own credentials.

3) config.py is gitignored — do NOT commit your real keys.
"""

# Telegram bot token from @BotFather
TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"

# Your Telegram chat ID (numeric).
# Find it via @userinfobot or by sending /start to your own bot
# and inspecting the update payload.
CHAT_ID = "YOUR_TELEGRAM_CHAT_ID_HERE"

# Google Gemini API key (https://aistudio.google.com/app/apikey)
GEMINI_API_KEY = "YOUR_GEMINI_API_KEY_HERE"

# Vision model — used for Guard / AI Assist / Voice command interpretation.
# Cheap and fast: gemini-2.5-flash
GEMINI_MODEL_VISION = "gemini-2.5-flash"

# Public reachable address of THIS server.
# Used by IT-Support mode to tell the target PC where to POST data back.
# Must be an IP/hostname the target PC can reach (i.e. not 127.0.0.1).
VPS_HOST = "your.vps.ip.or.hostname"
VPS_PORT = 8003
