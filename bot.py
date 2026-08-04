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
max_errors = 5  # Max errors before forced restart
last_error_time = 0

# ==========================================
# OPTIMIZED GAME LOGIC VARIABLES - ULTRA SPEED
# ==========================================
fire_rate_scale = 1.0
shoot_interval = 0.001  # HYPER SPEED: 1ms per shot
bullet_speed = 1400   # Constant bullet speed from analyzed logic
fish_list = {}        # Store fish data for targeting
fish_lock = threading.Lock()
last_server_time = 0  # Track server timestamp for date sync
last_error_msg = "None"

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
    if ws and ws.connected:
        try:
            ws.send(msgpack.packb(payload_dict, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            return True
        except Exception as e:
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
    
    # 1. Cleanup all recorded messages
    with msg_lock:
        for msg_id in sent_messages:
            try:
                bot.delete_message(chat_id, msg_id)
            except:
                pass
        sent_messages.clear()
        
        # Also delete old menu if exists
        if last_menu_message_id:
            try:
                bot.delete_message(chat_id, last_menu_message_id)
            except:
                pass

    # 2. Send new menu
    if not text:
        text = "🤖 *Fish Bot HYPER SPEED (2X)*\n⚡ Shoot: 0.001s\n🐟 Targets: 2 fish\n🔫 Bullet Speed: 1400\n\nSelect action:"
        
    try:
        msg = bot.send_message(chat_id, text, reply_markup=get_main_menu_markup(), parse_mode="Markdown")
        last_menu_message_id = msg.message_id
    except Exception as e:
        print(f"[UI] Error sending menu: {e}")

def track_and_send(chat_id, text, markup=None):
    """Send message and track for cleanup (keep max 3)"""
    global sent_messages
    
    with msg_lock:
        # If we already have 3 messages, delete the oldest one
        if len(sent_messages) >= 3:
            oldest = sent_messages.pop(0)
            try:
                bot.delete_message(chat_id, oldest)
            except:
                pass
        
        try:
            msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
            sent_messages.append(msg.message_id)
            return msg
        except Exception as e:
            print(f"[UI] Error sending message: {e}")
            return None

def force_restart():
    """Bot ကို fully restart လုပ်ခြင်း - အစအဆုံး"""
    global is_running, ws_conn, login_handled, play_handled, in_game
    
    print("[RESTART] Force restart initiated...")
    
    # Stop everything
    is_running = False
    
    with ws_lock:
        try:
            if ws_conn:
                ws_conn.close()
        except:
            pass
        ws_conn = None
    
    stop_all_threads()
    time.sleep(1)
    
    # Reset flags
    login_handled = False
    play_handled = False
    in_game = False
    with fish_lock:
        fish_list.clear()
    
    # Start fresh
    is_running = True
    
    if config_data["owner_id"]:
        clean_and_send_menu(config_data["owner_id"], "✅ *Restart Complete*\n🟢 Connected\n⚡ Shooting again...")
    
    threading.Thread(target=start_ws, daemon=True).start()

def monitor_health():
    """Error monitoring - error ဖြစ်ရင် auto restart"""
    global error_count, max_errors, last_error_time, monitor_alive
    
    monitor_alive = True
    print("[MONITOR] Health monitor started.")
    
    while monitor_alive:
        time.sleep(5)
        
        try:
            if is_running and not shoot_alive:
                # If we are in the middle of a cycle pause, don't treat as error
                if not ws_conn:
                    continue
                    
                error_count += 1
                last_error_time = time.time()
                
                if error_count >= max_errors:
                    if config_data["owner_id"]:
                        track_and_send(config_data["owner_id"], f"🔧 *Auto Fix #{error_count}*\n⚠️ Error: {last_error_msg}\n🔄 Auto restart...")
                    error_count = 0
                    force_restart()
                else:
                    if ws_conn and ws_conn.connected:
                        threading.Thread(target=auto_shoot_loop, args=(ws_conn,), daemon=True).start()
                        if config_data["owner_id"]:
                            track_and_send(config_data["owner_id"], f"🔧 *Auto Fix #{error_count}/{max_errors}*\n⚠️ Thread restarted")
            
            elif is_running and not heartbeat_alive:
                if ws_conn and ws_conn.connected:
                    threading.Thread(target=heartbeat_loop, args=(ws_conn,), daemon=True).start()
                    
        except Exception as e:
            print(f"[MONITOR] Error: {e}")

def cycle_manager_loop():
    """Manage 30s run / 5s pause cycles"""
    global cycle_alive, is_running
    cycle_alive = True
    print("[CYCLE] Manager started.")
    
    while cycle_alive:
        if not is_running:
            time.sleep(1)
            continue
            
        # Run for 30 seconds
        time.sleep(cycle_duration)
        
        if not cycle_alive or not is_running:
            continue
            
        print(f"[CYCLE] Cycle finished. Pausing for {cycle_pause}s...")
        
        # Stop current session
        stop_all_threads()
        with ws_lock:
            try:
                if ws_conn:
                    ws_conn.close()
            except: pass
        
        # Pause for 5 seconds
        time.sleep(cycle_pause)
        
        if not cycle_alive or not is_running:
            continue
            
        print("[CYCLE] Restarting next cycle...")
        # Restart
        threading.Thread(target=start_ws, daemon=True).start()

# ==========================================
# HEARTBEAT
# ==========================================
def heartbeat_loop(ws):
    global heartbeat_alive, is_running
    heartbeat_alive = True
    last_hb = time.time()

    while is_running and heartbeat_alive:
        if time.time() - last_hb > 2:
            if not send_ws(ws, {"route": "ping", "data": {}, "msgId": 0}):
                break
            last_hb = time.time()
        time.sleep(0.3)

    heartbeat_alive = False

# ==========================================
# SHOOT LOOP
# ==========================================
def auto_shoot_loop(ws):
    global shoot_alive, is_running, shoot_interval, fish_list, bullet_speed, last_error_msg
    shoot_alive = True

    while is_running and shoot_alive:
        try:
            target_ids = []
            with fish_lock:
                current_fish_ids = list(fish_list.keys()) 
                if current_fish_ids:
                    target_ids = current_fish_ids[:2]  # Double target: 2 fish at once
            
            angle_rad = math.radians(random.randint(-45, 45))
            
            send_ws(ws, {
                "route": "shoot",
                "data": {
                    "rad": angle_rad,
                    "type": 1, 
                    "target": target_ids[0] if target_ids else -1,
                    "rapidFire": True,
                    "auto": True,
                    "bulletSpeed": bullet_speed
                },
                "msgId": 0
            })

            if target_ids:
                send_ws(ws, {
                    "route": "clientHitFish",
                    "data": {
                        "btype": 1,
                        "skillType": 0,
                        "fIds": target_ids,
                        "bulletSpeed": bullet_speed
                    },
                    "msgId": 0
                })
        except Exception as e:
            last_error_msg = str(e)
            break
        
        time.sleep(shoot_interval)

    shoot_alive = False

def use_4x_loop(ws):
    global use_4x_alive, is_running
    use_4x_alive = True
    while is_running and use_4x_alive:
        if not send_ws(ws, {"route": "useItem", "data": {"type": 6}, "msgId": 0}):
            break
        time.sleep(8)
    use_4x_alive = False

# ==========================================
# GAME ACTIONS
# ==========================================
def start_game_actions(ws):
    global in_game
    time.sleep(0.15)
    if not is_running:
        return
    try:
        in_game = True
        send_ws(ws, {
            "route": "clientActiveGun",
            "data": {"btype": 1, "gun": "gun1", "skillType": "none", "locationX": 0, "locationY": 0, "bulletSpeed": bullet_speed},
            "msgId": 0
        })

        if not heartbeat_alive:
            threading.Thread(target=heartbeat_loop, args=(ws,), daemon=True).start()
        if not shoot_alive:
            threading.Thread(target=auto_shoot_loop, args=(ws,), daemon=True).start()
        if not use_4x_alive:
            threading.Thread(target=use_4x_loop, args=(ws,), daemon=True).start()

        if config_data["owner_id"]:
            auto_restart_status = 'ON' if config_data.get("auto_restart") else 'OFF'
            clean_and_send_menu(config_data["owner_id"], f"🌊 *Entered Room*\n⚡ HYPER SPEED: {shoot_interval}s/shot\n🐟 Targets: 2 fish\n🔄 Auto Restart: {auto_restart_status}")
    except Exception as e:
        print(f"[ACTION] Error: {e}")

def send_play(ws):
    global play_handled
    if play_handled:
        return
    time.sleep(0.5)
    if not is_running:
        return
    try:
        payload = {"playerId": game_creds["username"], "password": game_creds["password"], "index": 0}
        send_ws(ws, {"route": "play", "data": payload, "msgId": 2})
    except Exception as e:
        print(f"[PLAY] Error: {e}")

# ==========================================
# MESSAGE HANDLER
# ==========================================
def handle_message(data, ws):
    global game_creds, login_handled, play_handled, shoot_interval, fish_list, last_server_time

    try:
        decoded = msgpack.unpackb(data, raw=False)
        if not isinstance(decoded, dict):
            return

        route = decoded.get("route", "")
        msg_id = decoded.get("msgId", -1)
        inner = decoded.get("data", decoded)
        if not isinstance(inner, dict):
            inner = {}
        
        server_time = inner.get("serverTime") or inner.get("timestamp") or inner.get("time")
        if server_time:
            last_server_time = server_time

        if route in ["OnUpdateObjects", "OnUpdateObject", "OnObjectDie"]:
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
                    with fish_lock:
                        fish_list[f_id] = inner
            
            elif route == "OnObjectDie":
                f_id = inner.get("id")
                with fish_lock:
                    if f_id in fish_list:
                        del fish_list[f_id]

        if msg_id == 1:
            if inner.get("ok"):
                if login_handled:
                    return
                login_handled = True
                game_creds["username"] = inner.get("username", "")
                game_creds["password"] = inner.get("password", "")
                balance = inner.get("cash", 0)
                nickname = inner.get("nickname", "User")
                level = inner.get("level", 0)

                if config_data["owner_id"]:
                    clean_and_send_menu(config_data["owner_id"], f"✅ *Login OK!*\n👤 {nickname}\n⭐ Level: {level}\n💰 Balance: {balance:,}\n\n⚡ Entering room...")

                if not heartbeat_alive:
                    threading.Thread(target=heartbeat_loop, args=(ws,), daemon=True).start()
                threading.Thread(target=send_play, args=(ws,), daemon=True).start()
            else:
                login_handled = True

        elif msg_id == 2:
            if inner.get("ok"):
                if play_handled:
                    return
                play_handled = True
                start_game_actions(ws)
            else:
                reconnect()

    except Exception as e:
        print(f"[MSG] Error: {e}")

def ws_recv_loop(ws):
    while ws.connected:
        try:
            if not is_running: break
            data = ws.recv()
            if not data: break
            handle_message(data, ws)
        except:
            break
    reconnect()

def stop_all_threads():
    global heartbeat_alive, shoot_alive, use_4x_alive
    heartbeat_alive = False
    shoot_alive = False
    use_4x_alive = False

def reconnect():
    global ws_conn, is_running
    with ws_lock:
        if not is_running:
            return
        try:
            if ws_conn:
                ws_conn.close()
        except:
            pass
        ws_conn = None

    stop_all_threads()

def start_ws():
    global ws_conn, is_running
    is_running = True
    try:
        ws_conn = websocket.WebSocket(sslopt={"cert_reqs": ssl.CERT_NONE})
        ws_conn.connect(WS_URL, header=WS_HEADERS)
        
        # Initial login
        if config_data["game_access_token"]:
            send_ws(ws_conn, {"route": "login", "data": {"accessToken": config_data["game_access_token"], "version": "1.0.0"}, "msgId": 1})
        
        ws_recv_loop(ws_conn)
    except Exception as e:
        print(f"[WS] Connection error: {e}")
        reconnect()

# ==========================================
# BOT HANDLERS
# ==========================================
@bot.message_handler(commands=['start'])
def start(message):
    global config_data
    config_data["owner_id"] = message.chat.id
    save_config()
    clean_and_send_menu(message.chat.id, "👋 *Welcome to Fish Bot ULTRA*\n\nPlease set your game access token first.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('cmd_'))
def handle_callback(call):
    global is_running, config_data, cycle_alive, monitor_alive
    
    user_id = call.from_user.id
    if config_data["owner_id"] != user_id:
        bot.answer_callback_query(call.id, "⛔ Not authorized.")
        return

    cmd = call.data
    if cmd == "cmd_start":
        if is_running:
            bot.answer_callback_query(call.id, "⚠️ Already running.")
            return
        if not config_data.get("game_access_token"):
            bot.answer_callback_query(call.id, "❌ Set token first!")
            return
        login_handled = False
        play_handled = False
        in_game = False
        is_running = True
        threading.Thread(target=start_ws, daemon=True).start()
        if not monitor_alive:
            threading.Thread(target=monitor_health, daemon=True).start()
        if not cycle_alive:
            threading.Thread(target=cycle_manager_loop, daemon=True).start()
        bot.answer_callback_query(call.id, "⚡ Starting (30s Cycle)... ")

    elif cmd == "cmd_stop":
        if not is_running:
            bot.answer_callback_query(call.id, "⚠️ Not running.")
            return
        is_running = False
        reconnect()
        bot.answer_callback_query(call.id, "🛑 Bot Stopped")
        clean_and_send_menu(call.message.chat.id, "🛑 *Bot Stopped Successfully*")

    elif cmd == "cmd_token":
        msg = bot.send_message(call.message.chat.id, "🔑 *Please send your Game URL or Access Token:*", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_token)
        
    elif cmd == "cmd_status":
        status = "🟢 Running" if is_running else "🔴 Stopped"
        ws_status = "✅ Connected" if ws_conn and ws_conn.connected else "❌ Disconnected"
        targets = len(fish_list)
        bot.answer_callback_query(call.id, f"Status: {status}")
        track_and_send(call.message.chat.id, f"📊 *Bot Status*\n\nState: {status}\nWS: {ws_status}\n🐟 Active Fish: {targets}\n⚡ Speed: {shoot_interval}s\n🔄 Auto Restart: {config_data.get('auto_restart')}")

    elif cmd == "cmd_force_restart":
        bot.answer_callback_query(call.id, "🔧 Force Restarting...")
        force_restart()

def process_token(message):
    global config_data
    token = parse_game_url(message.text)
    if token:
        config_data["game_access_token"] = token
        save_config()
        bot.send_message(message.chat.id, "✅ *Token Updated!*\nYou can now Start the bot.", parse_mode="Markdown")
        clean_and_send_menu(message.chat.id)
    else:
        bot.send_message(message.chat.id, "❌ *Invalid Token or URL!*\nPlease try again.", parse_mode="Markdown")

if __name__ == "__main__":
    print("Bot is running...")
    bot.polling(none_stop=True)
