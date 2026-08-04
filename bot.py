import websocket
import msgpack
import json
import time
import threading
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import ssl
import random
import os
import math
import sys
from urllib.parse import urlparse, parse_qs


# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = "8801207672:AAGWX8HkTwWFf0P4Lt-tz8PGm94DCiULmeA"
WS_URL = "wss://api-fishmcloud.ugame.vn:2083"
CONFIG_FILE = "bot_config.json"


WS_HEADERS = [
    "User-Agent: Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Origin: https://fishmya.ugame.vn",
    "Accept-Language: my-MM,my;q=0.9,en-US;q=0.8,en;q=0.7",
    "X-Requested-With: com.mytel.myid"
]


bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)


# ==========================================
# STATE
# ==========================================
config_data = {"owner_id": None, "game_access_token": None, "auto_restart": True}
is_running = False
ws_conn = None
ws_lock = threading.Lock()
game_creds = {"username": "", "password": ""}


# Message cleanup state
sent_messages = []
msg_lock = threading.Lock()
last_menu_message_id = None


heartbeat_alive = False
shoot_alive = False
use_4x_alive = False
in_game = False
login_handled = False
play_handled = False


# ==========================================
# CYCLE CONFIGURATION
# ==========================================
cycle_alive = False
cycle_duration = 30
cycle_pause = 5


# ==========================================
# ERROR MONITORING
# ==========================================
monitor_alive = False
error_count = 0
max_errors = 1  # Restart immediately on any error
last_error_time = 0


# ==========================================
# OPTIMIZED GAME LOGIC VARIABLES - ULTRA SPEED
# ==========================================
