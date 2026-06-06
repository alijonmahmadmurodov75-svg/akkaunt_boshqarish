import asyncio
import random
import logging
import re
import json
from collections import defaultdict
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, FloodWaitError,
    UserAlreadyParticipantError, InviteHashExpiredError,
    ChatAdminRequiredError, ChannelPrivateError
)
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.phone import JoinGroupCallRequest, LeaveGroupCallRequest
from telethon.tl.types import UpdateGroupCall, GroupCall, DataJSON

import config
import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

clients: dict[int, TelegramClient]  = {}
group_activity: dict[int, list]     = defaultdict(list)
auto_tasks: dict[int, asyncio.Task] = {}
manual_picks: dict[int, list]       = defaultdict(list)
# akk_id -> {chat_id -> asyncio.Task}
vc_ping_tasks: dict[int, dict]      = defaultdict(dict)


# ═══════════════════════════════════════════════════════════════════════════════
# FSM
# ═══════════════════════════════════════════════════════════════════════════════

class AkkuntQoshish(StatesGroup):
    telefon = State()
    kod     = State()
    parol   = State()
    nom     = State()

class AdminQoshish(StatesGroup):
    id = State()

class SozQoshish(StatesGroup):
    soz = State()

class GuruhQoshilish(StatesGroup):
    link = State()

class VideoChat(StatesGroup):
    link = State()
    son  = State()

class SessionLogin(StatesGroup):
    kod   = State()
    parol = State()

class LoginAll(StatesGroup):
    kod   = State()
    parol = State()



# ═══════════════════════════════════════════════════════════════════════════════
# Yordamchi
# ═══════════════════════════════════════════════════════════════════════════════

def ikb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
        for row in rows
    ])

async def admin_mi(uid):      return bool(await db.get_admin(uid))
async def bosh_admin_mi(uid):
    a = await db.get_admin(uid)
    return bool(a and a["role"] == "main_admin")

async def adminlarga_xabar(matn, markup=None):
    for a in await db.get_all_admins():
        try:
            await bot.send_message(a["telegram_id"], matn, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass

def holat_belgisi(h): return {"idle":"🟢","busy":"🔴","paused":"⏸","failed":"❌"}.get(h,"❓")
def holat_nomi(h):    return {"idle":"Bo'sh","busy":"Band","paused":"Pauza","failed":"Xato"}.get(h,h)

def asosiy_menyu():
    return ikb([
        [("👤 Akkauntlar","m:akkauntlar"),("➕ Qo'shish","m:qoshish")],
        [("📊 Holat","m:holat"),("👥 Guruhga qo'sh","m:guruh")],
        [("🗣 Avto xabar","m:avto"),("🎥 Video chat","m:vc")],
        [("👁 Monitoring","m:monitor"),("⚙️ Sozlamalar","m:sozlamalar")],
    ])

async def xavfsiz_tahrir(cb, matn, markup=None):
    try:
        await cb.message.edit_text(matn, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        await cb.message.answer(matn, reply_markup=markup, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════════
# Client olish
# ═══════════════════════════════════════════════════════════════════════════════

async def _get_client(akk_id: int) -> TelegramClient | None:
    c = clients.get(akk_id)
    if c:
        if not c.is_connected():
            try:
                await c.connect()
            except Exception as e:
                log.error(f"[get_client] qayta ulanish xato akk={akk_id}: {e}")
                del clients[akk_id]
                c = None
        if c:
            return c

    akk = await db.get_account(akk_id)
    if not akk:
        log.error(f"[get_client] akk={akk_id} DB da yo'q")
        return None

    ses = (akk.get("session_string") or "").strip()
    if not ses:
        log.error(f"[get_client] akk={akk_id} ({akk.get('phone')}) session_string bo'sh!")
        return None

    try:
        c = TelegramClient(StringSession(ses), config.API_ID, config.API_HASH,
                           connection_retries=3, retry_delay=2)
        await c.connect()
        if not await c.is_user_authorized():
            log.error(f"[get_client] akk={akk_id} avtorizatsiya yo'q")
            await db.update_account_status(akk_id, "failed")
            return None
        me = await c.get_me()
        log.info(f"[get_client] akk={akk_id} ✅ @{me.username or me.id}")
        clients[akk_id] = c
        await _handlerlarni_qoshish(akk_id, c)
        return c
    except Exception as e:
        log.error(f"[get_client] akk={akk_id} XATO: {e}")
        await db.update_account_status(akk_id, "failed")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Link parse va guruhga qo'shilish
# ═══════════════════════════════════════════════════════════════════════════════

def link_parse(link: str) -> dict | None:
    link = link.strip()
    inv = re.search(r"t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)", link)
    if inv:
        hm = re.search(r"(?:videochat|voicechat|livestream)=?([^&\s]*)", link)
        return {"type": "invite", "value": inv.group(1), "vc_hash": hm.group(1) if hm else ""}
    usr = re.search(r"(?:t\.me/|@)([A-Za-z0-9_]{3,})", link)
    if usr:
        hm = re.search(r"(?:videochat|voicechat|livestream)=?([^&\s]*)", link)
        return {"type": "username", "value": usr.group(1), "vc_hash": hm.group(1) if hm else ""}
    return None


async def guruhga_qoshil(client: TelegramClient, link: str):
    """
    Guruhga qo'shiladi. Entity qaytaradi.
    Allaqachon a'zo bo'lsa — xatosiz o'tadi.
    """
    info = link_parse(link)
    if not info:
        raise ValueError(f"Noto'g'ri link: {link}")

    if info["type"] == "invite":
        try:
            result = await client(ImportChatInviteRequest(info["value"]))
            chats  = getattr(result, "chats", [])
            if chats:
                return chats[0]
        except UserAlreadyParticipantError:
            pass
        except Exception as e:
            raise e
        # A'zo bo'lib bo'lsa entity ni qaytaramiz
        try:
            return await client.get_entity(f"https://t.me/+{info['value']}")
        except Exception:
            raise RuntimeError("Invite link orqali entity topilmadi")
    else:
        entity = await client.get_entity(info["value"])
        try:
            await client(JoinChannelRequest(entity))
        except UserAlreadyParticipantError:
            pass
        return entity


# ═══════════════════════════════════════════════════════════════════════════════
# /start
# ═══════════════════════════════════════════════════════════════════════════════


@dp.message(Command("login_all"))
async def cmd_login_all(msg: Message, state: FSMContext):
    if not await admin_mi(msg.from_user.id):
        return await msg.answer("Ruxsat yo'q.")
    pool = await db.get_pool()
    yoqlar = await pool.fetch(
        "SELECT * FROM accounts "
        "WHERE (session_string IS NULL OR session_string='') "
        "AND status!='paused' ORDER BY id"
    )
    if not yoqlar:
        return await msg.answer("✅ Barcha akkuntlarda session bor!")
    navbat = [dict(a) for a in yoqlar]
    await state.update_data(navbat=navbat, idx=0, ok=0, xato=0)
    await msg.answer(
        "📋 Session yo'q: {} ta akkunt\nBirma-bir login qilamiz...\n\n/skip — o'tkazib yuborish\n/stop_login — to'xtatish".format(len(navbat))
    )
    await asyncio.sleep(1)
    await _la_yuborish(msg, state)


async def _la_yuborish(msg, state):
    """Navbatdagi akkuntga kod yuboradi."""
    malumot  = await state.get_data()
    navbat   = malumot["navbat"]
    idx      = malumot["idx"]
    if idx >= len(navbat):
        ok   = malumot.get("ok", 0)
        xato = malumot.get("xato", 0)
        await state.clear()
        await msg.answer("Hammasi tugadi!\nMuvaffaqiyatli: {}\nXato: {}".format(ok, xato), reply_markup=asosiy_menyu())
        return

    akk     = navbat[idx]
    telefon = akk["phone"]
    akk_id  = akk["id"]

    try:
        client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
        await client.connect()
        natija = await client.send_code_request(telefon)
        clients[akk_id] = client
        await state.update_data(
            la_akk_id=akk_id,
            la_telefon=telefon,
            la_hash=natija.phone_code_hash
        )
        await state.set_state(LoginAll.kod)
        await msg.answer(
            "{}/{} — {}  kod yuborildi, kiriting:".format(idx+1, len(navbat), telefon)
        )
    except FloodWaitError as e:
        try: await client.disconnect()
        except: pass
        d = e.seconds // 60 + 1
        await state.update_data(xato=malumot.get("xato",0)+1, idx=idx+1)
        await msg.answer("{}: FloodWait {} daqiqa, o'tkazildi.".format(telefon, d))
        await asyncio.sleep(2)
        await _la_yuborish(msg, state)
    except Exception as e:
        try: await client.disconnect()
        except: pass
        await state.update_data(xato=malumot.get("xato",0)+1, idx=idx+1)
        await msg.answer("{}: xato — {}, o'tkazildi.".format(telefon, str(e)[:60]))
        await asyncio.sleep(2)
        await _la_yuborish(msg, state)


@dp.message(Command("skip"), LoginAll.kod)
@dp.message(Command("skip"), LoginAll.parol)
async def la_skip(msg: Message, state: FSMContext):
    malumot = await state.get_data()
    akk_id  = malumot.get("la_akk_id")
    if akk_id and akk_id in clients:
        try: await clients[akk_id].disconnect()
        except: pass
        clients.pop(akk_id, None)
    await state.update_data(xato=malumot.get("xato",0)+1, idx=malumot.get("idx",0)+1)
    await msg.answer("O'tkazildi.")
    await asyncio.sleep(1)
    await _la_yuborish(msg, state)


@dp.message(Command("stop_login"))
async def la_stop(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("To'xtatildi.", reply_markup=asosiy_menyu())


@dp.message(LoginAll.kod)
async def la_kod(msg: Message, state: FSMContext):
    if msg.text and msg.text.startswith("/"):
        return
    malumot = await state.get_data()
    akk_id  = malumot.get("la_akk_id")
    telefon = malumot.get("la_telefon")
    hsh     = malumot.get("la_hash")
    client  = clients.get(akk_id)

    if not client or not hsh:
        await state.update_data(xato=malumot.get("xato",0)+1, idx=malumot.get("idx",0)+1)
        await msg.answer("Sessiya topilmadi, o'tkazildi.")
        return await _la_yuborish(msg, state)

    try:
        await client.sign_in(telefon, msg.text.strip(), phone_code_hash=hsh)
        await _la_saqlash(msg, state, akk_id, client)
    except SessionPasswordNeededError:
        await state.set_state(LoginAll.parol)
        await msg.answer("2FA paroli:")
    except Exception as e:
        clients.pop(akk_id, None)
        try: await client.disconnect()
        except: pass
        await state.update_data(xato=malumot.get("xato",0)+1, idx=malumot.get("idx",0)+1)
        await msg.answer("Kod xato: {}, o'tkazildi.".format(str(e)[:60]))
        await asyncio.sleep(1)
        await _la_yuborish(msg, state)


@dp.message(LoginAll.parol)
async def la_parol(msg: Message, state: FSMContext):
    if msg.text and msg.text.startswith("/"):
        return
    malumot = await state.get_data()
    akk_id  = malumot.get("la_akk_id")
    client  = clients.get(akk_id)
    if not client:
        await state.update_data(xato=malumot.get("xato",0)+1, idx=malumot.get("idx",0)+1)
        return await _la_yuborish(msg, state)
    try:
        await client.sign_in(password=msg.text.strip())
        await _la_saqlash(msg, state, akk_id, client)
    except Exception as e:
        await state.update_data(xato=malumot.get("xato",0)+1, idx=malumot.get("idx",0)+1)
        await msg.answer("Parol xato: {}, o'tkazildi.".format(str(e)[:60]))
        await asyncio.sleep(1)
        await _la_yuborish(msg, state)


async def _la_saqlash(msg, state, akk_id, client):
    """Session saqlaydi, keyingisiga o'tadi."""
    men = await client.get_me()
    ses = client.session.save()
    await db.update_account_session(akk_id, ses, men.username)
    pool = await db.get_pool()
    nom  = men.first_name or men.username or str(men.id)
    await pool.execute(
        "UPDATE accounts SET display_name=COALESCE(NULLIF(display_name,''), $1) WHERE id=$2",
        nom, akk_id
    )
    await _handlerlarni_qoshish(akk_id, client)
    malumot = await state.get_data()
    await state.update_data(ok=malumot.get("ok",0)+1, idx=malumot.get("idx",0)+1)
    await state.set_state(None)
    await msg.answer("{} ({}) — saqlandi!".format(nom, men.phone))
    await asyncio.sleep(1)
    await _la_yuborish(msg, state)

@dp.message(Command("reload"))
async def cmd_reload(msg: Message):
    """Restart qilmasdan barcha clientlarni qayta yuklaydi."""
    if not await admin_mi(msg.from_user.id):
        return await msg.answer("❌ Ruxsat yo'q.")
    await msg.answer("🔄 Clientlar qayta yuklanmoqda...")
    eski = len(clients)
    await _barcha_clientlarni_yukla()
    yangi = len(clients)
    await msg.answer(
        f"✅ Yuklash tugadi!\nOldin: {eski} ta\nHozir: {yangi} ta client",
        reply_markup=asosiy_menyu()
    )

@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    if not await admin_mi(msg.from_user.id):
        return await msg.answer("❌ Siz admin emassiz.")
    await msg.answer("🤖 Boshqaruv paneli:", reply_markup=asosiy_menyu())

@dp.callback_query(F.data == "m:bosh")
async def bosh_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await xavfsiz_tahrir(cb, "🤖 Boshqaruv paneli:", asosiy_menyu())


# ═══════════════════════════════════════════════════════════════════════════════
# Akkauntlar
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "m:akkauntlar")
async def menu_akkauntlar(cb: CallbackQuery):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    akkauntlar = await db.get_all_accounts()
    if not akkauntlar:
        return await xavfsiz_tahrir(cb, "Hech qanday akkunt yo'q.", ikb([[("⬅️ Orqaga","m:bosh")]]))
    qatorlar = [[(
        f"{holat_belgisi(a['status'])} {a['display_name'] or a['phone']} — {holat_nomi(a['status'])}",
        f"akk:{a['id']}"
    )] for a in akkauntlar]
    qatorlar.append([("➕ Qo'shish","m:qoshish"),("⬅️ Orqaga","m:bosh")])
    await xavfsiz_tahrir(cb, "📋 <b>Akkauntlar:</b>", ikb(qatorlar))

@dp.callback_query(F.data.startswith("akk:"))
async def akk_detail(cb: CallbackQuery):
    akk = await db.get_account(int(cb.data.split(":")[1]))
    if not akk: return await cb.answer("Topilmadi")
    has_ses = bool((akk.get("session_string") or "").strip())
    oxirgi  = akk["last_active"].strftime("%d.%m %H:%M") if akk["last_active"] else "—"
    matn = (
        f"{holat_belgisi(akk['status'])} <b>{akk['display_name'] or akk['phone']}</b>\n"
        f"📱 <code>{akk['phone']}</code>\n"
        f"👤 @{akk['username'] or '—'}\n"
        f"💾 Sessiya: {'✅' if has_ses else '❌ YO\'Q'}\n"
        f"🔵 Holat: <b>{holat_nomi(akk['status'])}</b>\n"
        f"🕐 Oxirgi faollik: {oxirgi}"
    )
    aid = akk["id"]
    kb = [
        [("⏸ Pauza",f"hs:{aid}:paused"),("▶️ Bo'shatish",f"hs:{aid}:idle")],
    ]
    if not has_ses:
        kb.append([("🔑 Login qil",f"ses_login:{aid}")])
    kb.append([("🗑 O'chirish",f"ochir:{aid}")])
    kb.append([("⬅️ Orqaga","m:akkauntlar")])
    await xavfsiz_tahrir(cb, matn, ikb(kb))

@dp.callback_query(F.data.startswith("ses_login:"))
async def ses_login_boshlash(cb: CallbackQuery, state: FSMContext):
    """Session yo'q akkuntga login qilish."""
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    akk_id = int(cb.data.split(":")[1])
    akk    = await db.get_account(akk_id)
    if not akk: return await cb.answer("Topilmadi")

    telefon = akk["phone"]
    await cb.answer("📨 Kod yuborilmoqda...")

    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    await client.connect()
    try:
        natija = await client.send_code_request(telefon)
        clients[akk_id] = client
        await state.update_data(
            telefon=telefon, akk_id=akk_id,
            hash=natija.phone_code_hash, login_msg_id=cb.message.message_id
        )
        await state.set_state(SessionLogin.kod)
        await cb.message.answer(
            f"📨 <b>{telefon}</b> ga kod yuborildi.\n\nKodni kiriting:",
            parse_mode="HTML"
        )
    except FloodWaitError as e:
        await client.disconnect()
        d = e.seconds // 60
        await cb.message.answer(f"⏳ FloodWait: {d} daqiqa kuting.")
    except Exception as e:
        await client.disconnect()
        await cb.message.answer(f"❌ Xato: {e}")

@dp.message(SessionLogin.kod)
async def ses_kod_qabul(msg: Message, state: FSMContext):
    malumot = await state.get_data()
    akk_id  = malumot["akk_id"]
    client  = clients.get(akk_id)
    if not client:
        await state.clear()
        return await msg.answer("❌ Sessiya topilmadi.", reply_markup=asosiy_menyu())
    try:
        await client.sign_in(malumot["telefon"], msg.text.strip(), phone_code_hash=malumot["hash"])
        await _ses_saqlash(msg, state, akk_id, client)
    except SessionPasswordNeededError:
        await state.set_state(SessionLogin.parol)
        await msg.answer("🔐 2FA parolini kiriting:")
    except Exception as e:
        await state.clear()
        if akk_id in clients:
            try: await clients[akk_id].disconnect()
            except: pass
            del clients[akk_id]
        await msg.answer(f"❌ Kod xato: {e}", reply_markup=asosiy_menyu())

@dp.message(SessionLogin.parol)
async def ses_parol_qabul(msg: Message, state: FSMContext):
    malumot = await state.get_data()
    akk_id  = malumot["akk_id"]
    client  = clients.get(akk_id)
    if not client:
        await state.clear()
        return await msg.answer("❌ Client topilmadi.", reply_markup=asosiy_menyu())
    try:
        await client.sign_in(password=msg.text.strip())
        await _ses_saqlash(msg, state, akk_id, client)
    except Exception as e:
        await state.clear()
        await msg.answer(f"❌ Parol xato: {e}", reply_markup=asosiy_menyu())

async def _ses_saqlash(msg: Message, state: FSMContext, akk_id: int, client: TelegramClient):
    """Session saqlash — SessionLogin uchun."""
    men = await client.get_me()
    ses = client.session.save()
    log.info(f"[ses_saqlash] akk={akk_id}, len={len(ses)}, @{men.username}")
    await db.update_account_session(akk_id, ses, men.username)
    pool = await db.get_pool()
    avto_nom = men.first_name or men.username or str(men.id)
    await pool.execute(
        "UPDATE accounts SET display_name=COALESCE(NULLIF(display_name,''), $1) WHERE id=$2",
        avto_nom, akk_id
    )
    await state.clear()
    await _handlerlarni_qoshish(akk_id, client)
    await msg.answer(
        f"✅ <b>{avto_nom}</b> — session saqlandi!\n"
        f"📱 <code>{men.phone}</code>\n"
        f"💾 Session uzunligi: {len(ses)}",
        parse_mode="HTML", reply_markup=asosiy_menyu()
    )

@dp.callback_query(F.data.startswith("hs:"))
async def holat_ozgartir(cb: CallbackQuery):
    _, aid, holat = cb.data.split(":")
    await db.update_account_status(int(aid), holat)
    await cb.answer(f"✅ {holat_nomi(holat)}")
    cb.data = f"akk:{aid}"
    await akk_detail(cb)

@dp.callback_query(F.data.startswith("ochir:"))
async def akkunt_ochir(cb: CallbackQuery):
    aid = int(cb.data.split(":")[1])
    if aid in clients:
        try: await clients[aid].disconnect()
        except: pass
        del clients[aid]
    await db.delete_account(aid)
    await cb.answer("🗑 O'chirildi")
    cb.data = "m:akkauntlar"
    await menu_akkauntlar(cb)


# ═══════════════════════════════════════════════════════════════════════════════
# Holat
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "m:holat")
async def menu_holat(cb: CallbackQuery):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    akkauntlar = await db.get_all_accounts()
    if not akkauntlar:
        return await xavfsiz_tahrir(cb, "Akkunt yo'q.", ikb([[("⬅️","m:bosh")]]))
    bosh  = sum(1 for a in akkauntlar if a["status"]=="idle")
    band  = sum(1 for a in akkauntlar if a["status"]=="busy")
    pauza = sum(1 for a in akkauntlar if a["status"]=="paused")
    xato  = sum(1 for a in akkauntlar if a["status"]=="failed")
    qatorlar = []
    for a in akkauntlar:
        cli = "🔗" if a["id"] in clients else "⚠️"
        ses = "💾" if (a.get("session_string") or "").strip() else "❌"
        qatorlar.append(f"{holat_belgisi(a['status'])} {cli}{ses} <b>{a['display_name'] or a['phone']}</b>")
    matn = (
        f"📊 <b>Akkunt holati</b>\n"
        f"🟢 {bosh}  🔴 {band}  ⏸ {pauza}  ❌ {xato}  🔗 {len(clients)}\n\n"
        + "\n".join(qatorlar)
        + "\n\n<i>🔗=ulangan ⚠️=ulanmagan 💾=sessiya bor ❌=sessiya yo'q</i>"
    )
    await xavfsiz_tahrir(cb, matn, ikb([[("🔄 Yangilash","m:holat"),("⬅️ Orqaga","m:bosh")]]))


# ═══════════════════════════════════════════════════════════════════════════════
# Akkunt qo'shish
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "m:qoshish")
async def qoshish_boshlash(cb: CallbackQuery, state: FSMContext):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    await xavfsiz_tahrir(cb, "📱 Telefon raqamni kiriting:\n<code>+998901234567</code>")
    await state.set_state(AkkuntQoshish.telefon)

@dp.message(AkkuntQoshish.telefon)
async def telefon_qabul(msg: Message, state: FSMContext):
    telefon = msg.text.strip()
    if not telefon.startswith("+"):
        return await msg.answer("❌ Format: +998901234567")
    qator  = await db.add_account(telefon, "")
    akk_id = qator["id"]
    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    await client.connect()
    try:
        natija = await client.send_code_request(telefon)
        clients[akk_id] = client
        await state.update_data(telefon=telefon, akk_id=akk_id, hash=natija.phone_code_hash)
        await state.set_state(AkkuntQoshish.kod)
        await msg.answer("📨 SMS kod yuborildi. Kodni kiriting:")
    except FloodWaitError as e:
        await db.delete_account(akk_id)
        await client.disconnect()
        await state.clear()
        d = e.seconds // 60; s = d // 60
        vaqt = f"{s} soat {d%60} daqiqa" if s > 0 else f"{d} daqiqa"
        await msg.answer(
            f"⏳ <b>Telegram cheklash qo'ydi</b>\n\n<b>{vaqt}</b> kuting.",
            parse_mode="HTML", reply_markup=asosiy_menyu()
        )
    except Exception as e:
        await db.delete_account(akk_id)
        await client.disconnect()
        await state.clear()
        await msg.answer(f"❌ Xatolik: {e}", reply_markup=asosiy_menyu())

@dp.message(AkkuntQoshish.kod)
async def kod_qabul(msg: Message, state: FSMContext):
    malumot = await state.get_data()
    akk_id  = malumot["akk_id"]
    client  = clients.get(akk_id)
    if not client:
        await state.clear()
        return await msg.answer("❌ Sessiya topilmadi.", reply_markup=asosiy_menyu())
    try:
        await client.sign_in(malumot["telefon"], msg.text.strip(), phone_code_hash=malumot["hash"])
        await _sessiya_saqlash(msg, state, akk_id, client)
    except SessionPasswordNeededError:
        await state.set_state(AkkuntQoshish.parol)
        await msg.answer("🔐 2FA parolini kiriting:")
    except Exception as e:
        await state.clear()
        await msg.answer(f"❌ Kod xato: {e}", reply_markup=asosiy_menyu())

@dp.message(AkkuntQoshish.parol)
async def parol_qabul(msg: Message, state: FSMContext):
    malumot = await state.get_data()
    client  = clients.get(malumot["akk_id"])
    try:
        await client.sign_in(password=msg.text.strip())
        await _sessiya_saqlash(msg, state, malumot["akk_id"], client)
    except Exception as e:
        await state.clear()
        await msg.answer(f"❌ Parol xato: {e}", reply_markup=asosiy_menyu())

@dp.message(AkkuntQoshish.nom)
async def nom_qabul(msg: Message, state: FSMContext):
    malumot = await state.get_data()
    akk_id  = malumot["akk_id"]
    nom     = msg.text.strip() or malumot.get("avto_nom", malumot["telefon"])
    # Avval session DB ga yozamiz
    await db.update_account_session(akk_id, malumot["sessiya"], malumot.get("username"))
    pool = await db.get_pool()
    await pool.execute("UPDATE accounts SET display_name=$1 WHERE id=$2", nom, akk_id)
    log.info(f"[nom_qabul] akk={akk_id} session DB ga saqlandi, len={len(malumot.get("sessiya",""))}")
    await state.clear()
    # Client allaqachon clients dict da — handlerlarni qo'shamiz
    client = clients.get(akk_id)
    if client:
        await _handlerlarni_qoshish(akk_id, client)
        log.info(f"[nom_qabul] akk={akk_id} handler qo'shildi, client bor")
    else:
        # Fallback: DB dan qayta yukla
        log.warning(f"[nom_qabul] akk={akk_id} clients da yo'q, DB dan yuklanmoqda...")
        await _get_client(akk_id)
    await msg.answer(f"✅ Akkunt qo'shildi: <b>{nom}</b>", reply_markup=asosiy_menyu(), parse_mode="HTML")

async def _sessiya_saqlash(msg, state, akk_id, client):
    men      = await client.get_me()
    ses      = client.session.save()
    avto_nom = men.first_name or men.username or str(men.id)
    log.info(f"[sessiya] akk={akk_id}, len={len(ses)}, @{men.username}")
    await state.update_data(sessiya=ses, username=men.username, avto_nom=avto_nom)
    await state.set_state(AkkuntQoshish.nom)
    await msg.answer(
        f"✅ Kirish muvaffaqiyatli!\n👤 @{men.username or '—'}\n\n"
        f"Akkunt uchun nom kiriting (bo'sh = <b>{avto_nom}</b>):",
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Guruhga qo'shish
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "m:guruh")
async def guruh_boshlash(cb: CallbackQuery, state: FSMContext):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    bosh = await db.get_accounts_by_status("idle")
    if not bosh:
        return await xavfsiz_tahrir(cb, "❌ Bo'sh akkunt yo'q.", ikb([[("⬅️","m:bosh")]]))
    await xavfsiz_tahrir(cb,
        f"🔗 Guruh linkini yuboring:\n"
        f"<code>t.me/guruh</code> yoki <code>t.me/+InviteKod</code>\n\n"
        f"🟢 Bo'sh: {len(bosh)} ta")
    await state.set_state(GuruhQoshilish.link)

@dp.message(GuruhQoshilish.link)
async def guruh_link_qabul(msg: Message, state: FSMContext):
    link = msg.text.strip()
    if not link_parse(link):
        return await msg.answer("❌ Link noto'g'ri. Format: <code>t.me/guruh</code> yoki <code>t.me/+Kod</code>", parse_mode="HTML")
    await state.clear()
    bosh = await db.get_accounts_by_status("idle")
    n    = len(bosh)
    await msg.answer(f"Nechta akkunt qo'shilsin?\n🟢 Bo'sh: {n} ta", reply_markup=ikb([
        [("👥 2 ta",f"gq:2|{link}"),("👥 4 ta",f"gq:4|{link}")],
        [("👥 Hammasi ({})".format(n),f"gq:A|{link}")],
        [("❌ Bekor","m:bosh")],
    ]))

@dp.callback_query(F.data.startswith("gq:"))
async def guruh_qoshilish_cb(cb: CallbackQuery):
    cnt, link = cb.data[3:].split("|", 1)
    bosh = await db.get_accounts_by_status("idle")
    if not bosh: return await cb.answer("Bo'sh akkunt yo'q")
    n         = len(bosh) if cnt == "A" else min(int(cnt), len(bosh))
    tanlangan = random.sample(bosh, n)
    await xavfsiz_tahrir(cb, f"⏳ {n} ta akkunt guruhga qo'shilmoqda...")
    asyncio.create_task(_guruh_qoshish_task(cb.message, tanlangan, link))

async def _guruh_qoshish_task(msg, akkauntlar, link, amal="guruh_qoshish"):
    ok = xato = 0
    xato_sabablari = []
    for akk in akkauntlar:
        nom    = akk["display_name"] or akk["phone"]
        akk_id = akk["id"]
        client = await _get_client(akk_id)
        if not client:
            xato += 1
            xato_sabablari.append(f"• {nom}: session yo'q")
            await asyncio.sleep(3)
            continue
        try:
            await guruhga_qoshil(client, link)
            await db.update_account_status(akk_id, "idle")
            await db.add_log(akk_id, amal, link)
            ok += 1
        except UserAlreadyParticipantError:
            ok += 1
        except FloodWaitError as e:
            await asyncio.sleep(min(e.seconds, 60))
            xato_sabablari.append(f"• {nom}: FloodWait")
            xato += 1
        except Exception as e:
            xato_sabablari.append(f"• {nom}: {str(e)[:50]}")
            log.error(f"guruh xato akk={akk_id}: {e}")
            xato += 1
        await asyncio.sleep(random.randint(3, 8))
    natija = f"✅ Qo'shildi: {ok}  ❌ Xato: {xato}"
    if xato_sabablari:
        natija += "\n\n" + "\n".join(xato_sabablari[:5])
    try:
        await msg.answer(natija, reply_markup=asosiy_menyu(), parse_mode="HTML")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Monitoring
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "m:monitor")
async def menu_monitor(cb: CallbackQuery):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    guruhlar   = await db.get_monitored_groups()
    akkauntlar = await db.get_all_accounts()
    bosh = sum(1 for a in akkauntlar if a["status"]=="idle")
    band = sum(1 for a in akkauntlar if a["status"]=="busy")
    matn = "👁 <b>Monitoring</b>\n\n<b>Guruhlar:</b>\n"
    matn += ("\n".join(f"• {g['group_name']} (<code>{g['group_id']}</code>)" for g in guruhlar)
             if guruhlar else "Guruh yo'q.")
    matn += f"\n\n🟢 Bo'sh: {bosh}  🔴 Band: {band}"
    await xavfsiz_tahrir(cb, matn, ikb([[("⬅️ Orqaga","m:bosh")]]))

def _faollik_tekshir(guruh_id):
    chegara  = datetime.utcnow() - timedelta(seconds=config.ACTIVITY_WINDOW_SEC)
    xabarlar = [t for t in group_activity[guruh_id] if t > chegara]
    group_activity[guruh_id] = xabarlar
    return len(xabarlar) >= config.ACTIVITY_THRESHOLD

async def _ogohlantirish_yuborish(guruh_id, guruh_nomi):
    bosh = await db.get_accounts_by_status("idle")
    n    = len(bosh)
    await adminlarga_xabar(
        f"🔔 <b>Guruhda faollik!</b>\n📍 <b>{guruh_nomi}</b>\n🟢 Bo'sh: {n} ta\n\nNima?",
        ikb([
            [("➕ 2 ta","ogl:2|{}".format(guruh_id)),("➕ 4 ta","ogl:4|{}".format(guruh_id))],
            [("➕ Hammasi ({})".format(n),"ogl:A|{}".format(guruh_id))],
            [("🖐 Qo'lda","ogl:Q|{}".format(guruh_id))],
            [("❌ E'tibor berma","ogl:yoq")],
        ])
    )

@dp.callback_query(F.data.startswith("ogl:"))
async def ogohlantirish_amal(cb: CallbackQuery):
    qiymat = cb.data[4:]
    if qiymat == "yoq":
        return await xavfsiz_tahrir(cb, "✅ E'tiborsiz qoldirildi.")
    cnt, gid_str = qiymat.split("|", 1)
    guruh_id     = int(gid_str)
    bosh         = await db.get_accounts_by_status("idle")
    if not bosh: return await cb.answer("Bo'sh akkunt yo'q")
    if cnt == "Q":
        qatorlar = [[(f"🟢 {a['display_name'] or a['phone']}",f"qt:{a['id']}|{guruh_id}")] for a in bosh]
        qatorlar.append([("✅ Boshlash",f"qb:{guruh_id}"),("❌ Bekor","ogl:yoq")])
        return await xavfsiz_tahrir(cb, "Akkuntlarni tanlang:", ikb(qatorlar))
    pool  = await db.get_pool()
    guruh = await pool.fetchrow("SELECT link FROM monitored_groups WHERE group_id=$1", guruh_id)
    link  = guruh["link"] if guruh else str(guruh_id)
    n     = len(bosh) if cnt == "A" else min(int(cnt), len(bosh))
    tanlangan = random.sample(bosh, n)
    await xavfsiz_tahrir(cb, f"⏳ {n} ta akkunt qo'shilmoqda...")
    asyncio.create_task(_guruh_qoshish_task(cb.message, tanlangan, link, "ogohlantirish_qoshish"))

@dp.callback_query(F.data.startswith("qt:"))
async def qolda_tanlash(cb: CallbackQuery):
    _, qolgan     = cb.data.split(":", 1)
    akk_id_str, _ = qolgan.split("|", 1)
    akk_id        = int(akk_id_str)
    admin_id      = cb.from_user.id
    if akk_id in manual_picks[admin_id]:
        manual_picks[admin_id].remove(akk_id)
        await cb.answer("➖ Olib tashlandi")
    else:
        manual_picks[admin_id].append(akk_id)
        await cb.answer("✅ Tanlandi")

@dp.callback_query(F.data.startswith("qb:"))
async def qolda_boshlash(cb: CallbackQuery):
    guruh_id     = int(cb.data.split(":")[1])
    tanlanganlar = manual_picks.pop(cb.from_user.id, [])
    if not tanlanganlar: return await cb.answer("Hech narsa tanlanmadi")
    pool  = await db.get_pool()
    guruh = await pool.fetchrow("SELECT link FROM monitored_groups WHERE group_id=$1", guruh_id)
    link  = guruh["link"] if guruh else str(guruh_id)
    akkauntlar = [a for aid in tanlanganlar if (a := await db.get_account(aid))]
    await xavfsiz_tahrir(cb, f"⏳ {len(akkauntlar)} ta akkunt qo'shilmoqda...")
    asyncio.create_task(_guruh_qoshish_task(cb.message, akkauntlar, link, "qolda_qoshish"))


# ═══════════════════════════════════════════════════════════════════════════════
# Avto xabar
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "m:avto")
async def menu_avto(cb: CallbackQuery):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    sozlar   = await db.get_all_words()
    ishlaydi = sum(1 for t in auto_tasks.values() if not t.done())
    qatorlar = [f"{s['id']}. {s['word']} {'✅' if s['is_active'] else '❌'}" for s in sozlar]
    matn     = f"🗣 <b>Avto xabarlar</b> (ishlaydi: {ishlaydi})\n\n"
    matn    += "\n".join(qatorlar) if qatorlar else "So'z yo'q."
    await xavfsiz_tahrir(cb, matn, ikb([
        [("➕ So'z qo'sh","sx:qosh"),("🗑 O'chir","sx:ochir")],
        [("▶️ Boshlash","avto:bosh"),("⏹ To'xtatish","avto:toxt")],
        [("⬅️ Orqaga","m:bosh")],
    ]))

@dp.callback_query(F.data == "sx:qosh")
async def soz_qosh(cb: CallbackQuery, state: FSMContext):
    await xavfsiz_tahrir(cb, "Yangi so'z kiriting:")
    await state.set_state(SozQoshish.soz)

@dp.message(SozQoshish.soz)
async def soz_saqlash(msg: Message, state: FSMContext):
    await db.add_word(msg.text.strip(), msg.from_user.id)
    await state.clear()
    await msg.answer("✅ So'z qo'shildi", reply_markup=asosiy_menyu())

@dp.callback_query(F.data == "sx:ochir")
async def soz_ochir_royxat(cb: CallbackQuery):
    sozlar = await db.get_all_words()
    if not sozlar: return await cb.answer("So'z yo'q")
    qatorlar = [[(f"🗑 {s['word']}",f"sd:{s['id']}")] for s in sozlar]
    qatorlar.append([("⬅️ Orqaga","m:avto")])
    await xavfsiz_tahrir(cb, "O'chirmoqchi bo'lgan so'zni tanlang:", ikb(qatorlar))

@dp.callback_query(F.data.startswith("sd:"))
async def soz_ochir(cb: CallbackQuery):
    await db.delete_word(int(cb.data.split(":")[1]))
    await cb.answer("🗑 O'chirildi")
    cb.data = "sx:ochir"
    await soz_ochir_royxat(cb)

@dp.callback_query(F.data == "avto:bosh")
async def avto_boshlash(cb: CallbackQuery):
    akkauntlar = await db.get_all_accounts()
    aktiv = [a for a in akkauntlar if a["status"] in ("idle","busy")]
    if not aktiv: return await cb.answer("Aktiv akkunt yo'q")
    boshlandi = 0
    for akk in aktiv:
        if akk["id"] not in auto_tasks or auto_tasks[akk["id"]].done():
            auto_tasks[akk["id"]] = asyncio.create_task(_avto_tsikl(akk["id"]))
            boshlandi += 1
    await cb.answer(f"▶️ {boshlandi} ta boshlandi")

@dp.callback_query(F.data == "avto:toxt")
async def avto_toxtatish(cb: CallbackQuery):
    for t in auto_tasks.values(): t.cancel()
    auto_tasks.clear()
    await cb.answer("⏹ To'xtatildi")

async def _avto_tsikl(akk_id):
    oxirgi = None
    while True:
        try:
            sozlar = [s["word"] for s in await db.get_active_words()]
            if not sozlar:
                await asyncio.sleep(30)
                continue
            tanlovlar = [s for s in sozlar if s != oxirgi] or sozlar
            soz       = random.choice(tanlovlar)
            oxirgi    = soz
            client    = await _get_client(akk_id)
            if client and client.is_connected():
                for g in await db.get_monitored_groups():
                    try:
                        await client.send_message(g["group_id"], soz)
                        await db.add_log(akk_id, "avto_xabar", f"{g['group_name']}: {soz}")
                    except Exception as e:
                        log.warning(f"avto_xabar akk={akk_id}: {e}")
            await asyncio.sleep(random.randint(30, 120))
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"avto_tsikl akk={akk_id}: {e}")
            await asyncio.sleep(60)


# ═══════════════════════════════════════════════════════════════════════════════
# Video Chat
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "m:vc")
async def vc_menu(cb: CallbackQuery, state: FSMContext):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    bosh = await db.get_accounts_by_status("idle")
    if not bosh:
        return await xavfsiz_tahrir(cb, "❌ Bo'sh akkunt yo'q.", ikb([[("⬅️","m:bosh")]]))
    await xavfsiz_tahrir(cb,
        f"🎥 <b>Video Chat</b>\n\n"
        f"🟢 Bo'sh: {len(bosh)} ta\n\n"
        f"Video chat linkini yuboring:\n"
        f"<code>t.me/username?videochat</code>\n"
        f"<code>t.me/username?videochat=HASH</code>\n"
        f"<code>t.me/+InviteKod?videochat</code>"
    )
    await state.set_state(VideoChat.link)

@dp.message(VideoChat.link)
async def vc_link_qabul(msg: Message, state: FSMContext):
    link = msg.text.strip()
    if not link_parse(link):
        return await msg.answer(
            "❌ Link to'g'ri emas.\nFormat: <code>t.me/username?videochat</code>",
            parse_mode="HTML"
        )
    bosh = await db.get_accounts_by_status("idle")
    n    = len(bosh)
    await state.update_data(vc_link=link)
    await state.set_state(VideoChat.son)
    await msg.answer(
        f"✅ Link qabul qilindi\n🟢 Bo'sh: {n} ta\n\nNechta akkunt kirsin?",
        parse_mode="HTML",
        reply_markup=ikb([
            [("🎥 2 ta","vcn:2"),("🎥 4 ta","vcn:4"),("🎥 6 ta","vcn:6")],
            [("🎥 Hammasi ({})".format(n),"vcn:A")],
            [("❌ Bekor","vcn:bekor")],
        ])
    )

@dp.message(VideoChat.son)
async def vc_son_qabul(msg: Message, state: FSMContext):
    matn = msg.text.strip()
    if not matn.isdigit() or int(matn) < 1:
        return await msg.answer("❌ Faqat musbat son kiriting.")
    malumot = await state.get_data()
    await state.clear()
    bosh = await db.get_accounts_by_status("idle")
    if not bosh:
        return await msg.answer("❌ Bo'sh akkunt yo'q.", reply_markup=asosiy_menyu())
    n         = min(int(matn), len(bosh))
    tanlangan = random.sample(bosh, n)
    await msg.answer(f"⏳ {n} ta akkunt video chatga qo'shilmoqda...", reply_markup=asosiy_menyu())
    asyncio.create_task(_vc_task(msg, tanlangan, malumot["vc_link"]))

@dp.callback_query(F.data.startswith("vcn:"))
async def vc_son_tugma(cb: CallbackQuery, state: FSMContext):
    cnt     = cb.data[4:]
    malumot = await state.get_data()
    await state.clear()
    if cnt == "bekor":
        return await xavfsiz_tahrir(cb, "❌ Bekor qilindi.", asosiy_menyu())
    if not malumot.get("vc_link"):
        return await xavfsiz_tahrir(cb, "❌ Link topilmadi.", asosiy_menyu())
    bosh = await db.get_accounts_by_status("idle")
    if not bosh:
        return await xavfsiz_tahrir(cb, "❌ Bo'sh akkunt yo'q.", asosiy_menyu())
    n         = len(bosh) if cnt == "A" else min(int(cnt), len(bosh))
    tanlangan = random.sample(bosh, n)
    await xavfsiz_tahrir(cb, f"⏳ {n} ta akkunt video chatga qo'shilmoqda...")
    asyncio.create_task(_vc_task(cb.message, tanlangan, malumot["vc_link"]))

@dp.callback_query(F.data.startswith("vcal:"))
async def vcal_amal(cb: CallbackQuery):
    qiymat = cb.data[5:]
    if qiymat == "yoq":
        return await xavfsiz_tahrir(cb, "✅ E'tiborsiz qoldirildi.")
    cnt, chat_id_str = qiymat.split("|", 1)
    chat_id = int(chat_id_str)
    bosh    = await db.get_accounts_by_status("idle")
    if not bosh: return await cb.answer("Bo'sh akkunt yo'q")
    n         = len(bosh) if cnt == "A" else min(int(cnt), len(bosh))
    tanlangan = random.sample(bosh, n)
    await xavfsiz_tahrir(cb, f"⏳ {n} ta akkunt video chatga qo'shilmoqda...")
    asyncio.create_task(_vc_task_by_id(cb.message, tanlangan, chat_id))


async def _vc_join_one(client: TelegramClient, entity):
    """Bir akkuntni video chatga qo'shadi, call obyektini qaytaradi."""
    full_info = await client(GetFullChannelRequest(entity))
    full_chat = full_info.full_chat
    if not (hasattr(full_chat, "call") and full_chat.call):
        raise RuntimeError("Aktiv video chat topilmadi")
    call   = full_chat.call
    ssrc   = random.randint(100_000_000, 999_999_999)
    params = DataJSON(data=json.dumps({
        "ufrag": f"uf{ssrc}", "pwd": f"pw{ssrc}",
        "fingerprints": [{"hash":"sha-256","fingerprint":"AA"*32}],
        "ssrc": ssrc, "ssrc-groups": []
    }))
    me = await client.get_input_entity("me")
    await client(JoinGroupCallRequest(
        call=call, join_as=me, params=params,
        muted=True, video_stopped=True, invite_hash=None
    ))
    return call


async def _vc_task(msg: Message, akkauntlar: list, link: str):
    """
    Video chatga qo'shilish:
    1. Har akkunt avval guruhga qo'shiladi (link orqali)
    2. Keyin video chatga kiradi
    3. keep_alive task boshlanadi — admin chiq demasa TURMAYDI
    """
    ok = xato = 0
    xato_sabablari = []

    for akk in akkauntlar:
        nom    = akk["display_name"] or akk["phone"]
        akk_id = akk["id"]

        # Client olish
        client = await _get_client(akk_id)
        if not client:
            xato += 1
            xato_sabablari.append(f"• {nom}: session yo'q")
            await asyncio.sleep(2)
            continue

        try:
            # 1. Guruhga qo'shilish (link orqali)
            entity = await guruhga_qoshil(client, link)
            if entity is None:
                raise RuntimeError("Entity topilmadi")

            # 2. Video chatga kirish
            call     = await _vc_join_one(client, entity)
            group_id = entity.id

            # 3. Keep-alive — admin chiq demasa chiqmaydi
            task = asyncio.create_task(
                _vc_keep_alive(akk_id, client, call, group_id, nom)
            )
            vc_ping_tasks[akk_id][group_id] = task

            await db.update_account_status(akk_id, "busy")
            await db.add_log(akk_id, "vc_kirdi", link)
            log.info(f"vc: {nom} ✅ kirdi")
            ok += 1

        except FloodWaitError as e:
            await asyncio.sleep(min(e.seconds, 60))
            xato_sabablari.append(f"• {nom}: FloodWait {e.seconds}s")
            xato += 1
        except Exception as e:
            await db.update_account_status(akk_id, "idle")
            await db.add_log(akk_id, "vc_xato", str(e)[:100], "error")
            xato_sabablari.append(f"• {nom}: {str(e)[:60]}")
            log.error(f"vc_task akk={akk_id}: {type(e).__name__}: {e}")
            xato += 1

        # Har akkunt orasida kichik kutish (spam bo'lmasin)
        if ok + xato < len(akkauntlar):
            await asyncio.sleep(random.randint(2, 4))

    natija = f"🎥 <b>Video chat natija</b>\n✅ Kirdi: {ok}  ❌ Xato: {xato}"
    if xato_sabablari:
        natija += "\n\n<b>Tafsilotlar:</b>\n" + "\n".join(xato_sabablari[:5])
    try:
        await msg.answer(natija, reply_markup=asosiy_menyu(), parse_mode="HTML")
    except Exception:
        pass


async def _vc_task_by_id(msg: Message, akkauntlar: list, chat_id: int):
    """Monitoring orqali — chat_id bo'yicha video chatga qo'shilish."""
    ok = xato = 0
    xato_sabablari = []

    for akk in akkauntlar:
        nom    = akk["display_name"] or akk["phone"]
        akk_id = akk["id"]

        client = await _get_client(akk_id)
        if not client:
            xato += 1
            xato_sabablari.append(f"• {nom}: session yo'q")
            await asyncio.sleep(2)
            continue

        try:
            entity = await client.get_entity(chat_id)
            call   = await _vc_join_one(client, entity)
            task   = asyncio.create_task(
                _vc_keep_alive(akk_id, client, call, chat_id, nom)
            )
            vc_ping_tasks[akk_id][chat_id] = task
            await db.update_account_status(akk_id, "busy")
            await db.add_log(akk_id, "vc_kirdi_auto", str(chat_id))
            ok += 1
        except FloodWaitError as e:
            await asyncio.sleep(min(e.seconds, 60))
            xato_sabablari.append(f"• {nom}: FloodWait {e.seconds}s")
            xato += 1
        except Exception as e:
            xato_sabablari.append(f"• {nom}: {str(e)[:60]}")
            log.error(f"vcal akk={akk_id}: {e}")
            xato += 1

        if ok + xato < len(akkauntlar):
            await asyncio.sleep(random.randint(2, 4))

    natija = f"🎥 <b>Video chat natija</b>\n✅ Kirdi: {ok}  ❌ Xato: {xato}"
    if xato_sabablari:
        natija += "\n" + "\n".join(xato_sabablari[:5])
    try:
        await msg.answer(natija, reply_markup=asosiy_menyu(), parse_mode="HTML")
    except Exception:
        pass


async def _vc_keep_alive(akk_id: int, client: TelegramClient, call, chat_id: int, nom: str):
    """
    Akkuntni video chatda USHLAB TURADI.
    FAQAT task.cancel() kelganda chiqadi.
    finally yo'q — u har doim ishlaydi va akkuntni chiqaradi.
    """
    log.info(f"vc_keep_alive: {nom} boshlandi")
    admin_chiqardi = False

    while True:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            admin_chiqardi = True
            break

        # Har 30 sekundda oddiy ping — ulanishni saqlash
        try:
            if not client.is_connected():
                await client.connect()
            await client.get_me()
        except asyncio.CancelledError:
            admin_chiqardi = True
            break
        except Exception as e:
            # Xato bo'lsa ham DAVOM ETAMIZ — chiqmaymiz
            log.warning(f"vc ping {nom}: {e}")

    # Faqat admin chiqargan bo'lsa Leave yuboramiz
    if admin_chiqardi:
        try:
            await client(LeaveGroupCallRequest(call=call, source=0))
            log.info(f"vc: {nom} chiqdi (admin buyrug'i)")
        except Exception as e:
            log.warning(f"vc Leave xato {nom}: {e}")

    await db.update_account_status(akk_id, "idle")
    if akk_id in vc_ping_tasks:
        vc_ping_tasks[akk_id].pop(chat_id, None)
    await db.add_log(akk_id, "vc_chiqdi", "admin" if admin_chiqardi else "kutilmagan")


# ═══════════════════════════════════════════════════════════════════════════════
# Sozlamalar
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "m:sozlamalar")
async def menu_sozlamalar(cb: CallbackQuery):
    if not await bosh_admin_mi(cb.from_user.id): return await cb.answer("Faqat bosh admin")
    await xavfsiz_tahrir(cb, "⚙️ <b>Sozlamalar</b>", ikb([
        [("➕ Admin qo'sh","adm:qosh"),("🗑 Admin o'chir","adm:ochir")],
        [("👥 Adminlar ro'yxati","adm:royxat")],
        [("⬅️ Orqaga","m:bosh")],
    ]))

@dp.callback_query(F.data == "adm:qosh")
async def adm_qosh(cb: CallbackQuery, state: FSMContext):
    await xavfsiz_tahrir(cb, "Admin Telegram ID sini kiriting:")
    await state.set_state(AdminQoshish.id)

@dp.message(AdminQoshish.id)
async def adm_saqlash(msg: Message, state: FSMContext):
    try:
        tid = int(msg.text.strip())
        await db.add_admin(tid, "", "admin")
        await state.clear()
        await msg.answer(f"✅ Admin qo'shildi: <code>{tid}</code>", reply_markup=asosiy_menyu(), parse_mode="HTML")
    except ValueError:
        await msg.answer("❌ Noto'g'ri ID.")

@dp.callback_query(F.data == "adm:royxat")
async def adm_royxat(cb: CallbackQuery):
    adminlar = await db.get_all_admins()
    qatorlar = [f"• <code>{a['telegram_id']}</code> {a['username'] or ''} ({a['role']})" for a in adminlar]
    await xavfsiz_tahrir(cb, "👥 <b>Adminlar:</b>\n" + "\n".join(qatorlar), ikb([[("⬅️ Orqaga","m:sozlamalar")]]))

@dp.callback_query(F.data == "adm:ochir")
async def adm_ochir_royxat(cb: CallbackQuery):
    adminlar = await db.get_all_admins()
    qatorlar = [[(f"🗑 {a['username'] or a['telegram_id']}",f"rmadm:{a['telegram_id']}")] for a in adminlar if a["role"] != "main_admin"]
    if not qatorlar: return await cb.answer("O'chirish uchun admin yo'q")
    qatorlar.append([("⬅️ Orqaga","m:sozlamalar")])
    await xavfsiz_tahrir(cb, "O'chirmoqchi bo'lgan adminni tanlang:", ikb(qatorlar))

@dp.callback_query(F.data.startswith("rmadm:"))
async def adm_ochir(cb: CallbackQuery):
    await db.remove_admin(int(cb.data.split(":")[1]))
    await cb.answer("🗑 O'chirildi")
    cb.data = "adm:ochir"
    await adm_ochir_royxat(cb)


# ═══════════════════════════════════════════════════════════════════════════════
# Telethon handlers
# ═══════════════════════════════════════════════════════════════════════════════

async def _handlerlarni_qoshish(akk_id: int, client: TelegramClient):
    @client.on(events.NewMessage())
    async def xabar_keldi(event):
        if not (event.is_group or event.is_channel): return
        gid = event.chat_id
        group_activity[gid].append(datetime.utcnow())
        if _faollik_tekshir(gid):
            group_activity[gid].clear()
            for g in await db.get_monitored_groups():
                if g["group_id"] == gid:
                    await _ogohlantirish_yuborish(gid, g["group_name"])
                    break

    @client.on(events.Raw(UpdateGroupCall))
    async def vc_boshlandi(event):
        try:
            call    = event.call
            chat_id = getattr(event, "chat_id", None)
            if chat_id is None: return
            neg_chat_id = int(f"-100{chat_id}") if chat_id > 0 else chat_id
            guruhlar = await db.get_monitored_groups()
            guruh = next((g for g in guruhlar if g["group_id"] in (chat_id, neg_chat_id)), None)
            if not guruh or not isinstance(call, GroupCall): return
            if getattr(call, "discarded", False) or getattr(call, "schedule_date", None): return
            bosh = await db.get_accounts_by_status("idle")
            n    = len(bosh)
            await adminlarga_xabar(
                f"🎥 <b>Video chat boshlandi!</b>\n📍 <b>{guruh['group_name']}</b>\n🟢 Bo'sh: {n} ta\n\nQo'shish?",
                ikb([
                    [("🎥 2 ta",f"vcal:2|{neg_chat_id}"),("🎥 4 ta",f"vcal:4|{neg_chat_id}")],
                    [("🎥 Hammasi ({})".format(n),f"vcal:A|{neg_chat_id}")],
                    [("❌ Kerak emas","vcal:yoq")],
                ])
            )
        except Exception as e:
            log.error(f"vc_boshlandi xato: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Ishga tushirish
# ═══════════════════════════════════════════════════════════════════════════════

async def _barcha_clientlarni_yukla():
    akkauntlar = await db.get_all_accounts()
    log.info(f"Jami {len(akkauntlar)} ta akkunt topildi")

    # Session bor akkuntlarni ajratib olamiz
    yuklanadi = [
        akk for akk in akkauntlar
        if akk["status"] not in ("paused", "failed")
        and (akk.get("session_string") or "").strip()
    ]
    yoq = len(akkauntlar) - len(yuklanadi)
    log.info(f"Session bor: {len(yuklanadi)}, yo'q: {yoq}")

    # Parallel yuklash (10 ta bir vaqtda)
    sem = asyncio.Semaphore(10)
    async def _yukla_bitta(akk):
        async with sem:
            c = await _get_client(akk["id"])
            if c:
                log.info(f"[yukla] ✅ akk={akk['id']} ({akk['phone']})")
            else:
                log.error(f"[yukla] ❌ akk={akk['id']} ({akk['phone']})")

    await asyncio.gather(*[_yukla_bitta(akk) for akk in yuklanadi])
    log.info(f"Yuklandi: {len(clients)} / {len(akkauntlar)} ta client")

async def _qayta_ulanish_tsikl():
    while True:
        await asyncio.sleep(60)
        for akk in await db.get_all_accounts():
            if akk["status"] in ("paused","failed"): continue
            c = clients.get(akk["id"])
            if c is None:
                if (akk.get("session_string") or "").strip():
                    await _get_client(akk["id"])
            elif not c.is_connected():
                try:
                    await c.connect()
                except Exception as e:
                    log.error(f"[qayta] akk={akk['id']}: {e}")
                    await db.update_account_status(akk["id"], "failed")

async def ishga_tushish():
    await db.init_db()
    await db.add_admin(config.MAIN_ADMIN_ID, "main_admin", "main_admin")
    await _barcha_clientlarni_yukla()
    asyncio.create_task(_qayta_ulanish_tsikl())
    log.info("Bot ishga tushdi ✅")

async def toxtatish():
    for c in clients.values():
        try: await c.disconnect()
        except: pass
    pool = await db.get_pool()
    await pool.close()

async def main():
    dp.startup.register(ishga_tushish)
    dp.shutdown.register(toxtatish)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
