import asyncio
import random
import logging
import re
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
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest
from telethon.tl.functions.phone import JoinGroupCallRequest
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
vc_ping_tasks: dict[int, dict]      = defaultdict(dict)

# Aktiv VC sessiyalar: {akk_id: {"task": Task, "call_ref": ..., "chat_id": int, "nom": str}}
vc_sessions: dict[int, dict] = {}


# ── FSM holatlari ─────────────────────────────────────────────────────────────

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


# ── Yordamchi funksiyalar ─────────────────────────────────────────────────────

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
            await bot.send_message(a["telegram_id"], matn, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass

def holat_belgisi(holat: str) -> str:
    return {"idle": "🟢", "busy": "🔴", "paused": "⏸", "failed": "❌"}.get(holat, "❓")

def holat_nomi(holat: str) -> str:
    return {"idle": "Bo'sh", "busy": "Band", "paused": "Pauza", "failed": "Xato"}.get(holat, holat)

def asosiy_menyu() -> InlineKeyboardMarkup:
    return ikb([
        [("👤 Akkauntlar",   "m:akkauntlar"), ("➕ Qo'shish",    "m:qoshish")],
        [("📊 Holat",        "m:holat"),      ("👥 Guruhga qo'sh","m:guruh")],
        [("🗣 Avto xabar",   "m:avto"),       ("🎥 Video chat",   "m:vc")],
        [("👁 Monitoring",   "m:monitor"),    ("⚙️ Sozlamalar",   "m:sozlamalar")],
    ])

async def xavfsiz_tahrir(cb: CallbackQuery, matn: str, markup=None):
    try:
        await cb.message.edit_text(matn, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        await cb.message.answer(matn, reply_markup=markup, parse_mode="HTML")


def vc_link_tahlil(link: str):
    link = link.strip()
    m = re.search(r"t\.me/([^?/\s]+)", link)
    if not m:
        return None
    username = m.group(1)
    hm = re.search(r"(?:videochat|voicechat|livestream)=?([^&\s]*)", link)
    vc_hash = hm.group(1) if hm else ""
    return username, vc_hash


# ── /start ────────────────────────────────────────────────────────────────────

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


# ── Akkauntlar ro'yxati ───────────────────────────────────────────────────────

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
    oxirgi = akk["last_active"].strftime("%d.%m %H:%M") if akk["last_active"] else "—"
    matn = (
        f"{holat_belgisi(akk['status'])} <b>{akk['display_name'] or akk['phone']}</b>\n"
        f"📱 <code>{akk['phone']}</code>\n"
        f"👤 @{akk['username'] or '—'}\n"
        f"🔵 Holat: <b>{holat_nomi(akk['status'])}</b>\n"
        f"🕐 Oxirgi faollik: {oxirgi}"
    )
    aid = akk["id"]
    await xavfsiz_tahrir(cb, matn, ikb([
        [("⏸ Pauza", f"hs:{aid}:paused"), ("▶️ Bo'shatish", f"hs:{aid}:idle")],
        [("🗑 O'chirish", f"ochir:{aid}")],
        [("⬅️ Orqaga", "m:akkauntlar")],
    ]))

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


# ── Holat ko'rish ─────────────────────────────────────────────────────────────

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
    qatorlar = [
        f"{holat_belgisi(a['status'])} <b>{a['display_name'] or a['phone']}</b> — {holat_nomi(a['status'])}"
        for a in akkauntlar
    ]
    matn = (
        f"📊 <b>Akkunt holati</b>\n"
        f"🟢 Bo'sh: {bosh}  🔴 Band: {band}  ⏸ Pauza: {pauza}  ❌ Xato: {xato}\n\n"
        + "\n".join(qatorlar)
    )
    await xavfsiz_tahrir(cb, matn, ikb([[("🔄 Yangilash", "m:holat"), ("⬅️ Orqaga", "m:bosh")]]))


# ── Akkunt qo'shish ───────────────────────────────────────────────────────────

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
        daqiqa = e.seconds // 60
        soat   = daqiqa // 60
        vaqt   = f"{soat} soat {daqiqa % 60} daqiqa" if soat > 0 else f"{daqiqa} daqiqa"
        await msg.answer(
            f"⏳ <b>Telegram cheklash qo'ydi</b>\n\n"
            f"Bu raqamga juda ko'p kod yuborildi.\n"
            f"<b>{vaqt}</b> kutib, qaytadan urinib ko'ring.\n\n"
            f"Yoki boshqa raqam ishlating.",
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
    await db.update_account_session(akk_id, malumot["sessiya"], malumot.get("username"))
    pool = await db.get_pool()
    await pool.execute("UPDATE accounts SET display_name=$1 WHERE id=$2", nom, akk_id)
    await state.clear()
    client = clients.get(akk_id)
    if client:
        await _handlerlarni_qoshish(akk_id, client)
    await msg.answer(f"✅ Akkunt qo'shildi: <b>{nom}</b>",
                     reply_markup=asosiy_menyu(), parse_mode="HTML")

async def _sessiya_saqlash(msg: Message, state: FSMContext, akk_id: int, client: TelegramClient):
    men      = await client.get_me()
    ses      = client.session.save()
    avto_nom = men.first_name or men.username or str(men.id)
    await state.update_data(sessiya=ses, username=men.username, avto_nom=avto_nom)
    await state.set_state(AkkuntQoshish.nom)
    await msg.answer(
        f"✅ Kirish muvaffaqiyatli!\n"
        f"👤 @{men.username or '—'} | {men.first_name or ''}\n\n"
        f"Akkunt uchun nom kiriting\n"
        f"(bo'sh qoldirsangiz <b>{avto_nom}</b> ishlatiladi):",
        parse_mode="HTML"
    )


# ── Guruhga qo'shish ──────────────────────────────────────────────────────────

@dp.callback_query(F.data == "m:guruh")
async def guruh_boshlash(cb: CallbackQuery, state: FSMContext):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    bosh = await db.get_accounts_by_status("idle")
    if not bosh:
        return await xavfsiz_tahrir(cb, "❌ Bo'sh akkunt yo'q.", ikb([[("⬅️", "m:bosh")]]))
    await xavfsiz_tahrir(cb, f"🔗 Guruh linkini yuboring:\n<code>t.me/guruh_nomi</code>\n\n🟢 Bo'sh: {len(bosh)} ta")
    await state.set_state(GuruhQoshilish.link)

@dp.message(GuruhQoshilish.link)
async def guruh_link_qabul(msg: Message, state: FSMContext):
    link = msg.text.strip()
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
async def guruh_qoshilish(cb: CallbackQuery):
    cnt, link = cb.data[3:].split("|", 1)
    bosh = await db.get_accounts_by_status("idle")
    if not bosh: return await cb.answer("Bo'sh akkunt yo'q")
    n         = len(bosh) if cnt == "A" else min(int(cnt), len(bosh))
    tanlangan = random.sample(bosh, n)
    await xavfsiz_tahrir(cb, f"⏳ {n} ta akkunt guruhga qo'shilmoqda...")
    asyncio.create_task(_guruh_qoshish_task(cb.message, tanlangan, link, "guruh_qoshish"))

async def _guruh_qoshish_task(msg: Message, akkauntlar: list, link: str, amal: str):
    ok = xato = 0
    for akk in akkauntlar:
        client = clients.get(akk["id"]) or await _client_yuklash(akk)
        if not client:
            xato += 1
            continue
        try:
            await client(JoinChannelRequest(link))
            await db.update_account_status(akk["id"], "idle")
            await db.add_log(akk["id"], amal, link)
            ok += 1
        except FloodWaitError as e:
            log.warning(f"FloodWait {e.seconds}s akk={akk['id']}")
            await asyncio.sleep(min(e.seconds, 30))
            xato += 1
        except Exception as e:
            await db.update_account_status(akk["id"], "failed")
            await db.add_log(akk["id"], amal, str(e), "error")
            log.error(f"guruh_qoshish xato akk={akk['id']}: {e}")
            xato += 1
        await asyncio.sleep(random.randint(config.JOIN_DELAY_MIN, config.JOIN_DELAY_MAX))
    try:
        await msg.answer(f"✅ Qo'shildi: {ok}  ❌ Xato: {xato}", reply_markup=asosiy_menyu())
    except Exception:
        pass


# ── Monitoring ────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "m:monitor")
async def menu_monitor(cb: CallbackQuery):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    guruhlar   = await db.get_monitored_groups()
    akkauntlar = await db.get_all_accounts()
    bosh = sum(1 for a in akkauntlar if a["status"] == "idle")
    band = sum(1 for a in akkauntlar if a["status"] == "busy")
    matn = "👁 <b>Monitoring</b>\n\n<b>Kuzatilayotgan guruhlar:</b>\n"
    matn += ("\n".join(f"• {g['group_name']} (<code>{g['group_id']}</code>)" for g in guruhlar)
             if guruhlar else "Guruh yo'q.") + f"\n\n<b>Akkauntlar:</b> 🟢 Bo'sh: {bosh}  🔴 Band: {band}"
    await xavfsiz_tahrir(cb, matn, ikb([[("⬅️ Orqaga", "m:bosh")]]))

def _faollik_tekshir(guruh_id: int) -> bool:
    chegara = datetime.utcnow() - timedelta(seconds=config.ACTIVITY_WINDOW_SEC)
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
        [("❌ E'tibor berma", "ogl:yo'q")],
    ]))

@dp.callback_query(F.data.startswith("ogl:"))
async def ogohlantirish_amal(cb: CallbackQuery):
    qiymat = cb.data[4:]
    if qiymat == "yo'q":
        return await xavfsiz_tahrir(cb, "✅ E'tiborsiz qoldirildi.")
    cnt, gid_str = qiymat.split("|", 1)
    guruh_id     = int(gid_str)
    bosh         = await db.get_accounts_by_status("idle")
    if not bosh: return await cb.answer("Bo'sh akkunt yo'q")
    if cnt == "Q":
        qatorlar = [[(f"🟢 {a['display_name'] or a['phone']}", f"qt:{a['id']}|{guruh_id}")] for a in bosh]
        qatorlar.append([("✅ Boshlash", f"qb:{guruh_id}"), ("❌ Bekor", "ogl:yo'q")])
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
    admin_id       = cb.from_user.id
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


# ── Avto xabar ────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "m:avto")
async def menu_avto(cb: CallbackQuery):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    sozlar   = await db.get_all_words()
    ishlaydi = sum(1 for t in auto_tasks.values() if not t.done())
    qatorlar = [f"{s['id']}. {s['word']} {'✅' if s['is_active'] else '❌'}" for s in sozlar]
    matn     = f"🗣 <b>Avto xabarlar</b>  (ishlaydi: {ishlaydi} ta)\n\n"
    matn    += "\n".join(qatorlar) if qatorlar else "So'z yo'q."
    await xavfsiz_tahrir(cb, matn, ikb([
        [("➕ So'z qo'sh", "sx:qosh"), ("🗑 O'chir",     "sx:ochir")],
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
            client    = clients.get(akk_id)
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


# ── Video Chat ────────────────────────────────────────────────────────────────

def _vc_holat_xabari(bosh_soni: int) -> str:
    if not vc_sessions:
        return f"🎥 <b>Video Chat</b>\n\nHozir hech kim video chatda yo'q.\n\n🟢 Bo'sh akkauntlar: {bosh_soni} ta"
    qatorlar = [f"🟢 {s['nom']}" for s in vc_sessions.values()]
    return (
        f"🎥 <b>Video Chatda ({len(vc_sessions)} ta):</b>\n\n"
        + "\n".join(qatorlar)
        + f"\n\n🟢 Bo'sh akkauntlar: {bosh_soni} ta"
    )


@dp.callback_query(F.data == "m:vc")
async def vc_menu(cb: CallbackQuery, state: FSMContext):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    bosh = await db.get_accounts_by_status("idle")
    await xavfsiz_tahrir(cb, _vc_holat_xabari(len(bosh)), ikb([
        [("➕ Qo'shish", "vc:qosh"), ("🚪 Hammasini chiqar", "vc:chiqar_hammasi")],
        [("⬅️ Orqaga", "m:bosh")],
    ]))


@dp.callback_query(F.data == "vc:qosh")
async def vc_qosh_boshlash(cb: CallbackQuery, state: FSMContext):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    bosh = await db.get_accounts_by_status("idle")
    if not bosh:
        return await cb.answer("❌ Bo'sh akkunt yo'q")
    await xavfsiz_tahrir(
        cb,
        f"🎥 <b>Video Chat — qo'shish</b>\n\n"
        f"🟢 Bo'sh: {len(bosh)} ta\n\n"
        f"Video chat linkini yuboring:\n"
        f"<code>t.me/username?videochat</code>\n"
        f"<code>t.me/username?videochat=HASH</code>"
    )
    await state.set_state(VideoChat.link)


@dp.message(VideoChat.link)
async def vc_link_qabul(msg: Message, state: FSMContext):
    link   = msg.text.strip()
    tahlil = vc_link_tahlil(link)
    if not tahlil:
        return await msg.answer(
            "❌ Link to'g'ri emas.\n\nTo'g'ri format:\n"
            "<code>t.me/username?videochat</code>",
            parse_mode="HTML"
        )
    username, vc_hash = tahlil
    bosh = await db.get_accounts_by_status("idle")
    n    = len(bosh)
    await state.update_data(vc_link=link, vc_username=username, vc_hash=vc_hash)
    await state.set_state(VideoChat.son)
    await msg.answer(
        f"✅ Link: <code>{username}</code>\n"
        f"🟢 Bo'sh: {n} ta\n\n"
        f"Nechta akkunt kirsin?\n"
        f"(Raqam yozing yoki tugma bosing)",
        parse_mode="HTML",
        reply_markup=ikb([
            [("🎥 5 ta",  "vcn:5"),  ("🎥 10 ta", "vcn:10"), ("🎥 20 ta", "vcn:20")],
            [("🎥 Hammasi ({} ta)".format(n), "vcn:A")],
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
    await msg.answer(f"⏳ {n} ta akkunt video chatga qo'shilmoqda...", reply_markup=asosiy_menyu())
    asyncio.create_task(_vc_task(msg, tanlangan, malumot["vc_link"], malumot["vc_username"]))


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
    asyncio.create_task(_vc_task(cb.message, tanlangan, malumot["vc_link"], malumot["vc_username"]))


@dp.callback_query(F.data.startswith("vcal:"))
async def vcal_amal(cb: CallbackQuery):
    qiymat = cb.data[5:]
    if qiymat == "yoq":
        return await xavfsiz_tahrir(cb, "✅ E'tiborsiz qoldirildi.")
    cnt, chat_id_str = qiymat.split("|", 1)
    chat_id = int(chat_id_str)
    bosh    = await db.get_accounts_by_status("idle")
    if not bosh:
        return await cb.answer("Bo'sh akkunt yo'q")
    n         = len(bosh) if cnt == "A" else min(int(cnt), len(bosh))
    tanlangan = random.sample(bosh, n)
    await xavfsiz_tahrir(cb, f"⏳ {n} ta akkunt video chatga qo'shilmoqda...")
    asyncio.create_task(_vc_task_by_id(cb.message, tanlangan, chat_id))


@dp.callback_query(F.data == "vc:chiqar_hammasi")
async def vc_chiqar_hammasi(cb: CallbackQuery):
    if not vc_sessions:
        return await cb.answer("Video chatda hech kim yo'q")
    soni = len(vc_sessions)
    for ses in list(vc_sessions.values()):
        ses["task"].cancel()
    await cb.answer(f"🚪 {soni} ta akkunt chiqarilmoqda...")
    await asyncio.sleep(0.5)
    bosh = await db.get_accounts_by_status("idle")
    await xavfsiz_tahrir(cb, _vc_holat_xabari(len(bosh)), ikb([
        [("➕ Qo'shish", "vc:qosh"), ("🚪 Hammasini chiqar", "vc:chiqar_hammasi")],
        [("⬅️ Orqaga", "m:bosh")],
    ]))



async def _vc_task(msg: Message, akkauntlar: list, link: str, username: str):
    import json, random as rnd

    # 1. Barcha akkuntlar uchun client'larni yuklaymiz
    yuklangan = []
    for akk in akkauntlar:
        c = clients.get(akk["id"])
        if not c:
            c = await _client_yuklash(akk)
        if c and c.is_connected():
            yuklangan.append((akk, c))
        elif c:
            try:
                await c.connect()
                if await c.is_user_authorized():
                    yuklangan.append((akk, c))
            except Exception as e:
                log.warning(f"vc reconnect xato {akk['phone']}: {e}")

    if not yuklangan:
        return await msg.answer(
            "❌ Hech qanday akkunt ulana olmadi.\n"
            "📊 Holat menyusidan akkuntlarni tekshiring.",
            reply_markup=asosiy_menyu(), parse_mode="HTML"
        )

    # 2. Entity va call_ref birinchi muvaffaqiyatli client orqali
    entity   = None
    call_ref = None
    for akk, c in yuklangan:
        try:
            entity    = await c.get_entity(username)
            full_info = await c(GetFullChannelRequest(entity))
            full_chat = full_info.full_chat
            if hasattr(full_chat, "call") and full_chat.call:
                call_ref = full_chat.call
                break
            else:
                return await msg.answer(
                    "❌ Bu kanalda hozir aktiv video chat yo'q.",
                    reply_markup=asosiy_menyu()
                )
        except Exception as e:
            log.warning(f"vc entity xato {akk['phone']}: {e}")
            continue

    if not entity or not call_ref:
        return await msg.answer(
            f"❌ Kanal topilmadi: <code>{username}</code>\n"
            f"Username to'g'riligini tekshiring.",
            reply_markup=asosiy_menyu(), parse_mode="HTML"
        )

    # 3. Barcha yuklangan akkuntlarni video chatga qo'shamiz
    # Har bir akkunt o'z client'i orqali entity va call_ref oladi
    ok = xato = 0
    for akk, client in yuklangan:
        nom = akk["display_name"] or akk["phone"]
        try:
            # Har bir client uchun alohida entity va call_ref olish
            try:
                akk_entity = await client.get_entity(username)
            except Exception as e:
                log.warning(f"vc get_entity {nom}: {e} — birinchi entity ishlatiladi")
                akk_entity = entity  # fallback

            try:
                akk_full  = await client(GetFullChannelRequest(akk_entity))
                akk_call  = akk_full.full_chat.call
                if not akk_call:
                    log.warning(f"vc: {nom} — call topilmadi, birinchi call_ref ishlatiladi")
                    akk_call = call_ref
            except Exception as e:
                log.warning(f"vc GetFull {nom}: {e} — birinchi call_ref ishlatiladi")
                akk_call = call_ref

            ssrc   = rnd.randint(100000000, 999999999)
            params = json.dumps({
                "ufrag": f"ufrag{ssrc}", "pwd": f"pwd{ssrc}",
                "fingerprints": [], "ssrc": ssrc
            })
            me = await client.get_input_entity("me")
            await client(JoinGroupCallRequest(
                call=akk_call, join_as=me,
                muted=True, video_stopped=True,
                params=DataJSON(data=params),
                invite_hash=None
            ))

            task = asyncio.create_task(
                _vc_keep_alive(akk["id"], client, akk_call, akk_entity.id, nom)
            )
            vc_sessions[akk["id"]] = {
                "task":     task,
                "call_ref": akk_call,
                "chat_id":  akk_entity.id,
                "nom":      nom,
            }
            vc_ping_tasks[akk["id"]][akk_entity.id] = task
            await db.update_account_status(akk["id"], "busy")
            log.info(f"vc: {nom} ✅ kirdi")
            ok += 1
        except Exception as e:
            log.error(f"vc: {nom} xato: {e}")
            await db.update_account_status(akk["id"], "idle")
            xato += 1

        if ok + xato < len(yuklangan):
            await asyncio.sleep(random.randint(2, 5))

    await db.add_log(None, "vc_qoshildi", f"{username}: {ok} kirdi, {xato} xato")
    try:
        await msg.answer(
            f"🎥 <b>Video chat natija</b>\n"
            f"✅ Kirdi: {ok}  ❌ Xato: {xato}\n\n"
            f"Chiqarish: 🎥 Video chat → 🚪 Hammasini chiqar",
            reply_markup=asosiy_menyu(), parse_mode="HTML"
        )
    except Exception:
        pass


async def _vc_task_by_id(msg: Message, akkauntlar: list, chat_id: int):
    import json, random as rnd

    yuklangan = []
    for akk in akkauntlar:
        c = clients.get(akk["id"])
        if not c:
            c = await _client_yuklash(akk)
        if c and c.is_connected():
            yuklangan.append((akk, c))
        elif c:
            try:
                await c.connect()
                if await c.is_user_authorized():
                    yuklangan.append((akk, c))
            except Exception as e:
                log.warning(f"vc reconnect xato {akk['phone']}: {e}")

    if not yuklangan:
        return await msg.answer(
            "❌ Hech qanday akkunt ulana olmadi.",
            reply_markup=asosiy_menyu()
        )

    entity   = None
    call_ref = None
    for akk, c in yuklangan:
        try:
            entity    = await c.get_entity(chat_id)
            full_info = await c(GetFullChannelRequest(entity))
            full_chat = full_info.full_chat
            if hasattr(full_chat, "call") and full_chat.call:
                call_ref = full_chat.call
                break
            else:
                return await msg.answer(
                    "❌ Bu kanalda hozir aktiv video chat yo'q.",
                    reply_markup=asosiy_menyu()
                )
        except Exception as e:
            log.warning(f"vc_by_id entity xato {akk['phone']}: {e}")
            continue

    if not entity or not call_ref:
        return await msg.answer("❌ Kanal topilmadi.", reply_markup=asosiy_menyu())

    ok = xato = 0
    for akk, client in yuklangan:
        nom = akk["display_name"] or akk["phone"]
        try:
            # Har bir client uchun alohida entity va call_ref olish
            try:
                akk_entity = await client.get_entity(chat_id)
                akk_full   = await client(GetFullChannelRequest(akk_entity))
                akk_call   = akk_full.full_chat.call
                if not akk_call:
                    akk_call = call_ref
            except Exception as e:
                log.warning(f"vc_by_id GetFull {nom}: {e} — birinchi call_ref ishlatiladi")
                akk_entity = entity
                akk_call   = call_ref

            ssrc   = rnd.randint(100000000, 999999999)
            params = json.dumps({
                "ufrag": f"ufrag{ssrc}", "pwd": f"pwd{ssrc}",
                "fingerprints": [], "ssrc": ssrc
            })
            me = await client.get_input_entity("me")
            await client(JoinGroupCallRequest(
                call=akk_call, join_as=me,
                muted=True, video_stopped=True,
                params=DataJSON(data=params),
                invite_hash=None
            ))

            task = asyncio.create_task(
                _vc_keep_alive(akk["id"], client, akk_call, akk_entity.id, nom)
            )
            vc_sessions[akk["id"]] = {
                "task":     task,
                "call_ref": akk_call,
                "chat_id":  akk_entity.id,
                "nom":      nom,
            }
            vc_ping_tasks[akk["id"]][akk_entity.id] = task
            await db.update_account_status(akk["id"], "busy")
            log.info(f"vc_by_id: {nom} ✅ kirdi")
            ok += 1
        except Exception as e:
            log.error(f"vc_by_id: {nom} xato: {e}")
            await db.update_account_status(akk["id"], "idle")
            xato += 1

        if ok + xato < len(yuklangan):
            await asyncio.sleep(random.randint(2, 5))

    try:
        await msg.answer(
            f"🎥 <b>Video chat natija</b>\n"
            f"✅ Kirdi: {ok}  ❌ Xato: {xato}\n\n"
            f"Chiqarish: 🎥 Video chat → 🚪 Hammasini chiqar",
            reply_markup=asosiy_menyu(), parse_mode="HTML"
        )
    except Exception:
        pass


async def _vc_keep_alive(akk_id: int, client: TelegramClient, call_ref, chat_id: int, nom: str):
    """
    Akkuntni video chatda USHLAB TURADI.
    FAQAT task.cancel() kelganda chiqadi — boshqa hech qanday holatda emas.
    finally ishlatilmaydi — u har doim ishlaydi va akkuntni chiqarib yuboradi.
    """
    from telethon.tl.functions.phone import LeaveGroupCallRequest

    log.info(f"vc_keep_alive boshlandi: {nom}")

    admin_chiqardi = False

    while True:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            admin_chiqardi = True
            break

        # Har 30 sekundda ulanishni tirik saqlash
        try:
            if not client.is_connected():
                await client.connect()
            await client.get_me()
        except asyncio.CancelledError:
            admin_chiqardi = True
            break
        except Exception as e:
            # Xato bo'lsa ham DAVOM ETAMIZ — chiqmaymiz
            log.warning(f"vc ping xato {nom}: {e}")

    # Faqat admin chiqargan bo'lsa Leave yuboramiz
    if admin_chiqardi:
        try:
            await client(LeaveGroupCallRequest(call=call_ref, source=0))
            log.info(f"vc: {nom} chiqdi (admin buyrug'i)")
        except Exception as e:
            log.warning(f"vc Leave xato {nom}: {e}")

    await db.update_account_status(akk_id, "idle")
    vc_sessions.pop(akk_id, None)
    if akk_id in vc_ping_tasks:
        vc_ping_tasks[akk_id].pop(chat_id, None)
    await db.add_log(akk_id, "vc_chiqdi", "admin" if admin_chiqardi else "kutilmagan_chiqish")


# ── Sozlamalar ────────────────────────────────────────────────────────────────

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
        await msg.answer(f"✅ Admin qo'shildi: <code>{tid}</code>",
                         reply_markup=asosiy_menyu(), parse_mode="HTML")
    except ValueError:
        await msg.answer("❌ Noto'g'ri ID. Faqat raqam kiriting.")

@dp.callback_query(F.data == "adm:royxat")
async def adm_royxat(cb: CallbackQuery):
    adminlar = await db.get_all_admins()
    qatorlar = [f"• <code>{a['telegram_id']}</code> {a['username'] or ''} ({a['role']})"
                for a in adminlar]
    await xavfsiz_tahrir(cb, "👥 <b>Adminlar:</b>\n" + "\n".join(qatorlar),
                         ikb([[("⬅️ Orqaga", "m:sozlamalar")]]))

@dp.callback_query(F.data == "adm:ochir")
async def adm_ochir_royxat(cb: CallbackQuery):
    adminlar = await db.get_all_admins()
    qatorlar = [[(f"🗑 {a['username'] or a['telegram_id']}", f"rmadm:{a['telegram_id']}")]
                for a in adminlar if a["role"] != "main_admin"]
    if not qatorlar: return await cb.answer("O'chirish uchun admin yo'q")
    qatorlar.append([("⬅️ Orqaga", "m:sozlamalar")])
    await xavfsiz_tahrir(cb, "O'chirmoqchi bo'lgan adminni tanlang:", ikb(qatorlar))

@dp.callback_query(F.data.startswith("rmadm:"))
async def adm_ochir(cb: CallbackQuery):
    await db.remove_admin(int(cb.data.split(":")[1]))
    await cb.answer("🗑 O'chirildi")
    cb.data = "adm:ochir"
    await adm_ochir_royxat(cb)


# ── Telethon ──────────────────────────────────────────────────────────────────

async def _client_yuklash(akk: dict) -> TelegramClient | None:
    if not akk.get("session_string"):
        return None
    try:
        c = TelegramClient(StringSession(akk["session_string"]), config.API_ID, config.API_HASH)
        await c.connect()
        if not await c.is_user_authorized():
            await db.update_account_status(akk["id"], "failed")
            await adminlarga_xabar(f"❌ Sessiya tugadi: {akk['phone']}")
            return None
        clients[akk["id"]] = c
        await _handlerlarni_qoshish(akk["id"], c)
        return c
    except Exception as e:
        log.error(f"client_yuklash {akk['phone']}: {e}")
        await db.update_account_status(akk["id"], "failed")
        return None

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
                (g for g in guruhlar if g["group_id"] in (chat_id, neg_chat_id)), None
            )
            if not guruh:
                return
            if not isinstance(call, GroupCall):
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
                [("🎥 5 ta qo'sh",  f"vcal:5|{neg_chat_id}"),
                 ("🎥 10 ta qo'sh", f"vcal:10|{neg_chat_id}")],
                [("🎥 Hammasi ({} ta)".format(n), f"vcal:A|{neg_chat_id}")],
                [("❌ Kerak emas", "vcal:yoq")],
            ]))
        except Exception as e:
            log.error(f"vc_boshlandi xato: {e}", exc_info=True)

async def _barcha_clientlarni_yukla():
    akkauntlar = await db.get_all_accounts()
    for akk in akkauntlar:
        if akk["session_string"] and akk["status"] not in ("paused", "failed"):
            await _client_yuklash(akk)
    log.info(f"Yuklandi: {len(clients)} ta client")

async def _qayta_ulanish_tsikl():
    while True:
        await asyncio.sleep(60)
        for akk in await db.get_all_accounts():
            if akk["status"] in ("paused", "failed"):
                continue
            c = clients.get(akk["id"])
            if c and not c.is_connected():
                try:
                    await c.connect()
                    log.info(f"Qayta ulandi: {akk['phone']}")
                except Exception as e:
                    log.error(f"Qayta ulanish xato {akk['phone']}: {e}")
                    await db.update_account_status(akk["id"], "failed")
                    await adminlarga_xabar(f"❌ Ulanmadi: {akk['phone']}")


# ── Ishga tushirish ───────────────────────────────────────────────────────────

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
