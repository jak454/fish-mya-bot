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

# Force unbuffered output for GitHub Actions logs
sys.stdout.reconfigure(line_buffering=True)

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

print(f"[DEBUG] Initializing Telegram Bot with token: {TELEGRAM_BOT_TOKEN[:10]}...", flush=True)
try:
    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
    print("[DEBUG] Telegram Bot object created.", flush=True)
except Exception as e:
    print(f"[CRITICAL] Failed to initialize Telegram Bot: {e}", flush=True)
    sys.exit(1)

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
# STATISTICS TRACKING
# ==========================================
stats = {
    "requests_sent": 0,
    "coins_spent": 0,
    "coins_gained": 0,
    "fish_killed": 0,
    "start_balance": 0,
    "current_balance": 0
}
stats_lock = threading.Lock()

def reset_session_stats():
    global stats
    with stats_lock:
        stats["requests_sent"] = 0
        stats["coins_spent"] = 0
        stats["coins_gained"] = 0
        stats["fish_killed"] = 0

def log_stats():
    with stats_lock:
        profit = stats["coins_gained"] - stats["coins_spent"]
        print(f"\n--- SESSION STATISTICS ---", flush=True)
        print(f"Requests Sent: {stats['requests_sent']}", flush=True)
        print(f"Coins Spent: {stats['coins_spent']:,}", flush=True)
        print(f"Coins Gained: {stats['coins_gained']:,}", flush=True)
        print(f"Net Profit: {profit:,}", flush=True)
        print(f"Fish Killed: {stats['fish_killed']}", flush=True)
        print(f"Current Balance: {stats['current_balance']:,}", flush=True)
        print(f"--------------------------\n", flush=True)

# ==========================================
# CYCLE CONFIGURATION
# ==========================================
cycle_duration = 30
cycle_pause = 5

# ==========================================
# ERROR MONITORING
# ==========================================
error_count = 0
max_errors = 1 # Restart immediately on any error
last_error_msg = "None"
restart_lock = threading.Lock()
is_restarting = False

# ==========================================
# OPTIMIZED GAME LOGIC VARIABLES - ULTRA SPEED
# ==========================================
fire_rate_scale = 1.0
shoot_interval = 0.001  # HYPER SPEED: 1ms per shot
bullet_speed = 1400   # Constant bullet speed from analyzed logic
fish_list = {}        # Store fish data for targeting
fish_lock = threading.Lock()
last_server_time = 0  # Track server timestamp for date sync

def load_config():
    global config_data
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config_data = json.load(f)
        except: pass

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config_data, f)

load_config()

# ==========================================
# UTILS
# ==========================================
def parse_game_url(url_or_token):
    url_or_token = url_or_token.strip()
    if "fishmya" in url_or_token or url_or_token.startswith("http"):
        parsed = urlparse(url_or_token)
        params = parse_qs(parsed.query)
        token = params.get("access_token", [None])[0]
        if not token:
            return None
        return token
    else:
        return url_or_token if url_or_token.startswith("eyJ") else None

def send_ws(ws, payload_dict):
    global error_count, last_error_msg, stats
    if ws and ws.connected:
        try:
            ws.send(msgpack.packb(payload_dict, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            with stats_lock:
                stats["requests_sent"] += 1
                # Track coin spending on shoot
                if payload_dict.get("route") == "shoot":
                    # Assume bullet type 1 costs 1 coin, change if needed
                    stats["coins_spent"] += 1
            return True
        except Exception as e:
            if not is_restarting:
                error_count += 1
                last_error_msg = f"Send error: {str(e)}"
            print(f"[SEND] Error: {e}")
    return False

# ==========================================
# TELEGRAM UI & CLEANUP
# ==========================================
def get_main_menu_markup():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("▶️ Start Bot", callback_data="cmd_start"),
        InlineKeyboardButton("🛑 Stop Bot", callback_data="cmd_stop"),
        InlineKeyboardButton("🔑 Set Token", callback_data="cmd_token"),
        InlineKeyboardButton("📊 Status", callback_data="cmd_status"),
        InlineKeyboardButton("🔧 Force Restart", callback_data="cmd_force_restart")
    )
    return markup

def clean_and_send_menu(chat_id, text=None):
    global last_menu_message_id
    with msg_lock:
        for msg_id in sent_messages:
            try: bot.delete_message(chat_id, msg_id)
            except: pass
        sent_messages.clear()
        if last_menu_message_id:
            try: bot.delete_message(chat_id, last_menu_message_id)
            except: pass

    if not text:
        text = "🤖 *Fish Bot HYPER SPEED (2X)*\n⚡ Shoot: 0.001s\n🐟 Targets: 2 fish\n🔫 Bullet Speed: 1400\n\nSelect action:"
    try:
        msg = bot.send_message(chat_id, text, reply_markup=get_main_menu_markup(), parse_mode="Markdown")
        last_menu_message_id = msg.message_id
    except Exception as e:
        print(f"[UI] Error sending menu: {e}")

def track_and_send(chat_id, text, markup=None):
    global sent_messages
    with msg_lock:
        if len(sent_messages) >= 3:
            oldest = sent_messages.pop(0)
            try: bot.delete_message(chat_id, oldest)
            except: pass
        try:
            msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
            sent_messages.append(msg.message_id)
            return msg
        except Exception as e:
            print(f"[UI] Error sending message: {e}")
            return None

def stop_all_threads():
    global heartbeat_alive, shoot_alive, use_4x_alive, login_handled, play_handled, in_game
    heartbeat_alive = False
    shoot_alive = False
    use_4x_alive = False
    login_handled = False
    play_handled = False
    in_game = False
    with fish_lock:
        fish_list.clear()

def force_restart():
    global is_restarting, error_count
    with restart_lock:
        if is_restarting: return
        is_restarting = True
    
    print("[RESTART] Executing restart...")
    error_count = 0
    
    with ws_lock:
        if ws_conn:
            try: ws_conn.close()
            except: pass
    
    stop_all_threads()
    time.sleep(1)
    
    # Re-trigger connection in manager loop
    is_restarting = False

# ==========================================
# CORE MANAGER
# ==========================================
def bot_manager_loop():
    """Unified loop to handle connection, cycles, and health"""
    global is_running, ws_conn, error_count, last_error_msg, is_restarting
    
    print("[MANAGER] Started.")
    while True:
        if not is_running:
            time.sleep(1)
            continue
            
        # 1. Start connection
        print("[MANAGER] Attempting connection...")
        start_ws_connection()
        
        # 2. Run cycle or wait for error
        start_time = time.time()
        while is_running and not is_restarting:
            elapsed = time.time() - start_time
            
            # Check for cycle end
            if elapsed >= cycle_duration:
                print(f"[MANAGER] Cycle finished ({cycle_duration}s). Pausing...")
                log_stats()
                break
                
            # Check for errors
            if error_count >= max_errors:
                print(f"[MANAGER] Max errors reached ({error_count}). Restarting...")
                log_stats()
                if config_data["owner_id"]:
                    track_and_send(config_data["owner_id"], f"🔧 *Auto Restart*\n⚠️ Error: {last_error_msg}\n🔄 Restarting now...")
                break
            
            # Check if shoot thread died unexpectedly
            if in_game and not shoot_alive and ws_conn and ws_conn.connected:
                print("[MANAGER] Shoot thread died. Restarting...")
                log_stats()
                break
                
            time.sleep(1)
            
        # 3. Shutdown current connection
        print("[MANAGER] Closing connection for next phase...")
        with restart_lock: is_restarting = True
        with ws_lock:
            if ws_conn:
                try: ws_conn.close()
                except: pass
            ws_conn = None
        stop_all_threads()
        error_count = 0
        
        # 4. Pause if still running
        if is_running:
            print(f"[MANAGER] Pausing for {cycle_pause}s...")
            time.sleep(cycle_pause)
        
        with restart_lock: is_restarting = False

def start_ws_connection():
    global ws_conn
    token = config_data.get("game_access_token")
    if not token: return

    url = f"{WS_URL}?access_token={token}"
    try:
        conn = websocket.create_connection(
            url, header=WS_HEADERS, sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=30
        )
        with ws_lock: ws_conn = conn
        
        print("[WS] Connected. Sending login...")
        send_ws(conn, {"route": "mytelLogin", "data": {"accessToken": token, "language": "my"}, "msgId": 1})
        
        # Start receiver in background
        threading.Thread(target=ws_recv_loop, args=(conn,), daemon=True).start()
    except Exception as e:
        print(f"[WS] Connection failed: {e}")
        time.sleep(2)

def ws_recv_loop(ws):
    while ws.connected and not is_restarting:
        try:
            data = ws.recv()
            if not data: break
            handle_message(data, ws)
        except: break

# ==========================================
# GAME LOOPS
# ==========================================
def heartbeat_loop(ws):
    global heartbeat_alive
    heartbeat_alive = True
    while is_running and heartbeat_alive and ws.connected and not is_restarting:
        send_ws(ws, {"route": "ping", "data": {}, "msgId": 0})
        time.sleep(2)
    heartbeat_alive = False

def auto_shoot_loop(ws):
    global shoot_alive
    shoot_alive = True
    while is_running and shoot_alive and ws.connected and not is_restarting:
        try:
            target_ids = []
            with fish_lock:
                current_fish_ids = list(fish_list.keys()) 
                if current_fish_ids: target_ids = current_fish_ids[:2]
            
            angle_rad = math.radians(random.randint(-45, 45))
            send_ws(ws, {
                "route": "shoot",
                "data": {"rad": angle_rad, "type": 1, "target": target_ids[0] if target_ids else -1, "rapidFire": True, "auto": True, "bulletSpeed": bullet_speed},
                "msgId": 0
            })
            if target_ids:
                send_ws(ws, {
                    "route": "clientHitFish",
                    "data": {"btype": 1, "skillType": 0, "fIds": target_ids, "bulletSpeed": bullet_speed},
                    "msgId": 0
                })
        except: break
        time.sleep(shoot_interval)
    shoot_alive = False

def use_4x_loop(ws):
    global use_4x_alive
    use_4x_alive = True
    while is_running and use_4x_alive and ws.connected and not is_restarting:
        send_ws(ws, {"route": "useItem", "data": {"type": 6}, "msgId": 0})
        time.sleep(8)
    use_4x_alive = False

# ==========================================
# MESSAGE HANDLER
# ==========================================
def handle_message(data, ws):
    global game_creds, login_handled, play_handled, fish_list, last_server_time, error_count, last_error_msg, stats
    try:
        decoded = msgpack.unpackb(data, raw=False)
        if not isinstance(decoded, dict): return
        route = decoded.get("route", "")
        msg_id = decoded.get("msgId", -1)
        inner = decoded.get("data", decoded)
        if not isinstance(inner, dict): inner = {}
        
        if route == "OnUpdateObjects":
            objects = inner.get("objects", [])
            dead_fish = inner.get("deadFish", [])
            with fish_lock:
                for obj in objects:
                    f_id = obj.get("id")
                    if f_id: fish_list[f_id] = obj
                for df in dead_fish:
                    f_id = df.get("id")
                    if f_id in fish_list: del fish_list[f_id]
        elif route == "OnUpdateObject":
            f_id = inner.get("id")
            if f_id:
                with fish_lock: fish_list[f_id] = inner
        elif route == "OnObjectDie":
            f_id = inner.get("id")
            with fish_lock:
                if f_id in fish_list: del fish_list[f_id]
            # Track kills and gains
            if inner.get("playerId") == game_creds.get("username"):
                with stats_lock:
                    stats["fish_killed"] += 1
                    stats["coins_gained"] += inner.get("cash", 0)

        elif route == "OnUpdateCash":
            if inner.get("playerId") == game_creds.get("username"):
                with stats_lock:
                    stats["current_balance"] = inner.get("cash", 0)

        if msg_id == 1: # Login
            if inner.get("ok"):
                login_handled = True
                game_creds["username"] = inner.get("username", "")
                game_creds["password"] = inner.get("password", "")
                with stats_lock:
                    stats["start_balance"] = inner.get("cash", 0)
                    stats["current_balance"] = inner.get("cash", 0)
                if config_data["owner_id"]:
                    clean_and_send_menu(config_data["owner_id"], f"✅ *Login OK!*\n👤 {inner.get('nickname', 'User')}\n💰 Balance: {inner.get('cash', 0):,}\n\n⚡ Entering room...")
                if not heartbeat_alive: threading.Thread(target=heartbeat_loop, args=(ws,), daemon=True).start()
                time.sleep(0.5)
                send_ws(ws, {"route": "play", "data": {"playerId": game_creds["username"], "password": game_creds["password"], "index": 0}, "msgId": 2})
        elif msg_id == 2: # Play
            if inner.get("ok"):
                play_handled = True
                start_game_actions(ws)
    except Exception as e:
        if not is_restarting:
            error_count += 1
            last_error_msg = f"Handler error: {str(e)}"

def start_game_actions(ws):
    global in_game
    if not is_running or is_restarting: return
    in_game = True
    send_ws(ws, {"route": "clientActiveGun", "data": {"btype": 1, "gun": "gun1", "skillType": "none", "locationX": 0, "locationY": 0, "bulletSpeed": bullet_speed}, "msgId": 0})
    if not shoot_alive: threading.Thread(target=auto_shoot_loop, args=(ws,), daemon=True).start()
    if not use_4x_alive: threading.Thread(target=use_4x_loop, args=(ws,), daemon=True).start()

# ==========================================
# TELEGRAM COMMANDS
# ==========================================
@bot.message_handler(commands=['start'])
def handle_start_cmd(message):
    global config_data
    user_id = message.chat.id
    if config_data["owner_id"] is None:
        config_data["owner_id"] = user_id
        save_config()
        bot.send_message(user_id, "👑 You are now the Owner!")
    elif config_data["owner_id"] != user_id: return
    clean_and_send_menu(user_id)

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    global is_running, config_data
    user_id = call.message.chat.id
    if config_data["owner_id"] != user_id: return
    cmd = call.data
    if cmd == "cmd_start":
        if is_running: bot.answer_callback_query(call.id, "⚠️ Already running.")
        elif not config_data.get("game_access_token"): bot.answer_callback_query(call.id, "❌ Set token first!")
        else:
            is_running = True
            reset_session_stats()
            bot.answer_callback_query(call.id, "⚡ Starting...")
    elif cmd == "cmd_stop":
        is_running = False
        bot.answer_callback_query(call.id, "🛑 Stopping...")
        clean_and_send_menu(user_id, "🔴 Bot Stopped.")
    elif cmd == "cmd_force_restart":
        bot.answer_callback_query(call.id, "🔄 Restarting...")
        force_restart()
    elif cmd == "cmd_token":
        msg = bot.send_message(user_id, "🔑 *Send your Game URL or Access Token:*", parse_mode="Markdown")
        bot.register_next_step_handler(msg, handle_token_input)
        bot.answer_callback_query(call.id)
    elif cmd == "cmd_status":
        status = "🟢 Running" if is_running else "🔴 Stopped"
        shoot = "🔥 Active" if shoot_alive else "💤 Idle"
        with stats_lock:
            profit = stats["coins_gained"] - stats["coins_spent"]
            stat_text = (
                f"📊 *Bot Status*\n"
                f"Status: {status}\n"
                f"Shooting: {shoot}\n"
                f"--------------------------\n"
                f"📡 Requests: {stats['requests_sent']}\n"
                f"💸 Spent: {stats['coins_spent']:,}\n"
                f"💰 Gained: {stats['coins_gained']:,}\n"
                f"📈 Profit: {profit:,}\n"
                f"🐟 Kills: {stats['fish_killed']}\n"
                f"🏦 Balance: {stats['current_balance']:,}\n"
                f"--------------------------\n"
                f"Cycle: {cycle_duration}s run / {cycle_pause}s pause"
            )
        clean_and_send_menu(user_id, stat_text)
        bot.answer_callback_query(call.id)

def handle_token_input(message):
    global config_data
    user_id = message.chat.id
    token = parse_game_url(message.text.strip())
    if not token:
        bot.send_message(user_id, "❌ Invalid. Try again.")
        return
    config_data["game_access_token"] = token
    save_config()
    clean_and_send_menu(user_id, "✅ Token updated!")

if __name__ == "__main__":
    print("[STARTUP] Starting Bot Manager Thread...", flush=True)
    threading.Thread(target=bot_manager_loop, daemon=True).start()
    
    if config_data.get("game_access_token"):
        print("[STARTUP] Found access token, setting is_running = True", flush=True)
        is_running = True
    else:
        print("[STARTUP] No access token found. Bot will wait for command.", flush=True)
        
    print("[STARTUP] Starting Telegram Polling...", flush=True)
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=10)
    except Exception as e:
        print(f"[CRITICAL] Telegram Polling crashed: {e}", flush=True)
        sys.exit(1)
