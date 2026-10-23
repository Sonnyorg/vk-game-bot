import os
import re
import time
import json
import hmac
import random
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("vk-game-bot")

VK_TOKEN = os.getenv("VK_TOKEN", "").strip()
VK_GROUP_ID = os.getenv("VK_GROUP_ID", "").strip()
VK_CONFIRMATION_TOKEN = os.getenv("VK_CONFIRMATION_TOKEN", "").strip()
VK_SECRET_KEY = os.getenv("VK_SECRET_KEY", "").strip()
VK_API_VERSION = os.getenv("VK_API_VERSION", "5.199")
DATABASE_PATH = os.getenv("DATABASE_PATH", "game.sqlite3")
PORT = int(os.getenv("PORT", "8080"))
CALLBACK_PATH = os.getenv("CALLBACK_PATH", "/vk")
DAILY_REWARD = int(os.getenv("DAILY_REWARD", "100"))

app = Flask(__name__)
VK_API_URL = "https://api.vk.com/method/"

SHOP = {
    "1": {"name": "Кирка", "price": 250, "description": "Для усердного шахтёра"},
    "2": {"name": "Удочка", "price": 400, "description": "Рыбалка — отличный отдых"},
    "3": {"name": "Талисман удачи", "price": 700, "description": "Редкий коллекционный предмет"},
}

HELP_TEXT = (
    "🎮 Экономическая RPG\n\n"
    "Команды:\n"
    "профиль — твой профиль и баланс\n"
    "топ — 10 самых богатых игроков\n"
    "ежедневно — получить ежедневную награду\n"
    "магазин — список товаров\n"
    "купить <id> — купить предмет, например: купить 1\n"
    "инвентарь — твои предметы\n"
    "клан — информация о своём клане\n"
    "клан создать <название> — создать клан (стоимость 1000 монет)\n"
    "клан вступить <id> — вступить в открытый клан\n"
    "клан список — список открытых кланов\n"
    "клан участники — участники твоего клана\n\n"
    "Совет: заходи каждый день за наградой и копи монеты!"
)

def now_ts():
    return int(time.time())

@contextmanager
def db():
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    parent = Path(DATABASE_PATH).expanduser().parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            coins INTEGER NOT NULL DEFAULT 100,
            created_at INTEGER NOT NULL,
            last_daily INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            item_id TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            UNIQUE(user_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS clans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            owner_id INTEGER NOT NULL REFERENCES users(user_id),
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS clan_members (
            user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            clan_id INTEGER NOT NULL REFERENCES clans(id) ON DELETE CASCADE,
            joined_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS processed_events (
            event_id TEXT PRIMARY KEY,
            processed_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_users_coins ON users(coins DESC);
        CREATE INDEX IF NOT EXISTS idx_clan_members_clan ON clan_members(clan_id);
        """)

def ensure_user(user_id):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(user_id, coins, created_at) VALUES (?, 100, ?)",
            (user_id, now_ts())
        )

def vk_send(peer_id, message):
    if not VK_TOKEN:
        log.error("VK_TOKEN is not configured; message not sent.")
        return
    try:
        response = requests.post(
            VK_API_URL + "messages.send",
            data={
                "access_token": VK_TOKEN,
                "v": VK_API_VERSION,
                "peer_id": int(peer_id),
                "random_id": random.randint(1, 2_000_000_000),
                "message": message[:4000],
            },
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            log.error("VK API error: %s", payload["error"])
    except (requests.RequestException, ValueError) as exc:
        log.exception("Failed to send VK message: %s", exc)

def profile(user_id):
    ensure_user(user_id)
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        membership = conn.execute(
            """SELECT c.id, c.name FROM clans c
               JOIN clan_members m ON m.clan_id=c.id WHERE m.user_id=?""",
            (user_id,)
        ).fetchone()
        item_count = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS n FROM inventory WHERE user_id=?",
            (user_id,)
        ).fetchone()["n"]
    clan = f"{membership['name']} (#{membership['id']})" if membership else "нет"
    return (
        f"👤 Профиль игрока #{user_id}\n"
        f"🪙 Монеты: {user['coins']}\n"
        f"🎒 Предметов: {item_count}\n"
        f"🛡 Клан: {clan}\n"
        f"📅 В игре с: {datetime.fromtimestamp(user['created_at'], timezone.utc).strftime('%Y-%m-%d')} UTC"
    )

def leaderboard():
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, coins FROM users ORDER BY coins DESC, created_at ASC LIMIT 10"
        ).fetchall()
    if not rows:
        return "Пока игроков нет. Напиши «профиль», чтобы начать!"
    lines = ["🏆 Топ-10 игроков по монетам:"]
    for index, row in enumerate(rows, 1):
        lines.append(f"{index}. Игрок #{row['user_id']} — 🪙 {row['coins']}")
    return "\n".join(lines)

def daily(user_id):
    ensure_user(user_id)
    now = now_ts()
    with db() as conn:
        row = conn.execute(
            "SELECT coins, last_daily FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        remaining = 86400 - (now - row["last_daily"])
        if remaining > 0:
            hours, rem = divmod(remaining, 3600)
            minutes = rem // 60
            return f"⏳ Награда уже получена. Возвращайся через {hours} ч. {minutes} мин."
        conn.execute(
            "UPDATE users SET coins=coins+?, last_daily=? WHERE user_id=?",
            (DAILY_REWARD, now, user_id)
        )
        balance = conn.execute(
            "SELECT coins FROM users WHERE user_id=?", (user_id,)
        ).fetchone()["coins"]
    return f"🎁 Ты получил {DAILY_REWARD} монет!\n🪙 Баланс: {balance}"

def shop_text():
    lines = ["🛒 Магазин:"]
    for item_id, item in SHOP.items():
        lines.append(f"{item_id}. {item['name']} — {item['price']} монет. {item['description']}")
    lines.append("\nПокупка: купить <id>")
    return "\n".join(lines)

def buy_item(user_id, item_id):
    ensure_user(user_id)
    item = SHOP.get(item_id)
    if not item:
        return "❌ Такого товара нет. Напиши «магазин»."
    with db() as conn:
        user = conn.execute("SELECT coins FROM users WHERE user_id=?", (user_id,)).fetchone()
        if user["coins"] < item["price"]:
            return f"❌ Не хватает монет. Цена: {item['price']}, у тебя: {user['coins']}."
        conn.execute("UPDATE users SET coins=coins-? WHERE user_id=?", (item["price"], user_id))
        conn.execute(
            """INSERT INTO inventory(user_id,item_id,quantity) VALUES(?,?,1)
               ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+1""",
            (user_id, item_id)
        )
        balance = conn.execute("SELECT coins FROM users WHERE user_id=?", (user_id,)).fetchone()["coins"]
    return f"✅ Куплено: {item['name']}!\n🪙 Остаток: {balance} монет."

def inventory_text(user_id):
    ensure_user(user_id)
    with db() as conn:
        rows = conn.execute(
            "SELECT item_id, quantity FROM inventory WHERE user_id=? ORDER BY item_id", (user_id,)
        ).fetchall()
    if not rows:
        return "🎒 Инвентарь пуст. Загляни в «магазин»!"
    lines = ["🎒 Твой инвентарь:"]
    for row in rows:
        item = SHOP.get(row["item_id"], {"name": "Неизвестный предмет"})
        lines.append(f"• {item['name']} × {row['quantity']}")
    return "\n".join(lines)

def clan_command(user_id, args):
    ensure_user(user_id)
    action = args[0].lower() if args else ""
    if action in ("создать", "create"):
        name = " ".join(args[1:]).strip()
        if len(name) < 3 or len(name) > 30:
            return "Укажи название клана от 3 до 30 символов: клан создать Драконы"
        with db() as conn:
            member = conn.execute("SELECT clan_id FROM clan_members WHERE user_id=?", (user_id,)).fetchone()
            if member:
                return "❌ Сначала выйди из текущего клана (выход из клана пока доступен владельцу через админ-панель БД)."
            user = conn.execute("SELECT coins FROM users WHERE user_id=?", (user_id,)).fetchone()
            if user["coins"] < 1000:
                return "❌ Создание клана стоит 1000 монет."
            try:
                cur = conn.execute(
                    "INSERT INTO clans(name,owner_id,created_at) VALUES(?,?,?)",
                    (name, user_id, now_ts())
                )
            except sqlite3.IntegrityError:
                return "❌ Клан с таким названием уже существует."
            clan_id = cur.lastrowid
            conn.execute("UPDATE users SET coins=coins-1000 WHERE user_id=?", (user_id,))
            conn.execute(
                "INSERT INTO clan_members(user_id,clan_id,joined_at) VALUES(?,?,?)",
                (user_id, clan_id, now_ts())
            )
        return f"🛡 Клан «{name}» создан! ID клана: {clan_id}. Потрачено 1000 монет."
    if action in ("вступить", "join"):
        if len(args) < 2 or not args[1].isdigit():
            return "Укажи ID клана: клан вступить 1"
        clan_id = int(args[1])
        with db() as conn:
            if conn.execute("SELECT 1 FROM clan_members WHERE user_id=?", (user_id,)).fetchone():
                return "❌ Ты уже состоишь в клане."
            clan = conn.execute("SELECT name FROM clans WHERE id=?", (clan_id,)).fetchone()
            if not clan:
                return "❌ Клан не найден."
            conn.execute(
                "INSERT INTO clan_members(user_id,clan_id,joined_at) VALUES(?,?,?)",
                (user_id, clan_id, now_ts())
            )
        return f"✅ Ты вступил в клан «{clan['name']}»!"
    if action in ("список", "list"):
        with db() as conn:
            rows = conn.execute(
                """SELECT c.id,c.name,COUNT(m.user_id) AS members
                   FROM clans c LEFT JOIN clan_members m ON m.clan_id=c.id
                   GROUP BY c.id ORDER BY members DESC,c.id ASC LIMIT 10"""
            ).fetchall()
        if not rows:
            return "Пока кланов нет. Создай первый: клан создать <название>"
        return "🛡 Кланы:\n" + "\n".join(
            f"#{r['id']} «{r['name']}» — {r['members']} участн." for r in rows
        )
    if action in ("участники", "members"):
        with db() as conn:
            clan = conn.execute(
                """SELECT c.id,c.name FROM clans c JOIN clan_members m ON m.clan_id=c.id
                   WHERE m.user_id=?""", (user_id,)
            ).fetchone()
            if not clan:
                return "Ты пока не состоишь в клане."
            rows = conn.execute(
                "SELECT user_id FROM clan_members WHERE clan_id=? ORDER BY joined_at LIMIT 50",
                (clan["id"],)
            ).fetchall()
        return f"👥 Участники клана «{clan['name']}»:\n" + "\n".join(
            f"• Игрок #{r['user_id']}" for r in rows
        )
    with db() as conn:
        clan = conn.execute(
            """SELECT c.id,c.name,c.owner_id,COUNT(m.user_id) AS members
               FROM clans c JOIN clan_members mine ON mine.clan_id=c.id
               JOIN clan_members m ON m.clan_id=c.id
               WHERE mine.user_id=? GROUP BY c.id""", (user_id,)
        ).fetchone()
    if not clan:
        return "Ты не в клане. Напиши «клан список» или «клан создать <название>»."
    return (
        f"🛡 Клан «{clan['name']}» (#{clan['id']})\n"
        f"👑 Владелец: игрок #{clan['owner_id']}\n"
        f"👥 Участников: {clan['members']}\n"
        "Команды: клан участники, клан список"
    )

def handle_message(user_id, text):
    text = (text or "").strip()
    if not text:
        return "Напиши «помощь», чтобы увидеть команды."
    normalized = text.lower()
    if normalized in ("начать", "старт", "start", "помощь", "помощь!", "help", "команды"):
        return HELP_TEXT
    if normalized in ("профиль", "проф", "profile", "баланс"):
        return profile(user_id)
    if normalized in ("топ", "топ игроков", "top", "рейтинг"):
        return leaderboard()
    if normalized in ("ежедневно", "дейли", "daily", "награда"):
        return daily(user_id)
    if normalized in ("магазин", "shop"):
        return shop_text()
    if normalized.startswith("купить ") or normalized.startswith("buy "):
        return buy_item(user_id, text.split(maxsplit=1)[1].strip())
    if normalized in ("инвентарь", "рюкзак", "inventory"):
        return inventory_text(user_id)
    if normalized == "клан" or normalized.startswith("клан ") or normalized.startswith("clan"):
        parts = text.split()
        return clan_command(user_id, parts[1:])
    return "🤔 Не знаю такую команду. Напиши «помощь»."

@app.get("/")
def index():
    return jsonify({"service": "VK Game Bot", "status": "ok"})

@app.get("/health")
def health():
    return jsonify({"status": "ok"})

@app.post(CALLBACK_PATH)
def vk_callback():
    data = request.get_json(silent=True) or {}
    event_type = data.get("type", "")
    group_id = str(data.get("group_id", ""))

    if VK_GROUP_ID and group_id != VK_GROUP_ID:
        return "forbidden", 403
    if VK_SECRET_KEY and not hmac.compare_digest(str(data.get("secret", "")), VK_SECRET_KEY):
        return "forbidden", 403

    if event_type == "confirmation":
        if not VK_CONFIRMATION_TOKEN:
            log.error("VK_CONFIRMATION_TOKEN is not configured.")
            return "confirmation token is not configured", 500
        return VK_CONFIRMATION_TOKEN

    event_id = str(data.get("event_id", ""))
    if event_id:
        with db() as conn:
            exists = conn.execute(
                "SELECT 1 FROM processed_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if exists:
                return "ok"
            conn.execute(
                "INSERT INTO processed_events(event_id,processed_at) VALUES(?,?)",
                (event_id, now_ts())
            )

    try:
        if event_type == "message_new":
            obj = data.get("object", {})
            message = obj.get("message", obj)
            user_id = int(message.get("from_id", 0))
            peer_id = int(message.get("peer_id", user_id))
            body = message.get("text", "")
            # Ignore messages sent by communities and other non-user senders.
            if user_id > 0 and body:
                answer = handle_message(user_id, body)
                vk_send(peer_id, answer)
    except Exception:
        log.exception("Failed to process VK callback event")
        # Callback receives "ok" to prevent endless retries; inspect server logs.
    return "ok"

if __name__ == "__main__":
    if not VK_TOKEN:
        log.warning("VK_TOKEN is empty. Callback will run, but replies cannot be sent.")
    init_db()
    app.run(host="0.0.0.0", port=PORT)
