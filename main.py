import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import time
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (CallbackQuery, ChatJoinRequest, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, WebAppInfo)

TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ["ADMIN_IDS"].replace(" ", "").split(",") if x}
WEBAPP_URL = os.environ["WEBAPP_URL"].rstrip("/")
DB_PATH = os.getenv("DB_PATH", "/data/ilovalar.db")
BASE = os.path.dirname(os.path.abspath(__file__))

try:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
except OSError:
    DB_PATH = os.path.join(BASE, "ilovalar.db")


def db(sql, args=(), fetch=False):
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        cur = c.execute(sql, args)
        c.commit()
        return [dict(r) for r in cur.fetchall()] if fetch else None
    finally:
        c.close()


def init_db():
    db("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, ts INTEGER)")
    db("CREATE TABLE IF NOT EXISTS apps(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT, ts INTEGER)")
    db("CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, title TEXT, mode TEXT, link TEXT)")
    db("CREATE TABLE IF NOT EXISTS reqs(chat_id INTEGER, uid INTEGER, PRIMARY KEY(chat_id, uid))")


def touch(uid):
    db("INSERT OR IGNORE INTO users VALUES(?,?)", (uid, int(time.time())))


def verify_init(init):
    try:
        d = dict(parse_qsl(init, keep_blank_values=True))
        h = d.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), h):
            return None
        if time.time() - int(d.get("auth_date", "0")) > 86400:
            return None
        return json.loads(d["user"])["id"]
    except Exception:
        return None


bot = Bot(TOKEN)
dp = Dispatcher()


# ---------- Obunani tekshirish ----------
async def is_sub(uid, ch):
    try:
        m = await bot.get_chat_member(ch["chat_id"], uid)
        if m.status in ("member", "administrator", "creator"):
            return True
        if m.status == "restricted" and getattr(m, "is_member", False):
            return True
    except Exception:
        pass
    if ch["mode"] == "request":
        return bool(db("SELECT 1 FROM reqs WHERE chat_id=? AND uid=?", (ch["chat_id"], uid), True))
    return False


async def missing(uid):
    if uid in ADMINS:
        return []
    chs = db("SELECT * FROM channels ORDER BY id", (), True)
    res = await asyncio.gather(*[is_sub(uid, c) for c in chs])
    return [c for c, ok in zip(chs, res) if not ok]


def app_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📱 ILOVALAR", web_app=WebAppInfo(url=WEBAPP_URL))]])


def sub_kb(miss):
    rows = [[InlineKeyboardButton(text="📢 " + c["title"], url=c["link"])] for c in miss]
    rows.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="chk")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(CommandStart())
async def start(m: Message):
    touch(m.from_user.id)
    miss = await missing(m.from_user.id)
    if miss:
        await m.answer("Davom etish uchun kanallarga obuna bo'ling, so'ng «Tekshirish» tugmasini bosing 👇",
                       reply_markup=sub_kb(miss))
    else:
        await m.answer("Xush kelibsiz! 👇", reply_markup=app_kb())


@dp.callback_query(F.data == "chk")
async def chk(cb: CallbackQuery):
    miss = await missing(cb.from_user.id)
    if miss:
        await cb.answer("Hali hammasiga obuna bo'lmadingiz", show_alert=True)
        return
    await cb.message.edit_text("Obuna tasdiqlandi ✅", reply_markup=app_kb())
    await cb.answer()


@dp.chat_join_request()
async def join_req(e: ChatJoinRequest):
    if db("SELECT 1 FROM channels WHERE chat_id=?", (e.chat.id,), True):
        db("INSERT OR IGNORE INTO reqs VALUES(?,?)", (e.chat.id, e.from_user.id))
        try:
            await e.approve()
        except Exception:
            pass


# ---------- Veb API ----------
def uid_of(r):
    return verify_init(r.headers.get("X-Init-Data", ""))


def err(msg, status=400):
    return web.json_response({"error": msg}, status=status)


async def index(_):
    return web.FileResponse(os.path.join(BASE, "index.html"))


async def health(_):
    return web.Response(text="ok")


async def state(r):
    uid = uid_of(r)
    if not uid:
        return web.json_response({"ok": False})
    touch(uid)
    miss = await missing(uid)
    adm = uid in ADMINS
    data = {"ok": True, "admin": adm, "apps": [],
            "missing": [{"id": c["id"], "title": c["title"], "link": c["link"]} for c in miss]}
    if not miss:
        data["apps"] = db("SELECT id,name,url FROM apps ORDER BY id DESC", (), True)
    if adm:
        data["channels"] = db("SELECT id,title,mode FROM channels ORDER BY id", (), True)
        data["users"] = db("SELECT COUNT(*) AS n FROM users", (), True)[0]["n"]
    return web.json_response(data)


def admin_only(r):
    return uid_of(r) in ADMINS


async def add_app(r):
    if not admin_only(r):
        return err("Ruxsat yo'q", 403)
    d = await r.json()
    name, url = str(d.get("name", "")).strip()[:40], str(d.get("url", "")).strip()
    if not name or not url.startswith("https://") or len(url) > 300:
        return err("Nom va https:// bilan boshlanadigan havola kerak")
    db("INSERT INTO apps(name,url,ts) VALUES(?,?,?)", (name, url, int(time.time())))
    return web.json_response({"ok": True})


async def del_app(r):
    if not admin_only(r):
        return err("Ruxsat yo'q", 403)
    db("DELETE FROM apps WHERE id=?", (int(r.match_info["id"]),))
    return web.json_response({"ok": True})


async def add_channel(r):
    if not admin_only(r):
        return err("Ruxsat yo'q", 403)
    d = await r.json()
    raw = str(d.get("chat", "")).strip()
    mode = "request" if d.get("mode") == "request" else "open"
    if not raw:
        return err("Kanal @username yoki ID kiriting")
    chat = int(raw) if raw.lstrip("-").isdigit() else (raw if raw.startswith("@") else "@" + raw)
    try:
        c = await bot.get_chat(chat)
        me = await bot.get_chat_member(c.id, (await bot.me()).id)
        if me.status not in ("administrator", "creator"):
            raise ValueError("not admin")
        if mode == "request":
            link = (await bot.create_chat_invite_link(c.id, name="ilovalar", creates_join_request=True)).invite_link
        elif c.username:
            link = "https://t.me/" + c.username
        else:
            link = (await bot.create_chat_invite_link(c.id, name="ilovalar")).invite_link
    except Exception:
        return err("Kanal topilmadi yoki bot kanalda admin emas")
    db("INSERT INTO channels(chat_id,title,mode,link) VALUES(?,?,?,?)", (c.id, c.title, mode, link))
    return web.json_response({"ok": True})


async def del_channel(r):
    if not admin_only(r):
        return err("Ruxsat yo'q", 403)
    db("DELETE FROM channels WHERE id=?", (int(r.match_info["id"]),))
    return web.json_response({"ok": True})


async def main():
    init_db()
    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/health", health), web.get("/api/state", state),
                    web.post("/api/admin/app", add_app), web.delete("/api/admin/app/{id}", del_app),
                    web.post("/api/admin/channel", add_channel), web.delete("/api/admin/channel/{id}", del_channel)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
