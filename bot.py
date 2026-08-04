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
config_data = {"game_access_token": None, "admin_chat_id": None}
is_running = False
ws_conn = None
ws_lock = threading.Lock()
game_creds = {"username": "", "password": ""}

current_msg = {}
heartbeat_alive = False
shoot_alive = False
in_game = False
login_handled = False
play_handled = False
fish_list = {}
last_error = ""
fish_shot = 0
bot_start_time = None
coin_balance = 0
coin_change = 0
last_coin_update = None

ULTRA_SPEED_INTERVAL = 0.005 
BULLET_COST_PER_SHOT = 500   
MIN_RETURN = 100000
MAX_RETURN = 5000000

in_room = False
room_lost_time = None          
REJOIN_TIMEOUT = 15            
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
        except: pass
    if not config_data.get("game_access_token") and os.getenv("GAME_ACCESS_TOKEN"):
        config_data["game_access_token"] = os.getenv("GAME_ACCESS_TOKEN")

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config_data, f)

def format_uptime(seconds):
    if seconds < 60: return f"{int(seconds)}s"
    elif seconds < 3600: return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"

def format_coin(amount):
    if amount >= 1_000_000_000: return f"{amount / 1_000_000_000:.1f}B"
    elif amount >= 1_000_000: return f"{amount / 1_000_000:.1f}M"
    elif amount >= 1_000: return f"{amount:,.0f}"
    return f"{amount}"

def delete_old(chat_id):
    old_msg_id = current_msg.get(chat_id)
    if old_msg_id is not None:
        try: bot.delete_message(chat_id, old_msg_id)
        except: pass
        finally: del current_msg[chat_id]

def show_page(chat_id, text, markup):
    delete_old(chat_id)
    try:
        msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        current_msg[chat_id] = msg.message_id
    except: pass

def send_untracked(chat_id, text, markup=None):
    try: return bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    except: return None

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
        except: return False
    return False

def stop_bot_internal():
    global is_running, ws_conn, heartbeat_alive, shoot_alive, in_game, login_handled, play_handled
    global fish_list, in_room, room_lost_time, rejoin_attempt_count
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
            if ws_conn: ws_conn.close()
        except: pass
        ws_conn = None

# ==========================================
# REJOIN & MONITOR
# ==========================================
def rejoin_room(ws):
    global in_room, room_lost_time, rejoin_attempt_count, in_game, shoot_alive
    if not is_running or not ws: return
    try:
        rejoin_attempt_count += 1
        in_game = False
        shoot_alive = False
        send_ws(ws, {
            "route": "play",
            "data": {"playerId": game_creds["username"], "password": game_creds["password"], "index": 0},
            "msgId": 2
        })
        room_lost_time = None
    except: pass

def room_monitor_loop():
    global in_room, room_lost_time, is_running, ws_conn
    while is_running:
        time.sleep(1)
        if not is_running: break
        if not in_room and room_lost_time is not None:
            elapsed = time.time() - room_lost_time
            if elapsed >= REJOIN_TIMEOUT:
                with ws_lock:
                    try:
                        if ws_conn: ws_conn.close()
                    except: pass
                    ws_conn = None
                room_lost_time = None
                threading.Thread(target=start_ws, daemon=True).start()
                break
            else:
                with ws_lock: current_ws = ws_conn
                if current_ws:
                    rejoin_room(current_ws)
                    time.sleep(3)

# ==========================================
# GAME LOGIC
# ==========================================
def auto_shoot_loop(ws):
    """အလိုအလျောက် ပစ်ခြင်း loop (20s ON / 5s OFF with Full Restart)"""
    global shoot_alive, is_running, ULTRA_SPEED_INTERVAL, fish_list, fish_shot
    global BULLET_COST_PER_SHOT, coin_balance, config_data
    shoot_alive = True
    fish_shot = 0
    cycle_start = time.time()
    cycle_shots = 0
    cycle_kills = 0
    cycle_start_balance = coin_balance
    
    while is_running and shoot_alive:
        try:
            now = time.time()
            if now - cycle_start >= 20:
                print(f"[{time.strftime('%H:%M:%S')}] 20s cycle complete. Reporting and Restarting...")
                end_balance = coin_balance
                balance_diff = end_balance - cycle_start_balance
                diff_sign = '+' if balance_diff >= 0 else ''
                report_text = (
                    f'📊 *Cycle Report (20s)*\n'
                    f'━━━━━━━━━━━━━━━━━━━━━━━\n'
                    f'🎯 Shots Sent: *{cycle_shots}*\n'
                    f'🐟 Fish Killed: *{cycle_kills}*\n'
                    f'💰 Balance Change: *{diff_sign}{format_coin(balance_diff)}*\n'
                    f'💎 Current: *{format_coin(end_balance)}*\n'
                    f'━━━━━━━━━━━━━━━━━━━━━━━\n'
                    f'⏳ Restarting in 5 seconds...'
                )
                admin_id = config_data.get('admin_chat_id')
                if admin_id:
                    try: bot.send_message(admin_id, report_text, parse_mode='Markdown')
                    except: pass
                threading.Thread(target=full_restart_trigger, daemon=True).start()
                break

            target_id = -1
            if fish_list: target_id = next(iter(fish_list))
            send_ws(ws, {
                'route': 'shoot',
                'data': {
                    'rad': 0, 'type': 1, 'target': target_id,
                    'charge': -BULLET_COST_PER_SHOT, 'cd': 0,
                    'maxCharge': 5000, 'imageDesc': 'gun1', 'cash': 0,
                    'time': int(time.time() * 1000), 'rapidFire': True, 'auto': True
                },
                'msgId': 0
            })
            cycle_shots += 1
            if target_id != -1 and random.random() < 0.0175:
                send_ws(ws, {
                    'route': 'clientHitFish',
                    'data': {'btype': 1, 'skillType': 0, 'fIds': [target_id]},
                    'msgId': 0
                })
                fish_shot += 1
                cycle_kills += 1
            time.sleep(ULTRA_SPEED_INTERVAL)
        except: break
    shoot_alive = False

def full_restart_trigger():
    global is_running
    print(f"[{time.strftime('%H:%M:%S')}] Initiating full restart cycle...")
    stop_bot_internal()
    time.sleep(5)
    is_running = True
    print(f"[{time.strftime('%H:%M:%S')}] Restarting bot...")
    threading.Thread(target=start_ws, daemon=True).start()

def heartbeat_loop(ws):
    global heartbeat_alive, is_running
    heartbeat_alive = True
    while is_running and heartbeat_alive:
        try:
            send_ws(ws, {'route': 'heartbeat', 'data': {}, 'msgId': 0})
            time.sleep(5)
        except: break
    heartbeat_alive = False

def start_game_actions(ws):
    global in_game, in_room
    time.sleep(1)
    if not is_running: return
    try:
        in_game = True
        in_room = True
        send_ws(ws, {
            'route': 'clientActiveGun',
            'data': {'btype': 1, 'gun': 'gun1', 'skillType': 'none', 'locationX': 0, 'locationY': 0},
            'msgId': 0
        })
        if not heartbeat_alive: threading.Thread(target=heartbeat_loop, args=(ws,), daemon=True).start()
        if not shoot_alive: threading.Thread(target=auto_shoot_loop, args=(ws,), daemon=True).start()
        send_ws(ws, {'route': 'useItem', 'data': {'type': 6}, 'msgId': 0})
        send_ws(ws, {'route': 'getPlayerInfo', 'data': {}, 'msgId': 100})
    except: pass

def handle_message(data, ws):
    global game_creds, login_handled, play_handled, fish_list, in_game
    global coin_balance, coin_change, last_coin_update, in_room, room_lost_time
    try:
        decoded = msgpack.unpackb(data, raw=False)
        if not isinstance(decoded, dict): return
        route = decoded.get('route', '')
        msg_id = decoded.get('msgId', -1)
        inner = decoded.get('data', decoded)
        if route in ['updateCoin', 'OnUpdateCoin', 'updateBalance', 'playerInfo', 'getPlayerInfo']:
            new_coin = inner.get('coin', inner.get('balance', inner.get('coins', coin_balance)))
            if new_coin is not None:
                coin_change = int(new_coin) - int(coin_balance)
                coin_balance = int(new_coin)
                last_coin_update = time.time()
        if route in ['OnUpdateObjects', 'OnUpdateJackpot', 'onSlotJp']:
            if in_room: room_lost_time = None
            if not in_game: threading.Thread(target=start_game_actions, args=(ws,), daemon=True).start()
        if route == 'OnUpdateObjects':
            in_room = True
            room_lost_time = None
            for obj in inner.get('objects', []): fish_list[obj.get('id')] = obj
            for df in inner.get('deadFish', []):
                if df.get('id') in fish_list: del fish_list[df.get('id')]
        if route in ['OnLeaveRoom', 'leaveRoom', 'onLeaveRoom', 'OnKickOut', 'kickOut']:
            in_room = False
            in_game = False
            fish_list = {}
            if room_lost_time is None: room_lost_time = time.time()
        if msg_id == 1 and inner.get('ok'):
            game_creds['username'] = inner.get('username')
            game_creds['password'] = inner.get('password')
            login_handled = True
            send_ws(ws, {
                'route': 'play',
                'data': {'playerId': game_creds['username'], 'password': game_creds['password'], 'index': 0},
                'msgId': 2
            })
        if msg_id == 2:
            play_handled = True
            in_room = True
            room_lost_time = None
    except: pass

def start_ws():
    global ws_conn, is_running, bot_start_time, last_error
    try:
        if bot_start_time is None: bot_start_time = time.time()
        ws = websocket.create_connection(WS_URL, header=WS_HEADERS, sslopt={'cert_reqs': ssl.CERT_NONE})
        with ws_lock: ws_conn = ws
        token = config_data.get('game_access_token')
        send_ws(ws, {'route': 'mytelLogin', 'data': {'accessToken': token, 'language': 'my'}, 'msgId': 1})
        last_error = ''
        threading.Thread(target=room_monitor_loop, daemon=True).start()
        while is_running:
            try:
                ws.settimeout(0.5)
                raw = ws.recv()
                if not raw: break
                handle_message(raw, ws)
            except websocket.WebSocketTimeoutException: continue
            except: break
    except: last_error = '❌ Connection error'
    finally: stop_bot_internal()

# ==========================================
# TELEGRAM UI
# ==========================================
def build_status_text():
    uptime = format_uptime(time.time() - bot_start_time) if bot_start_time and is_running else '—'
    coin_sec = f'💰 Balance: *{format_coin(coin_balance)}*\n📈 Change: *{format_coin(coin_change)}*\n' if coin_balance > 0 else '💰 Balance: *Waiting...*\n'
    return (f'📊 *Bot Status*\n🔘 Bot: *{"🟢 Running" if is_running else "🔴 Stopped"}*\n🎯 Fish Shot: *{fish_shot}*\n⏱️ Uptime: *{uptime}*\n{coin_sec}⚙️ 100k/s Bullet, 3-4 Fish/s')

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    global is_running
    uid = call.message.chat.id
    if not config_data.get('admin_chat_id'):
        config_data['admin_chat_id'] = uid
        save_config()
    data = call.data
    try: bot.delete_message(uid, call.message.message_id)
    except: pass
    if data == 'cmd_menu': show_page(uid, '🤖 *Fish Bot v6.2*', get_main_menu())
    elif data == 'cmd_start':
        if not config_data.get('game_access_token'): return bot.answer_callback_query(call.id, '❌ No Token!')
        if is_running: return bot.answer_callback_query(call.id, '⚠️ Already running!')
        is_running = True
        show_page(uid, '🟡 *Bot Starting...*', get_start_menu())
        threading.Thread(target=start_ws, daemon=True).start()
    elif data == 'cmd_stop':
        stop_bot_internal()
        show_page(uid, '🔴 *Bot Stopped*', get_main_menu())
    elif data == 'cmd_token':
        show_page(uid, '🔑 *Set Token*', get_back_only())
        msg = send_untracked(uid, '👇 *Send token below:*')
        if msg: bot.register_next_step_handler(msg, update_token)
    elif data == 'cmd_status': show_page(uid, build_status_text(), get_status_buttons())
    elif data == 'cmd_balance':
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(InlineKeyboardButton('🔄 Refresh', callback_data='cmd_balance'), InlineKeyboardButton('◀️ Back', callback_data='cmd_menu'))
        show_page(uid, f'💰 *Balance*: {format_coin(coin_balance)}', markup)

@bot.message_handler(commands=['start'])
def handle_start(message):
    config_data['admin_chat_id'] = message.chat.id
    save_config()
    show_page(message.chat.id, '🤖 *Fish Bot v6.2*', get_main_menu())

def update_token(message):
    token = parse_game_url(message.text)
    if token:
        config_data['game_access_token'] = token
        save_config()
        show_page(message.chat.id, '✅ *Token Updated!*', get_main_menu())
    else: show_page(message.chat.id, '❌ *Invalid Token!*', get_main_menu())

load_config()
if __name__ == '__main__':
    bot.infinity_polling(timeout=30)
