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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "8801207672:AAGWX8HkTwWFf0P4Lt-tz8PGm94DCiULmeA")
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

# ==========================================
# MARKUP BUTTONS
# ==========================================
def get_main_menu():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("▶️ Start Bot", callback_data="cmd_start"),
        InlineKeyboardButton("🛑 Stop Bot", callback_data="cmd_stop"),
    )
    markup.add(
        InlineKeyboardButton("🔑 Set Token", callback_data="cmd_token"),
        InlineKeyboardButton("📊 Status", callback_data="cmd_status"),
    )
    markup.add(
        InlineKeyboardButton("💰 Balance", callback_data="cmd_balance"),
        InlineKeyboardButton("❓ Help", callback_data="cmd_help"),
    )
    return markup

def get_status_buttons():
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("🔄 Refresh", callback_data="cmd_status"),
        InlineKeyboardButton("🛑 Stop", callback_data="cmd_stop"),
    )
    markup.add(
        InlineKeyboardButton("◀️ Back", callback_data="cmd_menu"),
    )
    return markup

def get_start_menu():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🛑 Stop", callback_data="cmd_stop"),
    )
    markup.add(
        InlineKeyboardButton("📊 Status", callback_data="cmd_status"),
        InlineKeyboardButton("◀️ Back", callback_data="cmd_menu"),
    )
    return markup

def get_back_only():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("◀️ Back to Menu", callback_data="cmd_menu"),
    )
    return markup

# ==========================================
# UTILS
# ==========================================
def load_config():
    global config_data
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config_data = json.load(f)
        except:
            pass
    if not config_data.get("game_access_token") and os.getenv("GAME_ACCESS_TOKEN"):
        config_data["game_access_token"] = os.getenv("GAME_ACCESS_TOKEN")

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config_data, f)

def format_uptime(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"

def format_coin(amount):
    if amount >= 1_000_000_000:
        return f"{amount / 1_000_000_000:.1f}B"
    elif amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M"
    elif amount >= 1_000:
        return f"{amount:,.0f}"
    return f"{amount}"

def delete_old(chat_id):
    """Delete old message and remove from tracking"""
    old_msg_id = current_msg.get(chat_id)
    if old_msg_id is not None:
        try:
            bot.delete_message(chat_id, old_msg_id)
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Delete error: {e}")
        finally:
            del current_msg[chat_id]

def show_page(chat_id, text, markup):
    """Delete old message then send new one"""
    delete_old(chat_id)
    try:
        msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        current_msg[chat_id] = msg.message_id
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] Send error: {e}")

def send_untracked(chat_id, text, markup=None):
    """Send a message NOT tracked (e.g. token input request)"""
    try:
        return bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    except:
        return None

def parse_game_url(url_or_token):
    url_or_token = url_or_token.strip()
    if "fishmya" in url_or_token or url_or_token.startswith("http"):
        parsed = urlparse(url_or_token)
        params = parse_qs(parsed.query)
        token = params.get("access_token", [None])[0]
        return token
    return url_or_token if url_or_token.startswith("eyJ") else None

def send_ws(ws, payload_dict):
    if ws and ws.connected:
        try:
            ws.send(msgpack.packb(payload_dict, use_bin_type=True), opcode=websocket.ABNF.OPCODE_BINARY)
            return True
        except:
            return False
    return False

def stop_bot_internal():
    global is_running, ws_conn, heartbeat_alive, shoot_alive, in_game, login_handled, play_handled
    global fish_list, last_error, bot_start_time, in_room, room_lost_time, rejoin_attempt_count
    is_running = False
    heartbeat_alive = False
    shoot_alive = False
    in_game = False
    login_handled = False
    play_handled = False
    fish_list = {}
    in_room = False
    room_lost_time = None
    rejoin_attempt_count = 0
    with ws_lock:
        try:
            if ws_conn:
                ws_conn.close()
        except:
            pass
        ws_conn = None

# ==========================================
# ROOM REJOIN LOGIC
# ==========================================
def rejoin_room(ws):
    """Room ထဲ ပြန်ဝင်ခြင်း"""
    global in_room, room_lost_time, rejoin_attempt_count, in_game, shoot_alive
    if not is_running or not ws:
        return
    try:
        rejoin_attempt_count += 1
        print(f"[{time.strftime('%H:%M:%S')}] Rejoining room (attempt #{rejoin_attempt_count})...")
        in_game = False
        shoot_alive = False
        # Play command ပြန်ပို့ပြီး room ပြန်ဝင်
        send_ws(ws, {
            "route": "play",
            "data": {
                "playerId": game_creds["username"],
                "password": game_creds["password"],
                "index": 0
            },
            "msgId": 2
        })
        room_lost_time = None
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] Rejoin error: {e}")

def room_monitor_loop():
    """Room ထဲ ရှိမရှိ monitor လုပ်သည့် loop"""
    global in_room, room_lost_time, is_running, ws_conn
    print(f"[{time.strftime('%H:%M:%S')}] Room monitor started.")
    while is_running:
        time.sleep(1)
        if not is_running:
            break

        if not in_room and room_lost_time is not None:
            elapsed = time.time() - room_lost_time
            if elapsed >= REJOIN_TIMEOUT:
                print(f"[{time.strftime('%H:%M:%S')}] Room rejoin timeout ({REJOIN_TIMEOUT}s). Full reconnect...")
                with ws_lock:
                    try:
                        if ws_conn:
                            ws_conn.close()
                    except:
                        pass
                    ws_conn = None
                room_lost_time = None
                threading.Thread(target=start_ws, daemon=True).start()
                break
            else:
                with ws_lock:
                    current_ws = ws_conn
                if current_ws:
                    rejoin_room(current_ws)
                    time.sleep(3)
    print(f"[{time.strftime('%H:%M:%S')}] Room monitor stopped.")

# ==========================================
# GAME LOGIC
# ==========================================
def auto_shoot_loop(ws):
    """အလိုအလျောက် ပစ်ခြင်း loop (Modified Logic)"""
    global shoot_alive, is_running, ULTRA_SPEED_INTERVAL, fish_list, fish_shot
    global BULLET_COST_PER_SHOT
    shoot_alive = True
    fish_shot = 0
    
    shot_counter = 0
    hit_in_cycle = 0
    
    while is_running and shoot_alive:
        try:
            target_id = -1
            if fish_list:
                target_id = next(iter(fish_list))

            # ── Shoot with modified bullet cost ──
            # User wants 100,000 per second. With 0.005 interval (200 shots/s), cost is 500.
            send_ws(ws, {
                "route": "shoot",
                "data": {
                    "rad": 0,
                    "type": 1,
                    "target": target_id,
                    "charge": -BULLET_COST_PER_SHOT, # Bullet cost
                    "cd": 0,
                    "maxCharge": 5000,
                    "imageDesc": "gun1",
                    "cash": 0,
                    "time": int(time.time() * 1000),
                    "rapidFire": True,
                    "auto": True
                },
                "msgId": 0
            })
            
            # ── Hit Logic: 2 fish die per 1-3 shots ──
            shot_counter += 1
            if hit_in_cycle < 2:
                if target_id != -1:
                    send_ws(ws, {
                        "route": "clientHitFish",
                        "data": {"btype": 1, "skillType": 0, "fIds": [target_id]},
                        "msgId": 0
                    })
                    fish_shot += 1
                    hit_in_cycle += 1
            
            # Reset cycle every 3 shots
            if shot_counter >= 3:
                shot_counter = 0
                hit_in_cycle = 0
                
        except:
            break
        time.sleep(ULTRA_SPEED_INTERVAL)
    shoot_alive = False

def heartbeat_loop(ws):
    global heartbeat_alive, is_running
    heartbeat_alive = True
    while is_running and heartbeat_alive:
        try:
            send_ws(ws, {"route": "heartbeat", "data": {}, "msgId": 0})
            time.sleep(5)
        except:
            break
    heartbeat_alive = False

def start_game_actions(ws):
    global in_game, in_room
    time.sleep(1)
    if not is_running:
        return
    try:
        in_game = True
        in_room = True
        send_ws(ws, {
            "route": "clientActiveGun",
            "data": {"btype": 1, "gun": "gun1", "skillType": "none", "locationX": 0, "locationY": 0},
            "msgId": 0
        })
        if not heartbeat_alive:
            threading.Thread(target=heartbeat_loop, args=(ws,), daemon=True).start()
        if not shoot_alive:
            threading.Thread(target=auto_shoot_loop, args=(ws,), daemon=True).start()
        send_ws(ws, {"route": "useItem", "data": {"type": 6}, "msgId": 0})
        send_ws(ws, {"route": "getPlayerInfo", "data": {}, "msgId": 100})
    except:
        pass

def handle_message(data, ws):
    global game_creds, login_handled, play_handled, fish_list, in_game
    global coin_balance, coin_change, last_coin_update
    global in_room, room_lost_time
    try:
        decoded = msgpack.unpackb(data, raw=False)
        if not isinstance(decoded, dict):
            return
        route = decoded.get("route", "")
        msg_id = decoded.get("msgId", -1)
        inner = decoded.get("data", decoded)

        # ── Handle coin/balance updates ──
        if route in ["updateCoin", "OnUpdateCoin", "updateBalance", "playerInfo", "getPlayerInfo"]:
            new_coin = inner.get("coin", inner.get("balance", inner.get("coins", coin_balance)))
            if new_coin is not None:
                coin_change = int(new_coin) - int(coin_balance)
                coin_balance = int(new_coin)
                last_coin_update = time.time()

        if route in ["playerInfo", "getPlayerInfo", "OnPlayerInfo"]:
            if inner.get("coin") is not None:
                new_coin = int(inner.get("coin", 0))
                coin_change = new_coin - coin_balance
                coin_balance = new_coin
                last_coin_update = time.time()

        # ── Game update objects ──
        if route in ["OnUpdateObjects", "OnUpdateJackpot", "onSlotJp"]:
            if in_room:
                room_lost_time = None
            if not in_game:
                threading.Thread(target=start_game_actions, args=(ws,), daemon=True).start()

        if route == "OnUpdateObjects":
            in_room = True
            room_lost_time = None
            for obj in inner.get("objects", []):
                fish_list[obj.get("id")] = obj
            for df in inner.get("deadFish", []):
                if df.get("id") in fish_list:
                    del fish_list[df.get("id")]

        # ── Room exit detect ──
        if route in ["OnLeaveRoom", "leaveRoom", "onLeaveRoom", "OnKickOut", "kickOut"]:
            print(f"[{time.strftime('%H:%M:%S')}] Left room (route={route}). Will rejoin...")
            in_room = False
            in_game = False
            fish_list = {}
            if room_lost_time is None:
                room_lost_time = time.time()

        # ── Login response ──
        if msg_id == 1 and inner.get("ok"):
            game_creds["username"] = inner.get("username")
            game_creds["password"] = inner.get("password")
            send_ws(ws, {
                "route": "play",
                "data": {
                    "playerId": game_creds["username"],
                    "password": game_creds["password"],
                    "index": 0
                },
                "msgId": 2
            })

        # ── Play response ──
        if msg_id == 2:
            if inner.get("coin") is not None:
                new_coin = int(inner.get("coin", 0))
                coin_change = new_coin - coin_balance
                coin_balance = new_coin
                last_coin_update = time.time()
            in_room = True
            room_lost_time = None
            print(f"[{time.strftime('%H:%M:%S')}] Entered room successfully.")

    except:
        pass

def start_ws():
    global ws_conn, last_error, bot_start_time, coin_balance, coin_change
    global in_room, room_lost_time, rejoin_attempt_count
    token = config_data.get("game_access_token")
    if not token:
        last_error = "❌ Token မသတ်မှတ်ရသေးပါ"
        return
    try:
        print(f"[{time.strftime('%H:%M:%S')}] Connecting to game server...")
        ws = websocket.create_connection(
            f"{WS_URL}?access_token={token}",
            header=WS_HEADERS,
            sslopt={"cert_reqs": ssl.CERT_NONE},
            timeout=10
        )
        with ws_lock:
            ws_conn = ws
        bot_start_time = time.time()
        in_room = False
        room_lost_time = None
        rejoin_attempt_count = 0
        print(f"[{time.strftime('%H:%M:%S')}] Connected! Sending login...")
        send_ws(ws, {
            "route": "mytelLogin",
            "data": {"accessToken": token, "language": "my"},
            "msgId": 1
        })
        last_error = ""

        threading.Thread(target=room_monitor_loop, daemon=True).start()

        while is_running:
            try:
                ws.settimeout(0.5)
                raw = ws.recv()
                if not raw:
                    break
                handle_message(raw, ws)
            except websocket.WebSocketTimeoutException:
                if in_game and not in_room and room_lost_time is None:
                    room_lost_time = time.time()
                    print(f"[{time.strftime('%H:%M:%S')}] No room data received. Marking room as lost.")
                continue
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] WS Error: {e}")
                break
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] Connection error: {e}")
        last_error = "❌ Connection error"
    finally:
        stop_bot_internal()
        print(f"[{time.strftime('%H:%M:%S')}] Bot stopped.")

# ==========================================
# STATUS PAGE BUILDER
# ==========================================
def build_status_text():
    uptime_text = format_uptime(time.time() - bot_start_time) if bot_start_time and is_running else "—"
    token_status = "✅ Set" if config_data.get("game_access_token") else "❌ Not Set"

    if coin_balance > 0:
        change_icon = "📈" if coin_change >= 0 else "📉"
        change_sign = "+" if coin_change >= 0 else ""
        coin_section = (
            f"💰 Balance: *{format_coin(coin_balance)}*\n"
            f"{change_icon} Change: *{change_sign}{format_coin(abs(coin_change))}*\n"
        )
    else:
        coin_section = f"💰 Balance: *Waiting...*\n"

    bot_run = "🟢 Running" if is_running else "🔴 Stopped"
    heart = "✅ Active" if heartbeat_alive else "❌ Off"
    shoot = "✅ Active" if shoot_alive else "❌ Off"
    game = "✅ In Game" if in_game else "❌ Off"
    room_status = "✅ In Room" if in_room else ("⏳ Rejoining..." if room_lost_time else "❌ Off")

    return (
        f"📊 *Bot Status (Modified)*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔘 Bot: *{bot_run}*\n"
        f"🔑 Token: *{token_status}*\n"
        f"🎯 Fish Shot: *{fish_shot}*\n"
        f"💓 Heartbeat: *{heart}*\n"
        f"🔫 Shooting: *{shoot}*\n"
        f"🎮 Game: *{game}*\n"
        f"🏠 Room: *{room_status}*\n"
        f"🔄 Rejoin Count: *{rejoin_attempt_count}*\n"
        f"⏱️ Uptime: *{uptime_text}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{coin_section}"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ Config: 100k/s Bullet, 2/3 Kill Rate"
    )

def build_balance_text():
    if coin_balance > 0:
        change_icon = "📈" if coin_change >= 0 else "📉"
        change_sign = "+" if coin_change >= 0 else ""
        return (
            f"💰 *Coin Balance*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💎 Balance: *{format_coin(coin_balance)}*\n"
            f"{change_icon} Change: *{change_sign}{format_coin(abs(coin_change))}*\n"
            f"🕐 Last Update: {time.strftime('%H:%M:%S') if last_coin_update else 'N/A'}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
        )
    else:
        return (
            f"💰 *Coin Balance*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ Balance data မရသေးပါ။\n"
            f"Bot စတင်ပြီးရင် auto update လုပ်ပေးပါမယ်။\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
        )

# ==========================================
# CALLBACK HANDLER
# ==========================================
@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    global is_running, bot_start_time
    uid = call.message.chat.id
    data = call.data

    try:
        bot.delete_message(uid, call.message.message_id)
    except:
        pass
    if uid in current_msg:
        del current_msg[uid]

    if data == "cmd_menu":
        show_page(uid,
            "🤖 *Fish Bot v6.1 Pro (Modified)*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Welcome! Bot ကို control လုပ်ပါ။\n"
            "━━━━━━━━━━━━━━━━━━━━━━━",
            get_main_menu())

    elif data == "cmd_help":
        show_page(uid,
            "❓ *Help & Commands*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n"
            "▶️ *Start Bot* — Bot စတင်ပါ\n"
            "🛑 *Stop Bot* — Bot ရပ်ပါ\n"
            "🔑 *Set Token* — Game token ထည့်ပါ\n"
            "📊 *Status* — Bot status ကြည့်ပါ\n"
            "💰 *Balance* — Coin balance ကြည့်ပါ\n"
            "━━━━━━━━━━━━━━━━━━━━━━━",
            get_back_only())

    elif data == "cmd_start":
        token = config_data.get("game_access_token")
        if not token:
            bot.answer_callback_query(call.id, "❌ Token မသတ်မှတ်ရသေးပါ!")
            return

        if is_running:
            bot.answer_callback_query(call.id, "⚠️ Already running!")
            return

        is_running = True
        bot.answer_callback_query(call.id, "🚀 Starting...")
        show_page(uid, "🟡 *Bot Starting...*", get_start_menu())
        threading.Thread(target=start_ws, daemon=True).start()

    elif data == "cmd_stop":
        stop_bot_internal()
        bot.answer_callback_query(call.id, "🔴 Stopped!")
        show_page(uid, "🔴 *Bot Stopped*", get_main_menu())

    elif data == "cmd_token":
        bot.answer_callback_query(call.id, "📝 Send token...")
        show_page(uid, "🔑 *Set Game Token*", get_back_only())
        msg = send_untracked(uid, "👇 *Token ကို အောက်မှာ ပို့ပါ:*")
        if msg:
            bot.register_next_step_handler(msg, update_token)

    elif data == "cmd_status":
        bot.answer_callback_query(call.id)
        show_page(uid, build_status_text(), get_status_buttons())

    elif data == "cmd_balance":
        bot.answer_callback_query(call.id)
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("🔄 Refresh", callback_data="cmd_balance"),
            InlineKeyboardButton("◀️ Back", callback_data="cmd_menu"),
        )
        show_page(uid, build_balance_text(), markup)

# ==========================================
# MESSAGE HANDLERS
# ==========================================
@bot.message_handler(commands=['start'])
def handle_start(message):
    show_page(message.chat.id, "🤖 *Fish Bot v6.1 Pro (Modified)*", get_main_menu())

def update_token(message):
    token = parse_game_url(message.text)
    if token:
        config_data["game_access_token"] = token
        save_config()
        show_page(message.chat.id, "✅ *Token Updated!*", get_main_menu())
    else:
        show_page(message.chat.id, "❌ *Invalid Token!*", get_main_menu())

# ==========================================
# MAIN
# ==========================================
load_config()

if __name__ == "__main__":
    print(f"[{time.strftime('%H:%M:%S')}] Fish Bot v6.1 Pro (Modified) starting...")
    try:
        bot.infinity_polling(allowed_updates=['message', 'callback_query'], timeout=30)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] Polling error: {e}")
        raise
