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
from urllib.parse import urlparse, parse_qs


# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip() or "8801207672:AAGWX8HkTwWFf0P4Lt-tz8PGm94DCiULmeA".strip()
WS_URL = "wss://api-fishmcloud.ugame.vn:2083"
CONFIG_FILE = "bot_config.json"


WS_HEADERS = [
    "User-Agent: Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Origin: https://fishmya.ugame.vn",
    "Accept-Language: my-MM,my;q=0.9,en-US;q=0.8,en;q=0.7",
    "X-Requested-With: com.mytel.myid"
]


bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, threaded=True)


# ==========================================
# STATE
# ==========================================
config_data = {"game_access_token": None}
is_running = False
ws_conn = None
ws_lock = threading.Lock()
game_creds = {"username": "", "password": ""}


# ── Message tracking: chat_id -> message_id ──
current_msg = {}


# Game state flags
heartbeat_alive = False
shoot_alive = False
in_game = False
login_handled = False
play_handled = False
fish_list = {}
last_error = ""
fish_shot = 0
bot_start_time = None


# Coin balance tracking
coin_balance = 0
coin_change = 0
last_coin_update = None


# Modified Settings
ULTRA_SPEED_INTERVAL = 0.005 # 200 shots per second
BULLET_COST_PER_SHOT = 500   # To reach 100,000 per second (200 * 500 = 100,000)
MIN_RETURN = 100000
MAX_RETURN = 5000000


# ── Room rejoin tracking ──
in_room = False
room_lost_time = None          # Room ထဲက ထွက်သွားသည့် အချိန်
REJOIN_TIMEOUT = 15            # 15s ကြာရင် အစကနေ reconnect
rejoin_attempt_count = 0
