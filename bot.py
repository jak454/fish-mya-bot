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
last_status_message_id = None

heartbeat_alive = False
shoot_alive = False
use_4x_alive = False
in_game = False
login_handled = False
play_handled = False

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
shoot_interval = 0.05   # STABLE SPEED: 50ms per shot (Increased for stability)
bullet_speed = 1400   # Constant bullet speed from analyzed logic
fish_list = {}        # Store fish data for targeting
fish_lock = threading.Lock() # Lock for thread-safe fish_list access
last_server_time = 0  # Track server timestamp for date sync
last_error_msg = "None" # Track last error for reporting

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
    time.sleep(1)  # Wait for threads to stop
    
    # Reset flags for fresh start
    login_handled = False
    play_handled = False
    in_game = False
    fish_list.clear()
    
    # Start fresh
    is_running = True
    print("[RESTART] Starting fresh connection...")
    
    if config_data["owner_id"]:
        try:
            bot.send_message(
                config_data["owner_id"],
                "✅ *Restart Complete*\n🟢 Connected\n⚡ Shooting again...",
                parse_mode="Markdown"
            )
        except: pass
    
    threading.Thread(target=start_ws, daemon=True).start()

def monitor_health():
    """Error monitoring - error ဖြစ်ရင် auto restart"""
    global error_count, max_errors, last_error_time, monitor_alive
    
    monitor_alive = True
    print("[MONITOR] Health monitor started.")
    
    while monitor_alive:
        time.sleep(5)  # Check every 5 seconds
        
        try:
            if is_running and not shoot_alive:
                error_count += 1
                last_error_time = time.time()
                print(f"[MONITOR] Shoot thread died! Error count: {error_count}/{max_errors}")
                
                if error_count >= max_errors:
                    print("[MONITOR] Too many errors! Force restart...")
                    if config_data["owner_id"]:
                        try:
                            bot.send_message(
                                config_data["owner_id"],
                                f"🔧 *Auto Fix #{error_count}*\n⚠️ Error: {last_error_msg}\n🔄 Auto restart လုပ်ပေးနေပါပြီ...",
                                parse_mode="Markdown"
                            )
                        except: pass
                    error_count = 0
                    force_restart()
                else:
                    print("[MONITOR] Restarting shoot thread...")
                    if ws_conn and ws_conn.connected:
                        threading.Thread(target=auto_shoot_loop, args=(ws_conn,), daemon=True).start()
                        if config_data["owner_id"]:
                            try:
                                bot.send_message(
                                    config_data["owner_id"],
                                    f"🔧 *Auto Fix #{error_count}/{max_errors}*\n⚠️ Error: {last_error_msg}\n🔄 Shoot thread ပြန်စပေးခဲ့တယ်",
                                    parse_mode="Markdown"
                                )
                            except: pass
            
            elif is_running and not heartbeat_alive:
                error_count += 1
                print(f"[MONITOR] Heartbeat thread died! Error count: {error_count}/{max_errors}")
                
                if ws_conn and ws_conn.connected:
                    threading.Thread(target=heartbeat_loop, args=(ws_conn,), daemon=True).start()
                    print("[MONITOR] Heartbeat restarted.")
                    
        except Exception as e:
            print(f"[MONITOR] Error: {e}")

# ==========================================
# TELEGRAM UI
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

def send_or_edit_message(chat_id, text, markup=None):
    global last_status_message_id
    if last_status_message_id:
        try:
            bot.edit_message_text(text, chat_id, last_status_message_id, reply_markup=markup, parse_mode="Markdown")
            return
        except: pass
    msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    last_status_message_id = msg.message_id

def delete_last_message(chat_id):
    global last_status_message_id
    if last_status_message_id:
        try: bot.delete_message(chat_id, last_status_message_id)
        except: pass
        last_status_message_id = None

# ==========================================
# HEARTBEAT (ULTRA OPTIMIZED)
# ==========================================
def heartbeat_loop(ws):
    global heartbeat_alive, is_running
    heartbeat_alive = True
    last_hb = time.time()
    print("[HEARTBEAT] Thread started - ULTRA FAST mode.")

    while is_running and heartbeat_alive:
        if time.time() - last_hb > 2:
            if not send_ws(ws, {"route": "ping", "data": {}, "msgId": 0}):
                break
            last_hb = time.time()
        time.sleep(0.3)

    heartbeat_alive = False
    print("[HEARTBEAT] Thread stopped.")

# ==========================================
# SHOOT LOOP - ULTRA SPEED
# ==========================================
def auto_shoot_loop(ws):
    global shoot_alive, is_running, shoot_interval, fish_list, bullet_speed, last_error_msg
    shoot_alive = True
    print(f"[SHOOT] Thread started - STABLE SPEED interval: {shoot_interval}s")

    while is_running and shoot_alive:
        try:
            target_ids = []
            with fish_lock:
                current_fish_ids = list(fish_list.keys()) 
                if current_fish_ids:
                    target_ids = [current_fish_ids[0]]
            
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
            print(f"[SHOOT] Error: {e}")
            break
        
        time.sleep(shoot_interval)

    shoot_alive = False
    print("[SHOOT] Thread stopped.")

def use_4x_loop(ws):
    global use_4x_alive, is_running
    use_4x_alive = True
    print("[USE_ITEM] Thread started.")

    while is_running and use_4x_alive:
        if not send_ws(ws, {"route": "useItem", "data": {"type": 6}, "msgId": 0}):
            break
        time.sleep(8)

    use_4x_alive = False
    print("[USE_ITEM] Thread stopped.")

# ==========================================
# GAME ACTIONS (ULTRA OPTIMIZED)
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

        print(f"[ACTION] Game actions started! Shoot interval: {shoot_interval}s")

        if config_data["owner_id"]:
            auto_restart_status = 'ON' if config_data.get("auto_restart") else 'OFF'
            send_or_edit_message(
                config_data["owner_id"], 
                f"🌊 *Entered Room*\n⚡ ULTRA SPEED: {shoot_interval}s/shot\n🔄 Auto Restart: {auto_restart_status}", 
                get_main_menu_markup()
            )
    except Exception as e:
        print(f"[ACTION] Error: {e}")

def send_play(ws):
    global play_handled
    if play_handled:
        return
    print("[PLAY] Waiting 0.5 seconds before sending play...")
    time.sleep(0.5)
    if not is_running:
        return
    try:
        payload = {"playerId": game_creds["username"], "password": game_creds["password"], "index": 0}
        print(f"[PLAY] Sending play payload: {payload}")
        send_ws(ws, {
            "route": "play",
            "data": payload,
            "msgId": 2
        })
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
                print(f"[LOGIN] Success! user={game_creds['username'][:15]}, balance={balance:,}")

                if config_data["owner_id"]:
                    send_or_edit_message(
                        config_data["owner_id"],
                        f"✅ *Login OK!*\n👤 {nickname}\n⭐ Level: {level}\n💰 Balance: {balance:,}\n\n⚡ Entering room...",
                        get_main_menu_markup()
                    )

                if not heartbeat_alive:
                    threading.Thread(target=heartbeat_loop, args=(ws,), daemon=True).start()

                threading.Thread(target=send_play, args=(ws,), daemon=True).start()
            else:
                login_handled = True
                print(f"[LOGIN] Failed: {inner}")

        elif msg_id == 2:
            if inner.get("ok"):
                if play_handled:
                    return
                play_handled = True
                print("[PLAY] Room entered successfully!")
                start_game_actions(ws)
            else:
                print(f"[PLAY] Failed: {inner}")
                reconnect()

    except Exception as e:
        print(f"[RECV] Error: {e}")

def ws_recv_loop(ws):
    print("[WS] Receive loop started.")
    while ws.connected:
        try:
            data = ws.recv()
            if not data:
                break
            handle_message(data, ws)
        except Exception as e:
            print(f"[WS RECV ERROR] {e}")
            break
    print("[WS] Receive loop stopped.")
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
    print("[RECONNECT] Waiting 2 seconds...")
    time.sleep(2)

    if is_running:
        threading.Thread(target=start_ws, daemon=True).start()

def start_ws():
    global ws_conn
    token = config_data.get("game_access_token")
    if not token:
        return

    url = f"{WS_URL}?access_token={token}"
    print(f"[WS] Connecting to: {WS_URL}?access_token=***")
    try:
        conn = websocket.create_connection(
            url,
            header=WS_HEADERS,
            sslopt={"cert_reqs": ssl.CERT_NONE},
            timeout=60
        )

        with ws_lock:
            ws_conn = conn

        if config_data["owner_id"]:
            send_or_edit_message(config_data["owner_id"], "🟢 Connected. Logging in...", get_main_menu_markup())

        time.sleep(0.3)
        send_ws(conn, {
            "route": "mytelLogin",
            "data": {"accessToken": token, "language": "my"},
            "msgId": 1
        })
        print("[WS] Sent mytelLogin")

        ws_recv_loop(conn)

    except websocket.WebSocketBadStatusException as e:
        print(f"[WS ERROR] BadStatus: {e}")
        if is_running and config_data["owner_id"]:
            send_or_edit_message(config_data["owner_id"], f"❌ Connection Failed. Retrying...", get_main_menu_markup())
        reconnect()
    except Exception as e:
        print(f"[WS ERROR] {type(e).__name__}: {e}")
        if is_running and config_data["owner_id"]:
            send_or_edit_message(config_data["owner_id"], f"⚠️ WS Error. Auto fix...", get_main_menu_markup())
        reconnect()

def stop_bot_internal():
    global is_running, ws_conn
    is_running = False
    stop_all_threads()
    with ws_lock:
        try:
            if ws_conn:
                ws_conn.close()
        except:
            pass
        ws_conn = None
    print("[BOT] Stopped.")

# ==========================================
# TELEGRAM COMMANDS
# ==========================================
@bot.message_handler(commands=['start'])
def handle_start_cmd(message):
    global config_data, last_status_message_id

    user_id = message.chat.id

    if config_data["owner_id"] is None:
        config_data["owner_id"] = user_id
        save_config()
        bot.send_message(user_id, "👑 You are now the Owner!")
    elif config_data["owner_id"] != user_id:
        bot.send_message(user_id, "⛔ Not authorized.")
        return

    delete_last_message(user_id)
    msg = bot.send_message(user_id, 
        f"🤖 *Fish Bot STABLE SPEED*\n"
        f"⚡ Shoot: {shoot_interval}s\n"
        f"🔫 Bullet Speed: {bullet_speed}\n"
        f"🔧 Error Fix: Auto\n\n"
        f"Select action:", 
        parse_mode="Markdown", reply_markup=get_main_menu_markup())
    last_status_message_id = msg.message_id

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    global is_running, config_data, login_handled, play_handled, in_game

    user_id = call.message.chat.id
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
        
        # Reset all flags for fresh start
        login_handled = False
        play_handled = False
        in_game = False
        
        is_running = True
        threading.Thread(target=start_ws, daemon=True).start()
        bot.answer_callback_query(call.id, "⚡ Starting STABLE SPEED...")

    elif cmd == "cmd_stop":
        if not is_running:
            bot.answer_callback_query(call.id, "⚠️ Already stopped.")
            return
        stop_bot_internal()
        bot.answer_callback_query(call.id, "🛑 Stopping...")
        send_or_edit_message(user_id, "🔴 Bot Stopped.", get_main_menu_markup())

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
        msg = (f"📊 *Bot Status (STABLE)*\n\n"
               f"Status: {status}\n"
               f"Shooting: {shoot}\n"
               f"Speed: {shoot_interval}s/shot\n"
               f"Bullet Speed: {bullet_speed}\n"
               f"Targeting: {len(fish_list)} fish\n"
               f"Server Time: {last_server_time}\n"
               f"Last Error: {last_error_msg}")
        send_or_edit_message(user_id, msg, get_main_menu_markup())
        bot.answer_callback_query(call.id)

def handle_token_input(message):
    global config_data
    user_id = message.chat.id
    raw = message.text.strip()
    token = parse_game_url(raw)

    if not token:
        bot.send_message(user_id, "❌ Invalid. Try again.")
        return

    config_data["game_access_token"] = token
    save_config()

    try:
        bot.delete_message(user_id, message.message_id)
    except:
        pass

    send_or_edit_message(user_id, "✅ Token updated!", get_main_menu_markup())

# ==========================================
# MAIN - AUTO START
# ==========================================
if __name__ == "__main__":
    print("=" * 50)
    print("🤖 Fish Bot STABLE SPEED - AUTO START")
    print(f"⚡ Shoot interval: {shoot_interval}s")
    print(f"🔫 Bullet speed: {bullet_speed}")
    print(f"🔧 Error auto fix: ON")
    print("=" * 50)
    
    # Start health monitor (always running)
    threading.Thread(target=monitor_health, daemon=True).start()
    print("[MAIN] Health monitor started.")
    
    # Auto start if token exists
    if config_data.get("game_access_token"):
        print("[Auto] Token configured. Starting bot automatically...")
        is_running = True
        threading.Thread(target=start_ws, daemon=True).start()
        print("[Auto] Bot started. STABLE SPEED MODE!")
    else:
        print("[Auto] No token configured. Waiting for Telegram command...")
    
    # Start Telegram bot polling
    try:
        print("[SYSTEM] Starting Telegram bot polling...")
        bot.infinity_polling(timeout=10, long_polling_timeout=5)
    except Exception as e:
        print(f"[SYSTEM] Bot polling error: {e}")
