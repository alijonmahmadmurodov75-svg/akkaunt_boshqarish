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

# Global: akk_id -> TelegramClient
clients: dict[int, TelegramClient]  = {}
group_activity: dict[int, list]     = defaultdict(list)
auto_tasks: dict[int, asyncio.Task] = {}
manual_picks: dict[int, list]       = defaultdict(list)
# akk_id -> {chat_id -> asyncio.Task}
vc_ping_tasks: dict[int, dict]      = defaultdict(dict)


# ═══════════════════════════════════════════════════════════════════════════════
# FSM holatlari
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


# ═══════════════════════════════════════════════════════════════════════════════
# Yordamchi funksiyalar
# ═══════════════════════════════════════════════════════════════════════════════

def ikb(rows: list[list[tuple]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
        for row in rows
    ])

async def admin_mi(uid: int) -> bool:
    return bool(await db.get_admin(uid))

async def bosh_admin_mi(uid: int) -> bool:
    a = await db.get_admin(uid)
    return bool(a and a["role"] == "main_admin")

async def adminlarga_xabar(matn: str, markup=None):
    for a in await db.get_all_admins():
        try:
            await bot.send_message(
                a["telegram_id"], matn,
                reply_markup=markup, parse_mode="HTML"
            )
        except Exception:
            pass

def holat_belgisi(h: str) -> str:
    return {"idle": "🟢", "busy": "🔴", "paused": "⏸", "failed": "❌"}.get(h, "❓")

def holat_nomi(h: str) -> str:
    return {"idle": "Bo'sh", "busy": "Band", "paused": "Pauza", "failed": "Xato"}.get(h, h)

def asosiy_menyu() -> InlineKeyboardMarkup:
    return ikb([
        [("👤 Akkauntlar", "m:akkauntlar"), ("➕ Qo'shish",      "m:qoshish")],
        [("📊 Holat",      "m:holat"),      ("👥 Guruhga qo'sh", "m:guruh")],
        [("🗣 Avto xabar", "m:avto"),       ("🎥 Video chat",    "m:vc")],
        [("👁 Monitoring", "m:monitor"),    ("⚙️ Sozlamalar",    "m:sozlamalar")],
    ])

async def xavfsiz_tahrir(cb: CallbackQuery, matn: str, markup=None):
    try:
        await cb.message.edit_text(matn, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        await cb.message.answer(matn, reply_markup=markup, parse_mode="HTML")

def join_delay() -> int:
    mn = getattr(config, "JOIN_DELAY_MIN", 8)
    mx = getattr(config, "JOIN_DELAY_MAX", 20)
    return random.randint(mn, mx)


# ═══════════════════════════════════════════════════════════════════════════════
# Client olish — ENG MUHIM funksiya
# ═══════════════════════════════════════════════════════════════════════════════

async def _get_client(akk_id: int) -> TelegramClient | None:
    """
    clients dict dan client oladi.
    Yo'q bo'lsa — database dan akkunt ma'lumotlarini yuklab, yangi client yaratadi.
    Barcha xatolarni log qiladi.
    """
    # 1. Avval dict dan tekshir
    c = clients.get(akk_id)
    if c:
        if not c.is_connected():
            try:
                await c.connect()
                log.info(f"[get_client] akk={akk_id} qayta ulandi")
            except Exception as e:
                log.error(f"[get_client] akk={akk_id} qayta ulanish xato: {e}")
                del clients[akk_id]
                c = None
        if c:
            return c

    # 2. Database dan akkunt ma'lumotlarini ol
    akk = await db.get_account(akk_id)
    if not akk:
        log.error(f"[get_client] akk={akk_id} database da topilmadi")
        return None

    session_str = akk.get("session_string") or ""
    if not session_str.strip():
        log.error(f"[get_client] akk={akk_id} ({akk.get('phone')}) session_string bo'sh!")
        return None

    # 3. Yangi client yarat
    try:
        log.info(f"[get_client] akk={akk_id} ({akk.get('phone')}) yangi client yaratilmoqda...")
        c = TelegramClient(
            StringSession(session_str),
            config.API_ID,
            config.API_HASH,
            connection_retries=3,
            retry_delay=2,
        )
        await c.connect()

        if not await c.is_user_authorized():
            log.error(f"[get_client] akk={akk_id} avtorizatsiya yo'q — sessiya muddati o'tgan")
            await db.update_account_status(akk_id, "failed")
            await adminlarga_xabar(f"❌ Sessiya muddati o'tgan: {akk.get('phone')}")
            return None

        me = await c.get_me()
        log.info(f"[get_client] akk={akk_id} ✅ ulandi: @{me.username or me.id}")
        clients[akk_id] = c
        await _handlerlarni_qoshish(akk_id, c)
        return c

    except Exception as e:
        log.error(f"[get_client] akk={akk_id} ({akk.get('phone')}) XATO: {type(e).__name__}: {e}")
        await db.update_account_status(akk_id, "failed")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Link tahlil
# ═══════════════════════════════════════════════════════════════════════════════

def link_parse(link: str) -> dict | None:
    """
    Guruh/kanal linkini ajratadi.
    Qaytaradi: {"type": "invite"|"username", "value": str, "vc_hash": str}
    """
    link = link.strip()
    # Invite: t.me/+XXX yoki t.me/joinchat/XXX
    inv = re.search(r"t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)", link)
    if inv:
        hm = re.search(r"(?:videochat|voicechat|livestream)=?([^&\s]*)", link)
        return {
            "type": "invite",
            "value": inv.group(1),
            "vc_hash": hm.group(1) if hm else ""
        }
    # Username: t.me/username yoki @username
    usr = re.search(r"(?:t\.me/|@)([A-Za-z0-9_]{3,})", link)
    if usr:
        hm = re.search(r"(?:videochat|voicechat|livestream)=?([^&\s]*)", link)
        return {
            "type": "username",
            "value": usr.group(1),
            "vc_hash": hm.group(1) if hm else ""
        }
    return None


async def guruhga_qoshil(client: TelegramClient, link: str):
    """
    Guruh/kanalga qo'shiladi. Invite va username linkni qo'llab-quvvatlaydi.
    Qaytaradi: entity
    """
    info = link_parse(link)
    if not info:
        raise ValueError(f"Noto'g'ri link format: {link}")

    if info["type"] == "invite":
        try:
            result = await client(ImportChatInviteRequest(info["value"]))
            chats = getattr(result, "chats", [])
            return chats[0] if chats else None
        except UserAlreadyParticipantError:
            # Allaqachon a'zo — entity ni qaytaramiz
            try:
                return await client.get_entity(f"t.me/{info['value']}")
            except Exception:
                return None
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
        return await xavfsiz_tahrir(cb, "Hech qanday akkunt yo'q.", ikb([[("⬅️ Orqaga", "m:bosh")]]))
    qatorlar = [[(
        f"{holat_belgisi(a['status'])} {a['display_name'] or a['phone']} — {holat_nomi(a['status'])}",
        f"akk:{a['id']}"
    )] for a in akkauntlar]
    qatorlar.append([("➕ Qo'shish", "m:qoshish"), ("⬅️ Orqaga", "m:bosh")])
    await xavfsiz_tahrir(cb, "📋 <b>Akkauntlar:</b>", ikb(qatorlar))

@dp.callback_query(F.data.startswith("akk:"))
async def akk_detail(cb: CallbackQuery):
    akk = await db.get_account(int(cb.data.split(":")[1]))
    if not akk: return await cb.answer("Topilmadi")
    # session bor-yo'qligini tekshir
    has_session = bool(akk.get("session_string", "").strip())
    oxirgi = akk["last_active"].strftime("%d.%m %H:%M") if akk["last_active"] else "—"
    matn = (
        f"{holat_belgisi(akk['status'])} <b>{akk['display_name'] or akk['phone']}</b>\n"
        f"📱 <code>{akk['phone']}</code>\n"
        f"👤 @{akk['username'] or '—'}\n"
        f"💾 Sessiya: {'✅' if has_session else '❌ YO\'Q'}\n"
        f"🔵 Holat: <b>{holat_nomi(akk['status'])}</b>\n"
        f"🕐 Oxirgi faollik: {oxirgi}"
    )
    aid = akk["id"]
    kb_rows = [
        [("⏸ Pauza", f"hs:{aid}:paused"), ("▶️ Bo'shatish", f"hs:{aid}:idle")],
        [("🗑 O'chirish", f"ochir:{aid}")],
        [("⬅️ Orqaga", "m:akkauntlar")],
    ]
    await xavfsiz_tahrir(cb, matn, ikb(kb_rows))

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
        return await xavfsiz_tahrir(cb, "Akkunt yo'q.", ikb([[("⬅️", "m:bosh")]]))
    bosh  = sum(1 for a in akkauntlar if a["status"] == "idle")
    band  = sum(1 for a in akkauntlar if a["status"] == "busy")
    pauza = sum(1 for a in akkauntlar if a["status"] == "paused")
    xato  = sum(1 for a in akkauntlar if a["status"] == "failed")
    # clients dict bilan solishtir
    ulangan = len(clients)
    qatorlar = []
    for a in akkauntlar:
        has_client = "🔗" if a["id"] in clients else "⚠️"
        has_session = "💾" if a.get("session_string") else "❌"
        qatorlar.append(
            f"{holat_belgisi(a['status'])} {has_client}{has_session} "
            f"<b>{a['display_name'] or a['phone']}</b> — {holat_nomi(a['status'])}"
        )
    matn = (
        f"📊 <b>Akkunt holati</b>\n"
        f"🟢 Bo'sh: {bosh}  🔴 Band: {band}  ⏸ Pauza: {pauza}  ❌ Xato: {xato}\n"
        f"🔗 Ulangan clientlar: {ulangan}\n\n"
        + "\n".join(qatorlar)
        + "\n\n<i>🔗 = ulangan, ⚠️ = ulanmagan, 💾 = sessiya bor, ❌ = sessiya yo'q</i>"
    )
    await xavfsiz_tahrir(cb, matn, ikb([[("🔄 Yangilash", "m:holat"), ("⬅️ Orqaga", "m:bosh")]]))


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
        await state.update_data(
            telefon=telefon, akk_id=akk_id,
            hash=natija.phone_code_hash
        )
        await state.set_state(AkkuntQoshish.kod)
        await msg.answer("📨 SMS kod yuborildi. Kodni kiriting:")
    except FloodWaitError as e:
        await db.delete_account(akk_id)
        await client.disconnect()
        await state.clear()
        daqiqa = e.seconds // 60
        soat   = daqiqa // 60
        vaqt   = f"{soat} soat {daqiqa % 60} daqiqa" if soat > 0 else f"{daqiqa} daqiqa"
        await msg.answer(
            f"⏳ <b>Telegram cheklash qo'ydi</b>\n\n"
            f"Bu raqamga juda ko'p kod yuborildi.\n"
            f"<b>{vaqt}</b> kutib, qaytadan urinib ko'ring.",
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
        await client.sign_in(
            malumot["telefon"], msg.text.strip(),
            phone_code_hash=malumot["hash"]
        )
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
    # Sessiyani saqlash
    await db.update_account_session(akk_id, malumot["sessiya"], malumot.get("username"))
    pool = await db.get_pool()
    await pool.execute(
        "UPDATE accounts SET display_name=$1 WHERE id=$2", nom, akk_id
    )
    await state.clear()
    # Client handler qo'shish
    client = clients.get(akk_id)
    if client:
        await _handlerlarni_qoshish(akk_id, client)
        log.info(f"[nom_qabul] akk={akk_id} ({nom}) handler qo'shildi")
    else:
        log.warning(f"[nom_qabul] akk={akk_id} clients dict da topilmadi!")
    await msg.answer(
        f"✅ Akkunt qo'shildi: <b>{nom}</b>",
        reply_markup=asosiy_menyu(), parse_mode="HTML"
    )

async def _sessiya_saqlash(msg, state, akk_id, client):
    men      = await client.get_me()
    ses      = client.session.save()
    avto_nom = men.first_name or men.username or str(men.id)
    log.info(f"[sessiya_saqlash] akk={akk_id}, session len={len(ses)}, username=@{men.username}")
    await state.update_data(sessiya=ses, username=men.username, avto_nom=avto_nom)
    await state.set_state(AkkuntQoshish.nom)
    await msg.answer(
        f"✅ Kirish muvaffaqiyatli!\n"
        f"👤 @{men.username or '—'} | {men.first_name or ''}\n\n"
        f"Akkunt uchun nom kiriting\n"
        f"(bo'sh qoldirsangiz <b>{avto_nom}</b> ishlatiladi):",
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
        return await xavfsiz_tahrir(cb, "❌ Bo'sh akkunt yo'q.", ikb([[("⬅️", "m:bosh")]]))
    await xavfsiz_tahrir(
        cb,
        f"🔗 Guruh linkini yuboring:\n\n"
        f"<b>Username:</b> <code>t.me/guruh_nomi</code>\n"
        f"<b>Invite:</b> <code>t.me/+AbCdEfGh</code>\n\n"
        f"🟢 Bo'sh: {len(bosh)} ta"
    )
    await state.set_state(GuruhQoshilish.link)

@dp.message(GuruhQoshilish.link)
async def guruh_link_qabul(msg: Message, state: FSMContext):
    link = msg.text.strip()
    if not link_parse(link):
        return await msg.answer(
            "❌ Link noto'g'ri.\n\n"
            "To'g'ri formatlar:\n"
            "<code>t.me/guruh_nomi</code>\n"
            "<code>t.me/+InviteKod</code>",
            parse_mode="HTML"
        )
    await state.clear()
    bosh = await db.get_accounts_by_status("idle")
    n    = len(bosh)
    await msg.answer(
        f"Nechta akkunt qo'shilsin?\n🟢 Bo'sh: {n} ta",
        reply_markup=ikb([
            [("👥 2 ta", f"gq:2|{link}"), ("👥 4 ta", f"gq:4|{link}")],
            [("👥 Hammasi ({})".format(n), f"gq:A|{link}")],
            [("❌ Bekor", "m:bosh")],
        ])
    )

@dp.callback_query(F.data.startswith("gq:"))
async def guruh_qoshilish_cb(cb: CallbackQuery):
    cnt, link = cb.data[3:].split("|", 1)
    bosh = await db.get_accounts_by_status("idle")
    if not bosh: return await cb.answer("Bo'sh akkunt yo'q")
    n         = len(bosh) if cnt == "A" else min(int(cnt), len(bosh))
    tanlangan = random.sample(bosh, n)
    await xavfsiz_tahrir(cb, f"⏳ {n} ta akkunt guruhga qo'shilmoqda...")
    asyncio.create_task(_guruh_qoshish_task(cb.message, tanlangan, link))


async def _guruh_qoshish_task(
    msg: Message,
    akkauntlar: list,
    link: str,
    amal: str = "guruh_qoshish"
):
    ok = xato = 0
    xato_sabablari = []

    for akk in akkauntlar:
        nom    = akk["display_name"] or akk["phone"]
        akk_id = akk["id"]

        # ── Client olish ──
        client = await _get_client(akk_id)
        if not client:
            xato += 1
            xato_sabablari.append(f"• {nom}: ulanmadi (session yo'q?)")
            await asyncio.sleep(join_delay())
            continue

        # ── Guruhga qo'shilish ──
        try:
            await guruhga_qoshil(client, link)
            await db.update_account_status(akk_id, "idle")
            await db.add_log(akk_id, amal, link)
            ok += 1
            log.info(f"guruh: {nom} ✅")
        except UserAlreadyParticipantError:
            ok += 1
            log.info(f"guruh: {nom} allaqachon a'zo")
        except FloodWaitError as e:
            wait = min(e.seconds, 60)
            log.warning(f"FloodWait {e.seconds}s akk={akk_id}, {wait}s kutilmoqda")
            xato_sabablari.append(f"• {nom}: FloodWait {e.seconds}s")
            await asyncio.sleep(wait)
            xato += 1
        except InviteHashExpiredError:
            xato_sabablari.append(f"• {nom}: Invite link eskirgan")
            xato += 1
        except (ChatAdminRequiredError, ChannelPrivateError) as e:
            xato_sabablari.append(f"• {nom}: {type(e).__name__}")
            xato += 1
        except Exception as e:
            await db.add_log(akk_id, amal, str(e)[:100], "error")
            log.error(f"guruh xato akk={akk_id}: {type(e).__name__}: {e}")
            xato_sabablari.append(f"• {nom}: {str(e)[:50]}")
            xato += 1

        await asyncio.sleep(join_delay())

    natija = f"✅ Qo'shildi: {ok}  ❌ Xato: {xato}"
    if xato_sabablari:
        natija += "\n\n<b>Xatolar:</b>\n" + "\n".join(xato_sabablari[:5])
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
    bosh = sum(1 for a in akkauntlar if a["status"] == "idle")
    band = sum(1 for a in akkauntlar if a["status"] == "busy")
    matn = "👁 <b>Monitoring</b>\n\n<b>Kuzatilayotgan guruhlar:</b>\n"
    matn += ("\n".join(
        f"• {g['group_name']} (<code>{g['group_id']}</code>)" for g in guruhlar
    ) if guruhlar else "Guruh yo'q.")
    matn += f"\n\n<b>Akkauntlar:</b> 🟢 Bo'sh: {bosh}  🔴 Band: {band}"
    await xavfsiz_tahrir(cb, matn, ikb([[("⬅️ Orqaga", "m:bosh")]]))

def _faollik_tekshir(guruh_id: int) -> bool:
    chegara  = datetime.utcnow() - timedelta(seconds=config.ACTIVITY_WINDOW_SEC)
    xabarlar = [t for t in group_activity[guruh_id] if t > chegara]
    group_activity[guruh_id] = xabarlar
    return len(xabarlar) >= config.ACTIVITY_THRESHOLD

async def _ogohlantirish_yuborish(guruh_id: int, guruh_nomi: str):
    bosh = await db.get_accounts_by_status("idle")
    n    = len(bosh)
    matn = (
        f"🔔 <b>Guruhda faollik!</b>\n"
        f"📍 <b>{guruh_nomi}</b>\n"
        f"🟢 Bo'sh akkauntlar: {n} ta\n\nNima qilish kerak?"
    )
    await adminlarga_xabar(matn, ikb([
        [("➕ 2 ta qo'sh", f"ogl:2|{guruh_id}"), ("➕ 4 ta qo'sh", f"ogl:4|{guruh_id}")],
        [("➕ Hammasi ({})".format(n), f"ogl:A|{guruh_id}")],
        [("🖐 Qo'lda tanlash", f"ogl:Q|{guruh_id}")],
        [("❌ E'tibor berma", "ogl:yoq")],
    ]))

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
        qatorlar = [[(f"🟢 {a['display_name'] or a['phone']}", f"qt:{a['id']}|{guruh_id}")] for a in bosh]
        qatorlar.append([("✅ Boshlash", f"qb:{guruh_id}"), ("❌ Bekor", "ogl:yoq")])
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
    _, qolgan      = cb.data.split(":", 1)
    akk_id_str, _ = qolgan.split("|", 1)
    akk_id         = int(akk_id_str)
    admin_id        = cb.from_user.id
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
    matn     = f"🗣 <b>Avto xabarlar</b>  (ishlaydi: {ishlaydi} ta)\n\n"
    matn    += "\n".join(qatorlar) if qatorlar else "So'z yo'q."
    await xavfsiz_tahrir(cb, matn, ikb([
        [("➕ So'z qo'sh", "sx:qosh"), ("🗑 O'chir",    "sx:ochir")],
        [("▶️ Boshlash",   "avto:bosh"), ("⏹ To'xtatish", "avto:toxt")],
        [("⬅️ Orqaga",     "m:bosh")],
    ]))

@dp.callback_query(F.data == "sx:qosh")
async def soz_qosh(cb: CallbackQuery, state: FSMContext):
    await xavfsiz_tahrir(cb, "Yangi so'z yoki ibora kiriting:")
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
    qatorlar = [[(f"🗑 {s['word']}", f"sd:{s['id']}")] for s in sozlar]
    qatorlar.append([("⬅️ Orqaga", "m:avto")])
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
    aktiv = [a for a in akkauntlar if a["status"] in ("idle", "busy")]
    if not aktiv: return await cb.answer("Aktiv akkunt yo'q")
    boshlandi = 0
    for akk in aktiv:
        if akk["id"] not in auto_tasks or auto_tasks[akk["id"]].done():
            auto_tasks[akk["id"]] = asyncio.create_task(_avto_tsikl(akk["id"]))
            boshlandi += 1
    await cb.answer(f"▶️ {boshlandi} ta akkunt uchun boshlandi")

@dp.callback_query(F.data == "avto:toxt")
async def avto_toxtatish(cb: CallbackQuery):
    for t in auto_tasks.values():
        t.cancel()
    auto_tasks.clear()
    await cb.answer("⏹ To'xtatildi")

async def _avto_tsikl(akk_id: int):
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
                        log.warning(f"avto_xabar xato akk={akk_id}: {e}")
            await asyncio.sleep(random.randint(30, 120))
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"avto_tsikl xato akk={akk_id}: {e}")
            await asyncio.sleep(60)


# ═══════════════════════════════════════════════════════════════════════════════
# Video Chat
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "m:vc")
async def vc_menu(cb: CallbackQuery, state: FSMContext):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    bosh = await db.get_accounts_by_status("idle")
    if not bosh:
        return await xavfsiz_tahrir(cb, "❌ Bo'sh akkunt yo'q.", ikb([[("⬅️", "m:bosh")]]))
    await xavfsiz_tahrir(
        cb,
        f"🎥 <b>Video Chat</b>\n\n"
        f"🟢 Bo'sh akkauntlar: {len(bosh)} ta\n\n"
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
            "❌ Link to'g'ri emas.\n\n"
            "To'g'ri formatlar:\n"
            "<code>t.me/username?videochat</code>\n"
            "<code>t.me/+InviteKod?videochat=HASH</code>",
            parse_mode="HTML"
        )
    bosh = await db.get_accounts_by_status("idle")
    n    = len(bosh)
    await state.update_data(vc_link=link)
    await state.set_state(VideoChat.son)
    await msg.answer(
        f"✅ Link qabul qilindi\n"
        f"🟢 Bo'sh akkauntlar: {n} ta\n\n"
        f"Nechta akkunt kirsin?",
        parse_mode="HTML",
        reply_markup=ikb([
            [("🎥 2 ta", "vcn:2"), ("🎥 4 ta", "vcn:4"), ("🎥 6 ta", "vcn:6")],
            [("🎥 Hammasi ({})".format(n), "vcn:A")],
            [("❌ Bekor", "vcn:bekor")],
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
    await msg.answer(
        f"⏳ {n} ta akkunt video chatga qo'shilmoqda...",
        reply_markup=asosiy_menyu()
    )
    asyncio.create_task(_vc_task(msg, tanlangan, malumot["vc_link"]))

@dp.callback_query(F.data.startswith("vcn:"))
async def vc_son_tugma(cb: CallbackQuery, state: FSMContext):
    cnt     = cb.data[4:]
    malumot = await state.get_data()
    await state.clear()
    if cnt == "bekor":
        return await xavfsiz_tahrir(cb, "❌ Bekor qilindi.", asosiy_menyu())
    if not malumot.get("vc_link"):
        return await xavfsiz_tahrir(cb, "❌ Link topilmadi. Qaytadan boshlang.", asosiy_menyu())
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
    """
    Bir akkuntni video chatga qo'shadi.
    Qaytaradi: (call_input, group_chat_id)
    """
    full_info = await client(GetFullChannelRequest(entity))
    full_chat = full_info.full_chat

    if not (hasattr(full_chat, "call") and full_chat.call):
        raise RuntimeError("Aktiv video chat topilmadi")

    call = full_chat.call  # InputGroupCall

    ssrc   = random.randint(100_000_000, 999_999_999)
    params = DataJSON(data=json.dumps({
        "ufrag":       f"uf{ssrc}",
        "pwd":         f"pw{ssrc}",
        "fingerprints": [{"hash": "sha-256", "fingerprint": "AA" * 32}],
        "ssrc":        ssrc,
        "ssrc-groups": []
    }))

    me = await client.get_input_entity("me")
    await client(JoinGroupCallRequest(
        call=call,
        join_as=me,
        params=params,
        muted=True,
        video_stopped=True,
        invite_hash=None
    ))
    return call


async def _vc_task(msg: Message, akkauntlar: list, link: str):
    """Video chatga link orqali qo'shilish."""
    ok = xato = 0
    xato_sabablari = []

    for akk in akkauntlar:
        nom    = akk["display_name"] or akk["phone"]
        akk_id = akk["id"]

        client = await _get_client(akk_id)
        if not client:
            xato += 1
            xato_sabablari.append(f"• {nom}: ulanmadi (session yo'q?)")
            await asyncio.sleep(join_delay())
            continue

        try:
            # Guruhga qo'shilish
            entity = await guruhga_qoshil(client, link)
            if entity is None:
                raise RuntimeError("Entity topilmadi")

            # Video chatga qo'shilish
            call     = await _vc_join_one(client, entity)
            group_id = entity.id

            # Keep-alive task
            task = asyncio.create_task(
                _vc_keep_alive(akk_id, client, call, group_id, nom)
            )
            vc_ping_tasks[akk_id][group_id] = task

            await db.update_account_status(akk_id, "busy")
            await db.add_log(akk_id, "vc_kirdi", link)
            log.info(f"vc: {nom} ✅ video chatga kirdi")
            ok += 1

        except RuntimeError as e:
            await db.update_account_status(akk_id, "idle")
            xato_sabablari.append(f"• {nom}: {e}")
            xato += 1
        except FloodWaitError as e:
            wait = min(e.seconds, 60)
            await asyncio.sleep(wait)
            xato_sabablari.append(f"• {nom}: FloodWait {e.seconds}s")
            xato += 1
        except Exception as e:
            await db.update_account_status(akk_id, "idle")
            await db.add_log(akk_id, "vc_xato", str(e)[:100], "error")
            xato_sabablari.append(f"• {nom}: {str(e)[:60]}")
            log.error(f"vc_task xato akk={akk_id}: {type(e).__name__}: {e}")
            xato += 1

        await asyncio.sleep(join_delay())

    natija = f"🎥 <b>Video chat natija</b>\n✅ Kirdi: {ok}  ❌ Xato: {xato}"
    if xato_sabablari:
        natija += "\n\n<b>Tafsilotlar:</b>\n" + "\n".join(xato_sabablari[:5])
    try:
        await msg.answer(natija, reply_markup=asosiy_menyu(), parse_mode="HTML")
    except Exception:
        pass


async def _vc_task_by_id(msg: Message, akkauntlar: list, chat_id: int):
    """Video chatga chat_id orqali (ogohlantirish tugmasidan)."""
    ok = xato = 0
    xato_sabablari = []

    for akk in akkauntlar:
        nom    = akk["display_name"] or akk["phone"]
        akk_id = akk["id"]

        client = await _get_client(akk_id)
        if not client:
            xato += 1
            xato_sabablari.append(f"• {nom}: ulanmadi")
            await asyncio.sleep(join_delay())
            continue

        try:
            entity = await client.get_entity(chat_id)
            call   = await _vc_join_one(client, entity)

            task = asyncio.create_task(
                _vc_keep_alive(akk_id, client, call, chat_id, nom)
            )
            vc_ping_tasks[akk_id][chat_id] = task

            await db.update_account_status(akk_id, "busy")
            await db.add_log(akk_id, "vc_kirdi_auto", str(chat_id))
            ok += 1

        except RuntimeError as e:
            xato_sabablari.append(f"• {nom}: {e}")
            xato += 1
        except FloodWaitError as e:
            wait = min(e.seconds, 60)
            await asyncio.sleep(wait)
            xato_sabablari.append(f"• {nom}: FloodWait {e.seconds}s")
            xato += 1
        except Exception as e:
            xato_sabablari.append(f"• {nom}: {str(e)[:60]}")
            log.error(f"vcal xato akk={akk_id}: {type(e).__name__}: {e}")
            xato += 1

        await asyncio.sleep(join_delay())

    natija = f"🎥 <b>Video chat natija</b>\n✅ Kirdi: {ok}  ❌ Xato: {xato}"
    if xato_sabablari:
        natija += "\n" + "\n".join(xato_sabablari[:5])
    try:
        await msg.answer(natija, reply_markup=asosiy_menyu(), parse_mode="HTML")
    except Exception:
        pass


async def _vc_keep_alive(
    akk_id: int, client: TelegramClient,
    call, chat_id: int, nom: str
):
    """Video chat tugaguncha akkuntni ushlab turadi."""
    log.info(f"vc_keep_alive: {nom} chat={chat_id}")
    try:
        while True:
            await asyncio.sleep(30)
            try:
                entity    = await client.get_entity(chat_id)
                full_info = await client(GetFullChannelRequest(entity))
                full_chat = full_info.full_chat
                if not (hasattr(full_chat, "call") and full_chat.call):
                    log.info(f"vc: {nom} — call tugadi")
                    break
            except Exception as e:
                log.warning(f"vc_keep_alive tekshiruv xato {nom}: {e}")
    except asyncio.CancelledError:
        log.info(f"vc_keep_alive bekor: {nom}")
    finally:
        try:
            await client(LeaveGroupCallRequest(call=call, source=0))
            log.info(f"vc: {nom} chiqdi")
        except Exception:
            pass
        await db.update_account_status(akk_id, "idle")
        if akk_id in vc_ping_tasks:
            vc_ping_tasks[akk_id].pop(chat_id, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Sozlamalar
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "m:sozlamalar")
async def menu_sozlamalar(cb: CallbackQuery):
    if not await bosh_admin_mi(cb.from_user.id):
        return await cb.answer("Faqat bosh admin")
    await xavfsiz_tahrir(cb, "⚙️ <b>Sozlamalar</b>", ikb([
        [("➕ Admin qo'sh", "adm:qosh"), ("🗑 Admin o'chir", "adm:ochir")],
        [("👥 Adminlar ro'yxati", "adm:royxat")],
        [("⬅️ Orqaga", "m:bosh")],
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
        await msg.answer(
            f"✅ Admin qo'shildi: <code>{tid}</code>",
            reply_markup=asosiy_menyu(), parse_mode="HTML"
        )
    except ValueError:
        await msg.answer("❌ Noto'g'ri ID. Faqat raqam kiriting.")

@dp.callback_query(F.data == "adm:royxat")
async def adm_royxat(cb: CallbackQuery):
    adminlar = await db.get_all_admins()
    qatorlar = [
        f"• <code>{a['telegram_id']}</code> {a['username'] or ''} ({a['role']})"
        for a in adminlar
    ]
    await xavfsiz_tahrir(
        cb, "👥 <b>Adminlar:</b>\n" + "\n".join(qatorlar),
        ikb([[("⬅️ Orqaga", "m:sozlamalar")]])
    )

@dp.callback_query(F.data == "adm:ochir")
async def adm_ochir_royxat(cb: CallbackQuery):
    adminlar = await db.get_all_admins()
    qatorlar = [
        [(f"🗑 {a['username'] or a['telegram_id']}", f"rmadm:{a['telegram_id']}")]
        for a in adminlar if a["role"] != "main_admin"
    ]
    if not qatorlar: return await cb.answer("O'chirish uchun admin yo'q")
    qatorlar.append([("⬅️ Orqaga", "m:sozlamalar")])
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
        if not (event.is_group or event.is_channel):
            return
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
            if chat_id is None:
                return
            neg_chat_id = int(f"-100{chat_id}") if chat_id > 0 else chat_id
            guruhlar = await db.get_monitored_groups()
            guruh = next(
                (g for g in guruhlar if g["group_id"] in (chat_id, neg_chat_id)),
                None
            )
            if not guruh or not isinstance(call, GroupCall):
                return
            if getattr(call, "discarded", False) or getattr(call, "schedule_date", None):
                return
            bosh = await db.get_accounts_by_status("idle")
            n    = len(bosh)
            matn = (
                f"🎥 <b>Video chat boshlandi!</b>\n"
                f"📍 <b>{guruh['group_name']}</b>\n"
                f"🟢 Bo'sh akkauntlar: {n} ta\n\n"
                f"Video chatga qo'shish kerakmi?"
            )
            await adminlarga_xabar(matn, ikb([
                [("🎥 2 ta qo'sh", f"vcal:2|{neg_chat_id}"),
                 ("🎥 4 ta qo'sh", f"vcal:4|{neg_chat_id}")],
                [("🎥 Hammasi ({})".format(n), f"vcal:A|{neg_chat_id}")],
                [("❌ Kerak emas", "vcal:yoq")],
            ]))
            log.info(f"🎥 VC boshlandi: {guruh['group_name']} ({neg_chat_id})")
        except Exception as e:
            log.error(f"vc_boshlandi xato: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Bot ishga tushirish
# ═══════════════════════════════════════════════════════════════════════════════

async def _barcha_clientlarni_yukla():
    """Barcha akkuntlarni yuklab, ulanadi."""
    akkauntlar = await db.get_all_accounts()
    log.info(f"Jami {len(akkauntlar)} ta akkunt topildi")

    for akk in akkauntlar:
        if akk["status"] in ("paused", "failed"):
            log.info(f"[yukla] akk={akk['id']} ({akk['phone']}) skip ({akk['status']})")
            continue

        session_str = akk.get("session_string") or ""
        if not session_str.strip():
            log.warning(f"[yukla] akk={akk['id']} ({akk['phone']}) — session_string YO'Q!")
            continue

        # _get_client orqali yukla (xatolarni o'zi handle qiladi)
        c = await _get_client(akk["id"])
        if c:
            log.info(f"[yukla] akk={akk['id']} ({akk['phone']}) ✅ yuklandi")
        else:
            log.error(f"[yukla] akk={akk['id']} ({akk['phone']}) ❌ yuklanmadi")

    log.info(f"Yuklandi: {len(clients)} / {len(akkauntlar)} ta client")


async def _qayta_ulanish_tsikl():
    """Har 60 sekund da ulanmagan clientlarni qayta ulanishga urinadi."""
    while True:
        await asyncio.sleep(60)
        for akk in await db.get_all_accounts():
            if akk["status"] in ("paused", "failed"):
                continue
            c = clients.get(akk["id"])
            if c is None:
                # Yuklanmagan — qayta urinish
                session_str = akk.get("session_string") or ""
                if session_str.strip():
                    log.info(f"[qayta] akk={akk['id']} yuklanmagan, urinilmoqda...")
                    await _get_client(akk["id"])
            elif not c.is_connected():
                try:
                    await c.connect()
                    log.info(f"[qayta] akk={akk['id']} qayta ulandi")
                except Exception as e:
                    log.error(f"[qayta] akk={akk['id']} ulanish xato: {e}")
                    await db.update_account_status(akk["id"], "failed")
                    await adminlarga_xabar(f"❌ Ulanmadi: {akk['phone']}")


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
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types()
    )

if __name__ == "__main__":
    asyncio.run(main())
