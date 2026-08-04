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
config_data = {"owner_id": None, "game_access_token": None}
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
# OPTIMIZED GAME LOGIC VARIABLES
# ==========================================
fire_rate_scale = 1.0
shoot_interval = 0.004  # OPTIMIZED: Reduced from 0.01 to 0.004 (2.5x faster shooting)
bullet_speed = 1400   # Constant bullet speed from analyzed logic
fish_list = {}        # Store fish data for targeting

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
# TELEGRAM UI
# ==========================================
def get_main_menu_markup():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("▶️ Start Bot", callback_data="cmd_start"),
        InlineKeyboardButton("🛑 Stop Bot", callback_data="cmd_stop"),
        InlineKeyboardButton("🔑 Set/Update Token", callback_data="cmd_token"),
        InlineKeyboardButton("📊 Status", callback_data="cmd_status")
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
# HEARTBEAT (OPTIMIZED)
# ==========================================
def heartbeat_loop(ws):
    global heartbeat_alive, is_running
    heartbeat_alive = True
    last_hb = time.time()
    print("[HEARTBEAT] Thread started.")

    while is_running and heartbeat_alive:
        if time.time() - last_hb > 3:
            if not send_ws(ws, {"route": "ping", "data": {}, "msgId": 0}):
                break
            last_hb = time.time()
        time.sleep(0.5)  # OPTIMIZED: Reduced from 1 to 0.5 seconds

    heartbeat_alive = False
    print("[HEARTBEAT] Thread stopped.")

# ==========================================
# SHOOT LOOP - OPTIMIZED FOR SPEED
# ==========================================
def auto_shoot_loop(ws):
    global shoot_alive, is_running, shoot_interval, fish_list
    shoot_alive = True
    print(f"[SHOOT] Thread started - interval: {shoot_interval}s (OPTIMIZED)")

    while is_running and shoot_alive:
        try:
            # Target selection: pick the first fish if available, else shoot empty
            target_ids = []
            if fish_list:
                # Simple logic: pick the first fish id
                fish_id = next(iter(fish_list))
                target_ids = [fish_id]
                # print(f"[SHOOT] Firing at fish: {target_ids}")
            
            # The correct route is 'shoot' (via notify/msgId:0)
            # Parameters: rad, type, target, rapidFire, auto
            # rad is in radians. random angle between -45 and 45 degrees
            angle_rad = math.radians(random.randint(-45, 45))
            
            send_ws(ws, {
                "route": "shoot",
                "data": {
                    "rad": angle_rad,
                    "type": 1, 
                    "target": target_ids[0] if target_ids else -1,
                    "rapidFire": True,  # OPTIMIZED: Enabled rapid fire
                    "auto": True  # OPTIMIZED: Enabled auto mode
                },
                "msgId": 0
            })

            # Also send clientHitFish for the targets (OPTIMIZED probability)
            if target_ids and random.random() < 0.025:  # OPTIMIZED: Increased from default to 2.5%
                send_ws(ws, {
                    "route": "clientHitFish",
                    "data": {
                        "btype": 1,
                        "skillType": 0,
                        "fIds": target_ids
                    },
                    "msgId": 0
                })
        except Exception as e:
            print(f"[SHOOT] Error: {e}")
            break
        
        # OPTIMIZED: Faster shoot interval
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
        time.sleep(8)  # OPTIMIZED: Reduced from 10 to 8 seconds

    use_4x_alive = False
    print("[USE_ITEM] Thread stopped.")

# ==========================================
# GAME ACTIONS (OPTIMIZED)
# ==========================================
def start_game_actions(ws):
    global in_game
    time.sleep(0.2)  # OPTIMIZED: Reduced from 1 to 0.2 seconds (5x faster room entry)
    if not is_running:
        return
    try:
        in_game = True

        # Send clientActiveGun - Setting up the gun
        send_ws(ws, {
            "route": "clientActiveGun",
            "data": {"btype": 1, "gun": "gun1", "skillType": "none", "locationX": 0, "locationY": 0},
            "msgId": 0
        })

        # Start heartbeat
        if not heartbeat_alive:
            threading.Thread(target=heartbeat_loop, args=(ws,), daemon=True).start()

        # Start shoot with the correct logic
        if not shoot_alive:
            threading.Thread(target=auto_shoot_loop, args=(ws,), daemon=True).start()

        # Start 4x item
        if not use_4x_alive:
            threading.Thread(target=use_4x_loop, args=(ws,), daemon=True).start()

        print("[ACTION] Game actions started with OPTIMIZED Speed & Shoot Logic!")

        if config_data["owner_id"]:
            send_or_edit_message(config_data["owner_id"], "🌊 *Entered Room*\n🚀 Bot shooting with OPTIMIZED Speed!", get_main_menu_markup())
    except Exception as e:
        print(f"[ACTION] Error: {e}")

def send_play(ws):
    global play_handled
    if play_handled:
        return
    print("[PLAY] Waiting 1 second before sending play...")
    time.sleep(1)  # OPTIMIZED: Reduced from 2 to 1 second
    if not is_running:
        return
    try:
        # Reverting to index 0 as per user feedback
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
    global game_creds, login_handled, play_handled, shoot_interval, fish_list

    try:
        # Check if data is encrypted (XOR) - though xorKey is usually null
        # For now, assume it's raw msgpack
        decoded = msgpack.unpackb(data, raw=False)
        if not isinstance(decoded, dict):
            return

        route = decoded.get("route", "")
        msg_id = decoded.get("msgId", -1)
        inner = decoded.get("data", decoded)
        if not isinstance(inner, dict):
            inner = {}
        
        # Log all routes for debugging
        if route:
            # print(f"[DEBUG] Route received: {route}")
            # If we see game-related broadcasts but haven't started actions, start them
            if not in_game and not shoot_alive and route in ["OnUpdateObjects", "OnUpdateJackpot", "onSlotJp"]:
                print(f"[DEBUG] Game activity detected via {route}. Starting actions.")
                threading.Thread(target=start_game_actions, args=(ws,), daemon=True).start()

        # Update fish list from server broadcast
        if route in ["OnUpdateObjects", "OnUpdateObject", "OnObjectDie"]:
            if route == "OnUpdateObjects":
                objects = inner.get("objects", [])
                dead_fish = inner.get("deadFish", [])
                # print(f"[GAME] Batch Update: {len(objects)} objects, {len(dead_fish)} dead")
                for obj in objects:
                    f_id = obj.get("id")
                    if f_id: fish_list[f_id] = obj
                for df in dead_fish:
                    f_id = df.get("id")
                    if f_id in fish_list: del fish_list[f_id]
            
            elif route == "OnUpdateObject":
                f_id = inner.get("id")
                if f_id:
                    fish_list[f_id] = inner
                    # print(f"[GAME] Single Update: {f_id}")
            
            elif route == "OnObjectDie":
                f_id = inner.get("id")
                if f_id in fish_list:
                    del fish_list[f_id]
                    # print(f"[GAME] Fish Died: {f_id}")

        # LOGIN RESPONSE (msgId == 1)
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
                        f"✅ *Login OK!*\n👤 {nickname}\n⭐ Level: {level}\n💰 Balance: {balance:,}\n\n🔑 Entering room...",
                        get_main_menu_markup()
                    )

                # Start heartbeat immediately
                if not heartbeat_alive:
                    threading.Thread(target=heartbeat_loop, args=(ws,), daemon=True).start()

                # Send play in thread
                threading.Thread(target=send_play, args=(ws,), daemon=True).start()
            else:
                login_handled = True
                print(f"[LOGIN] Failed: {inner}")
                if config_data["owner_id"]:
                    send_or_edit_message(config_data["owner_id"], f"❌ Login Failed: {inner.get('err', 'Unknown')}", get_main_menu_markup())
                stop_bot_internal()
            return

        # PLAY RESPONSE (msgId == 2)
        if msg_id == 2:
            if play_handled:
                return
            play_handled = True

            if not inner.get("err"):
                print("[PLAY] Room entered!")
                # Update shoot interval based on player fire rate scale if available
                # Logic: 0.25 * scale
                players = inner.get("players", [])
                for p in players:
                    if p.get("playerId") == game_creds["username"]:
                        scale = p.get("fireRateScale", 1.0)
                        # shoot_interval = 0.25 * scale
                        # print(f"[LOGIC] Shoot Interval set to {shoot_interval}s")
                        pass
                        break
                
                threading.Thread(target=start_game_actions, args=(ws,), daemon=True).start()
            else:
                print(f"[PLAY] Failed: {inner}")
                if config_data["owner_id"]:
                    send_or_edit_message(config_data["owner_id"], f"❌ Room entry failed: {inner.get('err', 'Unknown')}", get_main_menu_markup())
                stop_bot_internal()
            return

    except Exception as e:
        print(f"[DECODE] Error: {e}")

# ==========================================
# WEBSOCKET RUN LOOP (OPTIMIZED)
# ==========================================
def ws_recv_loop(ws):
    while is_running:
        try:
            ws.settimeout(0.1)  # OPTIMIZED: Reduced from 0.5 to 0.1 seconds
            raw = ws.recv()
            if not raw:
                break
            handle_message(raw, ws)
        except websocket.WebSocketTimeoutException:
            continue
        except websocket.WebSocketConnectionClosedException as e:
            code = e.args[0] if len(e.args) > 0 else "unknown"
            reason = e.args[1] if len(e.args) > 1 else "no reason"
            print(f"[WS CLOSED] code={code}, reason={reason}")
            if is_running and config_data["owner_id"]:
                send_or_edit_message(config_data["owner_id"], f"🔴 Disconnected (code={code}). Reconnecting...", get_main_menu_markup())
            reconnect()
            return
        except Exception as e:
            print(f"[WS ERROR] {type(e).__name__}: {e}")
            if is_running and config_data["owner_id"]:
                send_or_edit_message(config_data["owner_id"], f"⚠️ WS Error: {e}", get_main_menu_markup())
            reconnect()
            return

# ==========================================
# SYSTEM
# ==========================================
def stop_all_threads():
    global heartbeat_alive, shoot_alive, use_4x_alive, in_game, login_handled, play_handled, fish_list
    heartbeat_alive = False
    shoot_alive = False
    use_4x_alive = False
    in_game = False
    login_handled = False
    play_handled = False
    fish_list = {}
    print("[THREADS] All threads stopped.")

def reconnect():
    global is_running, ws_conn
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
    print("[RECONNECT] Waiting 3 seconds...")  # OPTIMIZED: Reduced from 5 to 3 seconds
    time.sleep(3)

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
        # Use a more robust SSL configuration
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

        time.sleep(0.5)  # OPTIMIZED: Reduced from 1 to 0.5 seconds
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
            send_or_edit_message(config_data["owner_id"], f"❌ Connection Failed: {e}", get_main_menu_markup())
        stop_bot_internal()
    except Exception as e:
        error_msg = str(e).replace("_", "\\_").replace("*", "\\*")
        print(f"[WS ERROR] {type(e).__name__}: {e}")
        if is_running and config_data["owner_id"]:
            send_or_edit_message(config_data["owner_id"], f"⚠️ WS Error: {error_msg}", get_main_menu_markup())
        # Instead of stopping, try to reconnect
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
    msg = bot.send_message(user_id, "🤖 *Professional Fish Bot (OPTIMIZED)*\n\nSelect action:", parse_mode="Markdown", reply_markup=get_main_menu_markup())
    last_status_message_id = msg.message_id

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    global is_running, config_data

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
        is_running = True
        threading.Thread(target=start_ws, daemon=True).start()
        bot.answer_callback_query(call.id, "🚀 Starting...")

    elif cmd == "cmd_stop":
        if not is_running:
            bot.answer_callback_query(call.id, "⚠️ Already stopped.")
            return
        stop_bot_internal()
        bot.answer_callback_query(call.id, "🛑 Stopping...")
        send_or_edit_message(user_id, "🔴 Bot Stopped.", get_main_menu_markup())

    elif cmd == "cmd_token":
        msg = bot.send_message(user_id, "🔑 *Send your Game URL or Access Token:*", parse_mode="Markdown")
        bot.register_next_step_handler(msg, handle_token_input)
        bot.answer_callback_query(call.id)

    elif cmd == "cmd_status":
        status = "🟢 Running" if is_running else "🔴 Stopped"
        shoot = "🔥 Active" if shoot_alive else "💤 Idle"
        bal = "Updating..."
        msg = f"📊 *Bot Status*\n\nStatus: {status}\nShooting: {shoot}\nTargeting: {len(fish_list)} fish"
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
# AUTO-RUN
# ==========================================
if config_data.get("game_access_token"):
    print("[Auto] Token configured. Starting...")
    is_running = True
    threading.Thread(target=start_ws, daemon=True).start()

if __name__ == "__main__":
    print("Bot polling started...")
    try:
        bot.infinity_polling()
    except Exception as e:
        print(f"Bot polling error: {e}")
