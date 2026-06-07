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
from telethon.tl.functions.phone import JoinGroupCallRequest, LeaveGroupCallRequest, GetGroupCallRequest
from telethon.tl.types import UpdateGroupCall, GroupCall, DataJSON

import config
import database as db
import proxy_fetcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

clients: dict[int, TelegramClient]  = {}
group_activity: dict[int, list]     = defaultdict(list)
auto_tasks: dict[int, asyncio.Task] = {}
reply_tasks: dict[int, asyncio.Task] = {}
manual_picks: dict[int, list]       = defaultdict(list)
vc_ping_tasks: dict[int, dict]      = defaultdict(dict)
vc_sessions: dict[int, dict]        = {}
_yuborilgan_vc_xabarlar: set        = set()


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

class AvtoXabarSozlash(StatesGroup):
    min_interval = State()
    max_interval = State()

class ReplyQoshish(StatesGroup):
    trigger = State()
    javob   = State()

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

class ProxyQoshish(StatesGroup):
    matn = State()


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
        [("🔴 Hammasini bo'shatish","m:boshat")],
        [("🌐 Proxy","m:proxy")],
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

    for urinish in range(3):
        try:
            _devices = [
                ("Samsung Galaxy S23", "Android 13"),
                ("iPhone 14 Pro", "iOS 16.5"),
                ("Xiaomi 13", "Android 13"),
                ("OnePlus 11", "Android 13"),
                ("Huawei P60", "Android 12"),
                ("Samsung Galaxy A54", "Android 13"),
                ("iPhone 13", "iOS 16.3"),
                ("Redmi Note 12", "Android 12"),
                ("Oppo Reno 10", "Android 13"),
                ("Vivo V27", "Android 13"),
                ("iPhone 12", "iOS 15.7"),
                ("Samsung Galaxy S21", "Android 12"),
                ("Poco X5 Pro", "Android 12"),
                ("Realme GT 3", "Android 13"),
                ("Nokia G60", "Android 12"),
                ("Motorola Edge 40", "Android 13"),
                ("Sony Xperia 1 V", "Android 13"),
                ("Google Pixel 7", "Android 13"),
                ("LG Velvet", "Android 11"),
                ("Asus ROG Phone 7", "Android 13"),
            ]
            _dev = _devices[akk_id % len(_devices)]

            _proxy_tuple = None
            try:
                if akk.get("proxy_id"):
                    _p = await db.get_proxy_by_id(akk["proxy_id"])
                    if _p and _p["is_active"] and _p["fail_count"] < 5:
                        _proxy_tuple = ("socks5", _p["host"], _p["port"])
                if not _proxy_tuple:
                    _rp = await db.get_random_proxy()
                    if _rp:
                        _proxy_tuple = ("socks5", _rp["host"], _rp["port"])
                        await db.set_account_proxy(akk_id, _rp["id"])
            except Exception:
                _proxy_tuple = None

            c = TelegramClient(
                StringSession(ses), config.API_ID, config.API_HASH,
                connection_retries=5,
                retry_delay=3,
                auto_reconnect=True,
                device_model=_dev[0],
                system_version=_dev[1],
                app_version="9.6.3",
                lang_code="uz",
                system_lang_code="uz-UZ",
                proxy=_proxy_tuple,
            )
            if _proxy_tuple:
                log.info(f"[get_client] akk={akk_id} proxy: {_proxy_tuple[1]}:{_proxy_tuple[2]}")
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
            err = str(e)
            if "AUTH_KEY_DUPLICATED" in err:
                log.warning(f"[get_client] akk={akk_id} AUTH_KEY_DUPLICATED — {urinish+1}/3 urinish, 10s kutilmoqda...")
                try: await c.disconnect()
                except: pass
                await asyncio.sleep(10)
                continue
            log.error(f"[get_client] akk={akk_id} XATO: {e}")
            await db.update_account_status(akk_id, "failed")
            return None
    log.error(f"[get_client] akk={akk_id} 3 urinishdan keyin ham ulanmadi")
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
# /start va asosiy menyular
# ═══════════════════════════════════════════════════════════════════════════════

async def _akkauntlar_ikb():
    akkauntlar = await db.get_all_accounts()
    qatorlar = [[(
        f"{holat_belgisi(a['status'])} {a['display_name'] or a['phone']} — {holat_nomi(a['status'])}",
        f"akk:{a['id']}"
    )] for a in akkauntlar]
    qatorlar.append([("➕ Qo'shish","m:qoshish"),("⬅️ Orqaga","m:bosh")])
    return ikb(qatorlar)

async def _akk_detail_show(cb: CallbackQuery, akk: dict):
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
    kb = [[("⏸ Pauza",f"hs:{aid}:paused"),("▶️ Bo'shatish",f"hs:{aid}:idle")]]
    if not has_ses:
        kb.append([("🔑 Login qil",f"ses_login:{aid}")])
    kb.append([("🗑 O'chirish",f"ochir:{aid}")])
    kb.append([("⬅️ Orqaga","m:akkauntlar")])
    await xavfsiz_tahrir(cb, matn, ikb(kb))

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
            "{}/{} — {} ({})  kod yuborildi, kiriting:".format(idx+1, len(navbat), akk.get("display_name") or "", telefon)
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

@dp.message(Command("add_proxies"))
async def cmd_add_proxies(msg: Message, state: FSMContext):
    if not await bosh_admin_mi(msg.from_user.id):
        return await msg.answer("Faqat bosh admin.")
    await msg.answer(
        "Proxylarni yuboring (har qatorda bitta):\n\n"
        "<code>IP:PORT:USER:PASS</code>\n\n"
        "Misol:\n"
        "<code>104.239.107.47:5699:orqorvft:hz07pe7rtvl5\n"
        "31.58.9.4:6077:orqorvft:hz07pe7rtvl5</code>",
        parse_mode="HTML"
    )
    await state.set_state(ProxyQoshish.matn)

@dp.message(ProxyQoshish.matn)
async def proxy_matn_qabul(msg: Message, state: FSMContext):
    await state.clear()
    proxies = []
    for qator in msg.text.strip().splitlines():
        qator = qator.strip()
        if not qator: continue
        parts = qator.split(":")
        if len(parts) == 4:
            try:
                proxies.append({
                    "host": parts[0], "port": int(parts[1]),
                    "username": parts[2], "password": parts[3]
                })
            except Exception:
                pass
        elif len(parts) == 2:
            try:
                proxies.append({"host": parts[0], "port": int(parts[1])})
            except Exception:
                pass
    if not proxies:
        return await msg.answer("❌ Format xato. Qaytadan /add_proxies yozing.")
    saqlandi   = await db.add_proxies_bulk(proxies)
    akkauntlar = await db.get_all_accounts()
    all_proxies = await db.get_active_proxies()
    birikmalar = 0
    for i, akk in enumerate(akkauntlar):
        if all_proxies:
            p = all_proxies[i % len(all_proxies)]
            await db.set_account_proxy(akk["id"], p["id"])
            birikmalar += 1
    await msg.answer(
        "✅ <b>{}</b> ta proxy saqlandi!\n"
        "🔗 <b>{}</b> ta akkuntga biriktirildi.\n\n"
        "Endi /reset_sessions → /login_all → /reload".format(saqlandi, birikmalar),
        parse_mode="HTML", reply_markup=asosiy_menyu()
    )

@dp.message(Command("reset_sessions"))
async def cmd_reset_sessions(msg: Message, state: FSMContext):
    if not await bosh_admin_mi(msg.from_user.id):
        return await msg.answer("Faqat bosh admin.")
    pool = await db.get_pool()
    for akk_id, c in list(clients.items()):
        try: await c.disconnect()
        except: pass
    clients.clear()
    r = await pool.execute("UPDATE accounts SET session_string=NULL, status='idle'")
    n = r.split()[-1] if r else "?"
    await msg.answer(
        "{} ta akkunt session o'chirildi.\n\nEndi /login_all bilan qayta login qiling.".format(n),
        reply_markup=asosiy_menyu()
    )

@dp.message(Command("reload"))
async def cmd_reload(msg: Message):
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

@dp.callback_query(F.data == "m:boshat")
async def boshat_confirm(cb: CallbackQuery):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    aktiv_vc   = len(vc_sessions)
    aktiv_avto = sum(1 for t in auto_tasks.values() if not t.done())
    aktiv_rep  = sum(1 for t in reply_tasks.values() if not t.done())
    busy = await db.get_accounts_by_status("busy")
    await xavfsiz_tahrir(cb,
        f"🔴 <b>Hammasini bo'shatish</b>\n\n"
        f"🎥 Video chatda: {aktiv_vc} ta\n"
        f"🗣 Avto xabar: {aktiv_avto} ta\n"
        f"🔄 Auto reply: {aktiv_rep} ta\n"
        f"🔴 Band akkunt: {len(busy)} ta\n\n"
        f"Tasdiqlaysizmi?",
        ikb([
            [("✅ Ha, hammasini to'xtat", "boshat:yes")],
            [("❌ Yo'q", "m:bosh")],
        ])
    )

@dp.callback_query(F.data == "boshat:yes")
async def boshat_execute(cb: CallbackQuery):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    vc_n = avto_n = rep_n = db_n = 0

    # 1. Video chatlardan chiqarish — chiqishni kutamiz
    vc_tasks = []
    for ses in list(vc_sessions.values()):
        ses["task"].cancel()
        vc_tasks.append(ses["task"])
        vc_n += 1
    if vc_tasks:
        await asyncio.gather(*vc_tasks, return_exceptions=True)

    # 2. Avto xabarlarni to'xtatish
    for t in list(auto_tasks.values()):
        try: t.cancel()
        except: pass
        avto_n += 1
    auto_tasks.clear()

    # 3. Reply larni to'xtatish
    for t in list(reply_tasks.values()):
        try: t.cancel()
        except: pass
        rep_n += 1
    reply_tasks.clear()

    # 4. DB da busy akkuntlarni idle ga qaytarish
    pool = await db.get_pool()
    r = await pool.execute("UPDATE accounts SET status='idle' WHERE status='busy'")
    db_n = int(r.split()[-1]) if r else 0

    await xavfsiz_tahrir(cb,
        f"✅ <b>Hammasi bo'shatildi!</b>\n\n"
        f"🎥 Video chatdan chiqarildi: {vc_n} ta\n"
        f"🗣 Avto xabar to'xtatildi: {avto_n} ta\n"
        f"🔄 Reply to'xtatildi: {rep_n} ta\n"
        f"🟢 Idle ga qaytarildi: {db_n} ta",
        asosiy_menyu()
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Akkauntlar
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "m:proxy")
async def menu_proxy(cb: CallbackQuery):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    stat = await db.get_proxy_count()
    matn = (
        "🌐 <b>Proxy boshqaruvi</b>\n\n"
        "📊 Jami: <b>{}</b>\n"
        "✅ Aktiv: <b>{}</b>\n"
        "❌ Yomon: <b>{}</b>"
    ).format(stat["jami"], stat["aktiv"], stat["yomon"])
    await xavfsiz_tahrir(cb, matn, ikb([
        [("🔄 Avtomatik yuklash", "px:auto"), ("📋 Ro'yxat", "px:royxat")],
        [("🗑 Yomonlarni tozala", "px:tozala"), ("🔗 Akkuntlarga biriktir", "px:biriktir")],
        [("⬅️ Orqaga", "m:bosh")],
    ]))

@dp.callback_query(F.data == "px:auto")
async def proxy_auto(cb: CallbackQuery):
    await xavfsiz_tahrir(cb, "⏳ Proxy yuklanmoqda... (30-60 soniya)")
    try:
        natija = await proxy_fetcher.proxies_yangilash(max_test=100)
        stat   = await db.get_proxy_count()
        await cb.message.edit_text(
            "✅ <b>Proxy yangilandi!</b>\n\n"
            "Tekshirildi: {} ta\n"
            "Ishlaydigan: {} ta\n"
            "Saqlandi: {} ta\n\n"
            "DB da aktiv: <b>{}</b> ta".format(
                natija["tekshirildi"], natija["ishlaydigan"],
                natija["saqlandi"], stat["aktiv"]
            ),
            parse_mode="HTML",
            reply_markup=ikb([[("⬅️ Orqaga", "m:proxy")]])
        )
    except Exception as e:
        await cb.message.edit_text(f"❌ Xato: {e}", reply_markup=ikb([[("⬅️", "m:proxy")]]))

@dp.callback_query(F.data == "px:biriktir")
async def proxy_biriktir(cb: CallbackQuery):
    akkauntlar = await db.get_all_accounts()
    n = 0
    for akk in akkauntlar:
        p = await db.get_random_proxy()
        if p:
            await db.set_account_proxy(akk["id"], p["id"])
            n += 1
    await cb.answer(f"✅ {n} ta akkuntga proxy biriktirildi")
    await menu_proxy(cb)

@dp.callback_query(F.data == "px:royxat")
async def proxy_royxat(cb: CallbackQuery):
    proxies = await db.get_all_proxies()
    if not proxies:
        return await xavfsiz_tahrir(cb, "Proxy yo'q.", ikb([[("⬅️", "m:proxy")]]))
    qatorlar = []
    for p in proxies[:15]:
        belgi = "✅" if (p["is_active"] and p["fail_count"] < 5) else "❌"
        qatorlar.append(f"{belgi} {p['host']}:{p['port']} xato:{p['fail_count']}")
    matn = "📋 <b>Proxylar ({} ta):</b>\n\n".format(len(proxies)) + "\n".join(qatorlar)
    if len(proxies) > 15:
        matn += f"\n...va {len(proxies)-15} ta yana"
    await xavfsiz_tahrir(cb, matn, ikb([[("⬅️ Orqaga", "m:proxy")]]))

@dp.callback_query(F.data == "px:tozala")
async def proxy_tozala(cb: CallbackQuery):
    await db.deactivate_bad_proxies()
    await cb.answer("✅ Tozalandi")
    await menu_proxy(cb)

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
    akk2 = await db.get_account(int(aid))
    if akk2:
        await _akk_detail_show(cb, akk2)

@dp.callback_query(F.data.startswith("ochir:"))
async def akkunt_ochir(cb: CallbackQuery):
    aid = int(cb.data.split(":")[1])
    if aid in clients:
        try: await clients[aid].disconnect()
        except: pass
        del clients[aid]
    await db.delete_account(aid)
    await cb.answer("🗑 O'chirildi")
    await cb.message.answer("📋 <b>Akkauntlar:</b>", reply_markup=await _akkauntlar_ikb())


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
    await db.update_account_session(akk_id, malumot["sessiya"], malumot.get("username"))
    pool = await db.get_pool()
    await pool.execute("UPDATE accounts SET display_name=$1 WHERE id=$2", nom, akk_id)
    log.info(f"[nom_qabul] akk={akk_id} session DB ga saqlandi, len={len(malumot.get('sessiya',''))}")
    await state.clear()
    client = clients.get(akk_id)
    if client:
        await _handlerlarni_qoshish(akk_id, client)
        log.info(f"[nom_qabul] akk={akk_id} handler qo'shildi, client bor")
    else:
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
    replys   = await db.get_all_replies()
    soz_s    = await db.get_avto_sozlamalar()
    ishlaydi = sum(1 for t in auto_tasks.values() if not t.done())
    r_ishl   = sum(1 for t in reply_tasks.values() if not t.done())

    matn = "🗣 <b>Avto Xabar</b>\n\n"
    matn += "<b>Xabarlar ({} ta):</b>\n".format(len(sozlar))
    for s in sozlar[:5]:
        matn += "  {} {}\n".format("✅" if s["is_active"] else "❌", s["word"][:30])
    if len(sozlar) > 5:
        matn += "  ...va {} ta yana\n".format(len(sozlar)-5)
    matn += "\n<b>Interval:</b> {}-{} soniya\n".format(soz_s["min_interval"], soz_s["max_interval"])
    matn += "<b>Guruhga:</b> {} | <b>Lichkaga:</b> {}\n".format(
        "✅" if soz_s["guruh_aktiv"] else "❌",
        "✅" if soz_s["lichka_aktiv"] else "❌"
    )
    matn += "<b>Avto xabar:</b> {} ta | <b>Reply:</b> {} ta\n\n".format(ishlaydi, r_ishl)
    matn += "<b>Auto Reply ({} ta):</b>\n".format(len(replys))
    for r in replys[:3]:
        matn += "  {} \"{}\" → \"{}\"\n".format(
            "✅" if r["is_active"] else "❌",
            r["trigger"][:15], r["javob"][:20]
        )
    if len(replys) > 3:
        matn += "  ...va {} ta yana\n".format(len(replys)-3)

    await xavfsiz_tahrir(cb, matn, ikb([
        [("📝 Xabarlar", "ax:xabarlar"), ("🔄 Reply", "ax:reply")],
        [("⚙️ Sozlamalar", "ax:sozlamalar")],
        [("▶️ Boshlash", "avto:bosh"), ("⏹ To'xtatish", "avto:toxt")],
        [("⬅️ Orqaga", "m:bosh")],
    ]))


@dp.callback_query(F.data == "ax:xabarlar")
async def ax_xabarlar(cb: CallbackQuery):
    sozlar   = await db.get_all_words()
    qatorlar = ["{}. {} {}".format(s["id"], "✅" if s["is_active"] else "❌", s["word"]) for s in sozlar]
    matn     = "📝 <b>Xabarlar ro'yxati:</b>\n\n" + ("\n".join(qatorlar) if qatorlar else "Hech narsa yo'q.")
    await xavfsiz_tahrir(cb, matn, ikb([
        [("➕ Qo'sh", "sx:qosh"), ("🗑 O'chir", "sx:ochir")],
        [("⬅️ Orqaga", "m:avto")],
    ]))

@dp.callback_query(F.data == "sx:qosh")
async def soz_qosh(cb: CallbackQuery, state: FSMContext):
    await xavfsiz_tahrir(
        cb,
        "Yangi xabar matnini kiriting:\n\n<i>Bir nechta qo'shish uchun har satrga bitta yozing</i>"
    )
    await state.set_state(SozQoshish.soz)

@dp.message(SozQoshish.soz)
async def soz_saqlash(msg: Message, state: FSMContext):
    qatorlar = [q.strip() for q in msg.text.strip().split("\n") if q.strip()]
    for q in qatorlar:
        await db.add_word(q, msg.from_user.id)
    await state.clear()
    await msg.answer("✅ {} ta xabar qo'shildi".format(len(qatorlar)), reply_markup=asosiy_menyu())

@dp.callback_query(F.data == "sx:ochir")
async def soz_ochir_royxat(cb: CallbackQuery):
    sozlar = await db.get_all_words()
    if not sozlar: return await cb.answer("Xabar yo'q")
    qatorlar = [[(s["word"][:30], "sd:{}".format(s["id"]))] for s in sozlar]
    qatorlar.append([("⬅️ Orqaga", "ax:xabarlar")])
    await xavfsiz_tahrir(cb, "O'chirmoqchi bo'lganni tanlang:", ikb(qatorlar))

@dp.callback_query(F.data.startswith("sd:"))
async def soz_ochir(cb: CallbackQuery):
    await db.delete_word(int(cb.data.split(":")[1]))
    await cb.answer("🗑 O'chirildi")
    sozlar3 = await db.get_all_words()
    qatorlar5 = [[(s["word"][:30], f"sd:{s['id']}")]  for s in sozlar3]
    qatorlar5.append([("⬅️ Orqaga", "m:avto")])
    matn5 = "📝 <b>Xabarlar:</b>\n\n" + ("\n".join(f"{s['id']}. {s['word']}" for s in sozlar3) if sozlar3 else "Bo'sh")
    await xavfsiz_tahrir(cb, matn5, ikb(qatorlar5))


@dp.callback_query(F.data == "ax:sozlamalar")
async def ax_sozlamalar(cb: CallbackQuery):
    soz  = await db.get_avto_sozlamalar()
    matn = (
        "⚙️ <b>Avto xabar sozlamalari</b>\n\n"
        "Interval: <b>{}-{}</b> soniya\n"
        "Guruhga yuborish: <b>{}</b>\n"
        "Lichkaga yuborish: <b>{}</b>"
    ).format(
        soz["min_interval"], soz["max_interval"],
        "✅ Yoqilgan" if soz["guruh_aktiv"] else "❌ O'chirilgan",
        "✅ Yoqilgan" if soz["lichka_aktiv"] else "❌ O'chirilgan"
    )
    g_txt = "❌ Guruhni o'chir" if soz["guruh_aktiv"] else "✅ Guruhni yoq"
    l_txt = "❌ Lichkani o'chir" if soz["lichka_aktiv"] else "✅ Lichkani yoq"
    await xavfsiz_tahrir(cb, matn, ikb([
        [("⏱ Intervalni o'zgar", "ax:interval")],
        [(g_txt, "ax:toggle_guruh")],
        [(l_txt, "ax:toggle_lichka")],
        [("⬅️ Orqaga", "m:avto")],
    ]))

@dp.callback_query(F.data == "ax:toggle_guruh")
async def ax_toggle_guruh(cb: CallbackQuery):
    soz = await db.get_avto_sozlamalar()
    await db.update_avto_sozlamalar(guruh_aktiv=not soz["guruh_aktiv"])
    await cb.answer("✅ O'zgartirildi")
    await ax_sozlamalar(cb)

@dp.callback_query(F.data == "ax:toggle_lichka")
async def ax_toggle_lichka(cb: CallbackQuery):
    soz = await db.get_avto_sozlamalar()
    await db.update_avto_sozlamalar(lichka_aktiv=not soz["lichka_aktiv"])
    await cb.answer("✅ O'zgartirildi")
    await ax_sozlamalar(cb)

@dp.callback_query(F.data == "ax:interval")
async def ax_interval(cb: CallbackQuery, state: FSMContext):
    await xavfsiz_tahrir(cb, "Minimal intervalni kiriting (soniya):\n<i>Masalan: 30</i>")
    await state.set_state(AvtoXabarSozlash.min_interval)

@dp.message(AvtoXabarSozlash.min_interval)
async def ax_min_qabul(msg: Message, state: FSMContext):
    if not msg.text.strip().isdigit():
        return await msg.answer("❌ Faqat raqam kiriting.")
    await state.update_data(mn=int(msg.text.strip()))
    await state.set_state(AvtoXabarSozlash.max_interval)
    await msg.answer("Maksimal intervalni kiriting (soniya):")

@dp.message(AvtoXabarSozlash.max_interval)
async def ax_max_qabul(msg: Message, state: FSMContext):
    if not msg.text.strip().isdigit():
        return await msg.answer("❌ Faqat raqam kiriting.")
    malumot = await state.get_data()
    mn = malumot["mn"]
    mx = int(msg.text.strip())
    if mx <= mn:
        return await msg.answer("❌ Maksimal minimal dan katta bo'lishi kerak.")
    await db.update_avto_sozlamalar(min_interval=mn, max_interval=mx)
    await state.clear()
    await msg.answer("✅ Interval: {}-{} soniya".format(mn, mx), reply_markup=asosiy_menyu())


@dp.callback_query(F.data == "ax:reply")
async def ax_reply_menu(cb: CallbackQuery):
    replys = await db.get_all_replies()
    r_ishl = sum(1 for t in reply_tasks.values() if not t.done())
    qatorlar = []
    for r in replys:
        tur = {"guruh": "👥", "lichka": "👤", "both": "👥👤"}.get(r["tur"], "?")
        qatorlar.append("{} {} <code>{}</code> → {}".format(
            "✅" if r["is_active"] else "❌", tur,
            r["trigger"][:20], r["javob"][:25]
        ))
    matn = "🔄 <b>Auto Reply</b> (ishlaydi: {})\n\n".format(r_ishl)
    matn += "\n".join(qatorlar) if qatorlar else "Hech narsa yo'q."
    matn += "\n\n<i>Trigger so'z xabarda bo'lsa — akkunt avtomatik javob beradi</i>"
    await xavfsiz_tahrir(cb, matn, ikb([
        [("➕ Qo'sh", "rp:qosh"), ("🗑 O'chir", "rp:ochir")],
        [("▶️ Reply boshlash", "reply:bosh"), ("⏹ To'xtatish", "reply:toxt")],
        [("⬅️ Orqaga", "m:avto")],
    ]))

@dp.callback_query(F.data == "rp:qosh")
async def rp_qosh(cb: CallbackQuery, state: FSMContext):
    await xavfsiz_tahrir(
        cb,
        "➕ <b>Reply qo'shish</b>\n\n"
        "Trigger so'z kiriting:\n"
        "<i>Bu so'z xabarda bo'lsa akkunt javob beradi</i>\n\n"
        "Masalan: <code>narx</code>, <code>salom</code>, <code>price</code>"
    )
    await state.set_state(ReplyQoshish.trigger)

@dp.message(ReplyQoshish.trigger)
async def rp_trigger_qabul(msg: Message, state: FSMContext):
    await state.update_data(trigger=msg.text.strip().lower(), tur="both")
    await msg.answer(
        "Trigger: <code>{}</code>\n\nQayerga javob bersin?".format(msg.text.strip()),
        parse_mode="HTML",
        reply_markup=ikb([
            [("👥 Faqat guruh", "rpt:guruh"), ("👤 Faqat lichka", "rpt:lichka")],
            [("👥👤 Ikkalasi", "rpt:both")],
        ])
    )

@dp.callback_query(F.data.startswith("rpt:"), ReplyQoshish.trigger)
@dp.callback_query(F.data.startswith("rpt:"), ReplyQoshish.javob)
async def rp_tur_tanlash(cb: CallbackQuery, state: FSMContext):
    tur = cb.data.split(":")[1]
    await state.update_data(tur=tur)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Javob matnini yozing:")
    await state.set_state(ReplyQoshish.javob)

@dp.message(ReplyQoshish.javob)
async def rp_javob_qabul(msg: Message, state: FSMContext):
    malumot = await state.get_data()
    trigger = malumot.get("trigger", "")
    tur     = malumot.get("tur", "both")
    javob   = msg.text.strip()
    await db.add_reply(trigger, javob, tur)
    await state.clear()
    tur_txt = {"guruh": "Guruh", "lichka": "Lichka", "both": "Ikkalasi"}.get(tur, "?")
    await msg.answer(
        "✅ Reply qo'shildi!\nTrigger: <code>{}</code>\nJavob: {}\nTuri: {}".format(trigger, javob, tur_txt),
        parse_mode="HTML", reply_markup=asosiy_menyu()
    )

@dp.callback_query(F.data == "rp:ochir")
async def rp_ochir_royxat(cb: CallbackQuery):
    replys = await db.get_all_replies()
    if not replys: return await cb.answer("Reply yo'q")
    qatorlar = [[(
        "🗑 {} → {}".format(r["trigger"][:20], r["javob"][:20]),
        "rd:{}".format(r["id"])
    )] for r in replys]
    qatorlar.append([("⬅️ Orqaga", "ax:reply")])
    await xavfsiz_tahrir(cb, "O'chirmoqchi bo'lganni tanlang:", ikb(qatorlar))

@dp.callback_query(F.data.startswith("rd:"))
async def rp_ochir(cb: CallbackQuery):
    await db.delete_reply(int(cb.data.split(":")[1]))
    await cb.answer("🗑 O'chirildi")
    replys2 = await db.get_all_replies()
    if not replys2: return
    qatorlar3 = [[(f"🗑 {r['trigger'][:20]} → {r['javob'][:20]}", f"rd:{r['id']}")] for r in replys2]
    qatorlar3.append([("⬅️ Orqaga", "ax:reply")])
    await xavfsiz_tahrir(cb, "O'chirmoqchi bo'lganni tanlang:", ikb(qatorlar3))

@dp.callback_query(F.data == "reply:bosh")
async def reply_boshlash(cb: CallbackQuery):
    akkauntlar = await db.get_all_accounts()
    aktiv = [a for a in akkauntlar if a["status"] in ("idle", "busy")]
    if not aktiv: return await cb.answer("Aktiv akkunt yo'q")
    boshlandi = 0
    for akk in aktiv:
        if akk["id"] not in reply_tasks or reply_tasks[akk["id"]].done():
            reply_tasks[akk["id"]] = asyncio.create_task(_reply_tsikl(akk["id"]))
            boshlandi += 1
    await cb.answer("▶️ {} ta reply boshlandi".format(boshlandi))

@dp.callback_query(F.data == "reply:toxt")
async def reply_toxtatish(cb: CallbackQuery):
    for t in reply_tasks.values(): t.cancel()
    reply_tasks.clear()
    await cb.answer("⏹ Reply to'xtatildi")


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
    await cb.answer("▶️ {} ta boshlandi".format(boshlandi))

@dp.callback_query(F.data == "avto:toxt")
async def avto_toxtatish(cb: CallbackQuery):
    for t in auto_tasks.values(): t.cancel()
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
            soz_s     = await db.get_avto_sozlamalar()
            client    = await _get_client(akk_id)
            if not client or not client.is_connected():
                await asyncio.sleep(30)
                continue
            if soz_s["guruh_aktiv"]:
                for g in await db.get_monitored_groups():
                    try:
                        await client.send_message(g["group_id"], soz)
                        await db.add_log(akk_id, "avto_guruh", "{}: {}".format(g["group_name"], soz[:30]))
                    except Exception as e:
                        log.warning("avto guruh akk={}: {}".format(akk_id, e))
            if soz_s["lichka_aktiv"]:
                try:
                    dialogs = await client.get_dialogs(limit=20)
                    for d in dialogs:
                        if d.is_user and not d.entity.bot:
                            try:
                                await client.send_message(d.entity, soz)
                                await db.add_log(akk_id, "avto_lichka", str(d.entity.id))
                                await asyncio.sleep(random.randint(3, 8))
                            except Exception:
                                pass
                except Exception as e:
                    log.warning("avto lichka akk={}: {}".format(akk_id, e))
            mn = soz_s["min_interval"]
            mx = soz_s["max_interval"]
            await asyncio.sleep(random.randint(mn, mx))
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("avto_tsikl akk={}: {}".format(akk_id, e))
            await asyncio.sleep(60)


async def _reply_tsikl(akk_id: int):
    client = await _get_client(akk_id)
    if not client:
        return

    @client.on(events.NewMessage(incoming=True))
    async def reply_handler(event):
        try:
            replys = await db.get_active_replies()
            if not replys:
                return
            matn     = (event.message.text or "").lower()
            is_guruh = event.is_group or event.is_channel
            is_lch   = event.is_private
            for r in replys:
                if r["trigger"].lower() not in matn:
                    continue
                tur = r["tur"]
                if tur == "guruh"  and not is_guruh: continue
                if tur == "lichka" and not is_lch:   continue
                try:
                    await asyncio.sleep(random.randint(2, 8))
                    await event.reply(r["javob"])
                    await db.add_log(akk_id, "auto_reply",
                                     "trigger={} chat={}".format(r["trigger"], event.chat_id))
                    break
                except Exception as e:
                    log.warning("reply akk={}: {}".format(akk_id, e))
        except Exception as e:
            log.error("reply_handler akk={}: {}".format(akk_id, e))

    try:
        while True:
            await asyncio.sleep(60)
            if not client.is_connected():
                try: await client.connect()
                except Exception: pass
    except asyncio.CancelledError:
        try: client.remove_event_handler(reply_handler)
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════════════
# Video Chat — TO'G'RILANGAN
# ═══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "m:vc")
async def vc_menu(cb: CallbackQuery, state: FSMContext):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    bosh   = await db.get_accounts_by_status("idle")
    ichida = len(vc_sessions)
    tugmalar = [
        [("➕ Video chatga qo'shish", "vc:qosh")],
    ]
    if ichida > 0:
        tugmalar.append([("🔄 Boshqa VC ga ko'chirish", "vc:kochir")])
        tugmalar.append([("🚪 Hammasini chiqar", "vc:chiqar")])
    tugmalar.append([("⬅️ Orqaga", "m:bosh")])
    matn = (
        f"🎥 <b>Video Chat</b>\n\n"
        f"🟢 Bo'sh: {len(bosh)} ta\n"
        f"🔴 Video chatda: {ichida} ta\n"
    )
    if vc_sessions:
        matn += "\n<b>Hozir chatda:</b>\n"
        for s in list(vc_sessions.values())[:10]:
            matn += f"• {s['nom']}\n"
    await xavfsiz_tahrir(cb, matn, ikb(tugmalar))

@dp.callback_query(F.data == "vc:qosh")
async def vc_qosh(cb: CallbackQuery, state: FSMContext):
    if not await admin_mi(cb.from_user.id): return await cb.answer("Ruxsat yo'q")
    bosh = await db.get_accounts_by_status("idle")
    if not bosh:
        return await cb.answer("Bo'sh akkunt yo'q")
    await xavfsiz_tahrir(cb,
        f"🎥 Video chat linkini yuboring:\n"
        f"<code>t.me/username?videochat</code>\n"
        f"<code>t.me/username?videochat=HASH</code>\n"
        f"<code>t.me/+InviteKod?videochat</code>\n\n"
        f"🟢 Bo'sh: {len(bosh)} ta"
    )
    await state.set_state(VideoChat.link)

@dp.callback_query(F.data == "vc:chiqar")
async def vc_chiqar_hammasi(cb: CallbackQuery):
    """
    TO'G'RILANGAN: task.cancel() + gather() — chiqishni kutadi.
    finally bloki LeaveGroupCallRequest ni kafolat bilan yuboradi.
    """
    if not vc_sessions:
        return await cb.answer("Video chatda hech kim yo'q")

    soni = len(vc_sessions)
    await cb.answer(f"🚪 {soni} ta chiqarilmoqda...")

    tasks = [ses["task"] for ses in list(vc_sessions.values())]
    for ses in list(vc_sessions.values()):
        ses["task"].cancel()

    # Barcha finally bloklari ishlasin deb kutamiz
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    await xavfsiz_tahrir(cb,
        f"✅ {soni} ta akkunt video chatdan chiqarildi.",
        asosiy_menyu()
    )

@dp.callback_query(F.data == "vc:kochir")
async def vc_kochir_boshlash(cb: CallbackQuery, state: FSMContext):
    if not vc_sessions:
        return await cb.answer("Video chatda hech kim yo'q")
    n = len(vc_sessions)
    await xavfsiz_tahrir(cb,
        f"🔄 <b>Boshqa VC ga ko'chirish</b>\n\n"
        f"Hozir {n} ta akkunt video chatda\n\n"
        f"Yangi video chat linkini yuboring:"
    )
    await state.update_data(kochirish=True)
    await state.set_state(VideoChat.link)

@dp.message(VideoChat.link)
async def vc_link_qabul(msg: Message, state: FSMContext):
    link = msg.text.strip()
    if not link_parse(link):
        return await msg.answer(
            "❌ Link to'g'ri emas.\nFormat: <code>t.me/username?videochat</code>",
            parse_mode="HTML"
        )
    malumot   = await state.get_data()
    kochirish = malumot.get("kochirish", False)
    bosh      = await db.get_accounts_by_status("idle")
    n         = len(bosh)

    if kochirish:
        band_list = list(vc_sessions.values())
        nb        = len(band_list)
        await state.clear()
        await msg.answer(
            f"🔄 {nb} ta akkunt yangi chatga ko'chirilmoqda...",
            reply_markup=asosiy_menyu()
        )
        asyncio.create_task(_vc_kochirish_task(msg, band_list, link))
        return

    await state.update_data(vc_link=link)
    await state.set_state(VideoChat.son)
    await msg.answer(
        f"✅ Link qabul qilindi\n🟢 Bo'sh: {n} ta\n\n"
        f"Nechta akkunt kirsin?\n(Raqam yozing yoki tugma bosing)",
        parse_mode="HTML",
        reply_markup=ikb([
            [("🎥 5 ta","vcn:5"),("🎥 10 ta","vcn:10"),("🎥 15 ta","vcn:15")],
            [("🎥 Hammasi ({} ta)".format(n),"vcn:A")],
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


# ── VC core funksiyalar (TO'G'RILANGAN) ──────────────────────────────────────

async def _vc_join_one(client: TelegramClient, entity):
    """Bir akkuntni video chatga qo'shadi, call qaytaradi."""
    full_info = await client(GetFullChannelRequest(entity))
    full_chat = full_info.full_chat
    if not (hasattr(full_chat, "call") and full_chat.call):
        raise RuntimeError("Aktiv video chat topilmadi")
    call = full_chat.call
    ssrc = random.randint(100_000_000, 999_999_999)
    params = DataJSON(data=json.dumps({
        "ufrag": f"uf{ssrc}", "pwd": f"pw{ssrc}",
        "fingerprints": [{"hash": "sha-256", "fingerprint": "AA" * 32}],
        "ssrc": ssrc, "ssrc-groups": []
    }))
    me = await client.get_input_entity("me")
    await client(JoinGroupCallRequest(
        call=call, join_as=me, params=params,
        muted=True, video_stopped=True, invite_hash=None
    ))
    return call


async def _vc_leave_safely(client: TelegramClient, akk_id: int, chat_id: int, nom: str):
    """
    Akkuntni video chatdan chiqaradi.
    Har doim DB dan YANGI call olinadi — eskirgan ob'ektga tayanmaydi.
    """
    try:
        entity    = await client.get_entity(chat_id)
        full_info = await client(GetFullChannelRequest(entity))
        full_chat = full_info.full_chat
        if hasattr(full_chat, "call") and full_chat.call:
            await client(LeaveGroupCallRequest(call=full_chat.call, source=0))
            log.info(f"vc_leave: {nom} ✅ chiqdi")
        else:
            log.info(f"vc_leave: {nom} — call allaqachon tugagan")
    except Exception as e:
        log.warning(f"vc_leave {nom}: {e}")


async def _vc_keep_alive(akk_id: int, client: TelegramClient, call, chat_id: int, nom: str):
    """
    Akkuntni video chatda USHLAB TURADI.

    TO'G'RILANGAN:
    - finally blokida KAFOLATLANGAN chiqish (har qanday holatda)
    - Leave uchun DB dan YANGI call olinadi (eskirgan ob'ekt emas)
    - Admin task.cancel() bosdi — darhol chiqadi
    """
    log.info(f"vc_keep_alive BOSHLANDI: {nom} (chat={chat_id})")

    try:
        while True:
            # 60 soniya kutish — cancel kelsa shu yerda CancelledError ko'tariladi
            await asyncio.sleep(60)

            # Har daqiqada: hali VC da turibmizmi?
            try:
                entity    = await client.get_entity(chat_id)
                full_info = await client(GetFullChannelRequest(entity))
                full_chat = full_info.full_chat

                if not (hasattr(full_chat, "call") and full_chat.call):
                    log.info(f"vc: {nom} — call tugadi (serverdan)")
                    break

                # Participants tekshiruv
                me = await client.get_me()
                try:
                    gc_info = await client(GetGroupCallRequest(
                        call=full_chat.call, limit=500
                    ))
                    user_ids = [
                        p.peer.user_id
                        for p in gc_info.participants
                        if hasattr(p.peer, "user_id")
                    ]
                    if me.id not in user_ids:
                        log.warning(f"vc: {nom} calldan chiqib ketgan — qayta kirilmoqda...")
                        asyncio.create_task(
                            _vc_qayta_kirish(akk_id, client, chat_id, nom)
                        )
                except Exception:
                    pass

            except asyncio.CancelledError:
                raise  # Yuqoriga uzatamiz — finally ishlaydi
            except Exception as e:
                log.warning(f"vc check {nom}: {e} — davom")
                continue

    except asyncio.CancelledError:
        log.info(f"vc_keep_alive CANCEL: {nom}")

    finally:
        # ══════════════════════════════════════════════════════════════
        # KAFOLATLANGAN CHIQISH — har qanday holatda ishlaydi
        # task.cancel() → CancelledError → finally → Leave + tozalash
        # ══════════════════════════════════════════════════════════════
        log.info(f"vc_keep_alive FINALLY: {nom} — chiqilmoqda...")

        # DB dan yangi call olib chiqamiz (eskirgan ob'ekt emas!)
        await _vc_leave_safely(client, akk_id, chat_id, nom)

        # Holat va session tozalash
        await db.update_account_status(akk_id, "idle")
        vc_sessions.pop(akk_id, None)
        if akk_id in vc_ping_tasks:
            vc_ping_tasks[akk_id].pop(chat_id, None)

        await db.add_log(akk_id, "vc_chiqdi", "admin buyrug'i yoki call tugadi")
        log.info(f"vc_keep_alive TUGADI: {nom}")


async def _vc_qayta_kirish(akk_id: int, client: TelegramClient, chat_id: int, nom: str):
    """Akkunt VC dan chiqib ketsa — qayta kiradi."""
    await asyncio.sleep(2)
    for urinish in range(5):
        try:
            entity    = await client.get_entity(chat_id)
            full_info = await client(GetFullChannelRequest(entity))
            full_chat = full_info.full_chat
            if not (hasattr(full_chat, "call") and full_chat.call):
                log.info(f"vc qayta: {nom} — call tugagan, qayta kirish shart emas")
                return

            ssrc = random.randint(100_000_000, 999_999_999)
            params = DataJSON(data=json.dumps({
                "ufrag": f"uf{ssrc}", "pwd": f"pw{ssrc}",
                "fingerprints": [{"hash": "sha-256", "fingerprint": "AA" * 32}],
                "ssrc": ssrc, "ssrc-groups": []
            }))
            me = await client.get_input_entity("me")
            await client(JoinGroupCallRequest(
                call=full_chat.call, join_as=me, params=params,
                muted=True, video_stopped=True, invite_hash=None
            ))

            # keep_alive task yangilash
            if akk_id in vc_sessions:
                vc_sessions[akk_id]["call"] = full_chat.call
                old_task = vc_sessions[akk_id]["task"]
                if not old_task.done():
                    old_task.cancel()
                    await asyncio.sleep(0.5)
                new_task = asyncio.create_task(
                    _vc_keep_alive(akk_id, client, full_chat.call, chat_id, nom)
                )
                vc_sessions[akk_id]["task"] = new_task
                vc_ping_tasks[akk_id][chat_id] = new_task

            log.info(f"vc qayta: {nom} ✅ qayta kirdi ({urinish + 1}-urinish)")
            return
        except Exception as e:
            log.warning(f"vc qayta: {nom} {urinish + 1}-urinish xato: {e}")
            await asyncio.sleep(3 * (urinish + 1))
    log.error(f"vc qayta: {nom} 5 urinishdan keyin ham kira olmadi")


async def _vc_task(msg, akkauntlar: list, link: str):
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
            log.info(f"vc: {nom} — guruhga qo'shilmoqda...")
            entity = await guruhga_qoshil(client, link)
            if entity is None:
                raise RuntimeError("Entity topilmadi")

            log.info(f"vc: {nom} — video chatga kirilmoqda...")
            call     = await _vc_join_one(client, entity)
            group_id = entity.id

            task = asyncio.create_task(
                _vc_keep_alive(akk_id, client, call, group_id, nom)
            )
            vc_ping_tasks[akk_id][group_id] = task
            vc_sessions[akk_id] = {
                "task":    task,
                "chat_id": group_id,
                "call":    call,
                "nom":     nom,
                "akk_id":  akk_id,   # qayta kirish uchun kerak
            }

            await db.update_account_status(akk_id, "busy")
            await db.add_log(akk_id, "vc_kirdi", link)
            log.info(f"vc: {nom} ✅ VIDEO CHATDA")
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

        if ok + xato < len(akkauntlar):
            await asyncio.sleep(random.randint(8, 15))

    natija = f"🎥 <b>Video chat natija</b>\n✅ Kirdi: {ok}  ❌ Xato: {xato}"
    if xato_sabablari:
        natija += "\n\n<b>Tafsilotlar:</b>\n" + "\n".join(xato_sabablari[:5])
    try:
        await msg.answer(natija, reply_markup=asosiy_menyu(), parse_mode="HTML")
    except Exception:
        pass


async def _vc_task_by_id(msg, akkauntlar: list, chat_id: int):
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
            vc_sessions[akk_id] = {
                "task":    task,
                "chat_id": chat_id,
                "call":    call,
                "nom":     nom,
                "akk_id":  akk_id,
            }
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
            await asyncio.sleep(random.randint(8, 15))

    natija = f"🎥 <b>Video chat natija</b>\n✅ Kirdi: {ok}  ❌ Xato: {xato}"
    if xato_sabablari:
        natija += "\n" + "\n".join(xato_sabablari[:5])
    try:
        await msg.answer(natija, reply_markup=asosiy_menyu(), parse_mode="HTML")
    except Exception:
        pass


async def _vc_kochirish_task(msg, band_list: list, yangi_link: str):
    """Ko'chirish — TO'G'RILANGAN: chiqishni kutib keyin yangi chatga kiradi."""
    # 1. Hozirgi chatdan chiqarish va finally bloklarini kutish
    tasks = [ses["task"] for ses in band_list if not ses["task"].done()]
    for ses in band_list:
        ses["task"].cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(2)

    # 2. Yangi chatga kiritish
    band_nomlar = {s["nom"] for s in band_list}
    idle_akkauntlar = await db.get_accounts_by_status("idle")
    tanlangan = [
        a for a in idle_akkauntlar
        if (a["display_name"] or a["phone"]) in band_nomlar
    ]
    if not tanlangan:
        tanlangan = idle_akkauntlar[:len(band_list)]
    if not tanlangan:
        try:
            await msg.answer("❌ Ko'chirish uchun akkunt topilmadi.", reply_markup=asosiy_menyu())
        except Exception:
            pass
        return

    await _vc_task(msg, tanlangan, yangi_link)


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
    adminlar2 = await db.get_all_admins()
    qatorlar4 = [[(f"🗑 {a['username'] or a['telegram_id']}", f"rmadm:{a['telegram_id']}")] for a in adminlar2 if a["role"] != "main_admin"]
    if not qatorlar4: return
    qatorlar4.append([("⬅️ Orqaga", "m:sozlamalar")])
    await xavfsiz_tahrir(cb, "O'chirmoqchi bo'lgan adminni tanlang:", ikb(qatorlar4))


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

    @client.on(events.Raw())
    async def har_qanday_event(event):
        from telethon.tl.types import UpdateGroupCallParticipants
        if not isinstance(event, UpdateGroupCallParticipants):
            return
        try:
            for p in event.participants:
                if getattr(p, "left", False):
                    me = await client.get_me()
                    if p.peer and hasattr(p.peer, "user_id") and p.peer.user_id == me.id:
                        for aid, ses in list(vc_sessions.items()):
                            c2 = clients.get(aid)
                            if c2 is client:
                                nom = ses.get("nom", str(aid))
                                log.warning(f"vc: {nom} chiqib ketdi — qayta kirilmoqda...")
                                asyncio.create_task(
                                    _vc_qayta_kirish(aid, c2, ses["chat_id"], nom)
                                )
                                break
        except Exception:
            pass

    @client.on(events.Raw(UpdateGroupCall))
    async def vc_boshlandi(event):
        try:
            call    = event.call
            chat_id = getattr(event, "chat_id", None)
            if chat_id is None: return
            if not isinstance(call, GroupCall): return

            neg_chat_id = int(f"-100{chat_id}") if chat_id > 0 else chat_id
            discarded   = getattr(call, "discarded", False)

            # Video chat TUGADI
            if discarded:
                chiqarildi = 0
                for aid, ses in list(vc_sessions.items()):
                    if ses["chat_id"] in (chat_id, neg_chat_id, abs(chat_id)):
                        ses["task"].cancel()
                        chiqarildi += 1
                if chiqarildi:
                    log.info(f"VC tugadi chat={neg_chat_id}, {chiqarildi} akkunt chiqarildi")
                    await adminlarga_xabar(
                        f"📴 <b>Video chat tugadi</b>\n"
                        f"<b>{chiqarildi}</b> ta akkunt chiqarildi."
                    )
                return

            if getattr(call, "schedule_date", None): return

            # Video chat BOSHLANDI — deduplikatsiya
            xabar_kaliti = f"vc_xabar_{neg_chat_id}_{call.id}"
            if xabar_kaliti in _yuborilgan_vc_xabarlar:
                return
            _yuborilgan_vc_xabarlar.add(xabar_kaliti)
            if len(_yuborilgan_vc_xabarlar) > 100:
                _yuborilgan_vc_xabarlar.clear()

            guruhlar  = await db.get_monitored_groups()
            guruh     = next((g for g in guruhlar if g["group_id"] in (chat_id, neg_chat_id)), None)
            guruh_nom = guruh["group_name"] if guruh else None
            if not guruh_nom:
                try:
                    entity    = await client.get_entity(neg_chat_id)
                    guruh_nom = getattr(entity, "title", None) or str(neg_chat_id)
                except Exception:
                    guruh_nom = str(neg_chat_id)

            bosh = await db.get_accounts_by_status("idle")
            n    = len(bosh)
            eslatma = "" if guruh else "\n🟡 Monitoring da yo'q"
            await adminlarga_xabar(
                f"🎥 <b>Video chat boshlandi!</b>\n"
                f"📍 <b>{guruh_nom}</b>{eslatma}\n"
                f"🟢 Bo'sh: {n} ta\n\nQo'shish?",
                ikb([
                    [("🎥 5 ta",f"vcal:5|{neg_chat_id}"),("🎥 10 ta",f"vcal:10|{neg_chat_id}")],
                    [("🎥 Hammasi ({} ta)".format(n),f"vcal:A|{neg_chat_id}")],
                    [("❌ Kerak emas","vcal:yoq")],
                ])
            )
            log.info(f"VC boshlandi: {guruh_nom} ({neg_chat_id})")
        except Exception as e:
            log.error(f"vc_boshlandi xato: {e}", exc_info=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Ishga tushirish
# ═══════════════════════════════════════════════════════════════════════════════

async def _barcha_clientlarni_yukla():
    akkauntlar = await db.get_all_accounts()
    log.info(f"Jami {len(akkauntlar)} ta akkunt topildi")

    yuklanadi = [
        akk for akk in akkauntlar
        if akk["status"] not in ("paused", "failed")
        and (akk.get("session_string") or "").strip()
    ]
    yoq = len(akkauntlar) - len(yuklanadi)
    log.info(f"Session bor: {len(yuklanadi)}, yo'q: {yoq}")

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
        await asyncio.sleep(30)
        for akk in await db.get_all_accounts():
            if akk["status"] in ("paused",): continue
            akk_id = akk["id"]
            c = clients.get(akk_id)

            if c is None:
                if (akk.get("session_string") or "").strip():
                    log.info(f"[qayta] akk={akk_id} yuklanmagan, urinilmoqda...")
                    await _get_client(akk_id)
            elif not c.is_connected():
                log.warning(f"[qayta] akk={akk_id} uzilgan, qayta ulanmoqda...")
                try:
                    await c.connect()
                    log.info(f"[qayta] akk={akk_id} ✅ qayta ulandi")
                    if akk_id in vc_sessions:
                        ses = vc_sessions[akk_id]
                        log.info(f"[qayta] akk={akk_id} VC ga qaytarilmoqda...")
                        ses["task"].cancel()
                except Exception as e:
                    err = str(e)
                    if "AUTH_KEY_DUPLICATED" in err:
                        log.warning(f"[qayta] akk={akk_id} AUTH_KEY_DUPLICATED — 15s keyin qayta urinish")
                        del clients[akk_id]
                        await asyncio.sleep(15)
                        await _get_client(akk_id)
                    else:
                        log.error(f"[qayta] akk={akk_id}: {e}")

async def _proxy_auto_tsikl():
    await asyncio.sleep(120)
    while True:
        try:
            stat = await db.get_proxy_count()
            if stat["aktiv"] < 10:
                log.info("Proxy kam — avtomatik yangilanmoqda...")
                natija = await proxy_fetcher.proxies_yangilash(max_test=100)
                akkauntlar = await db.get_all_accounts()
                for akk in akkauntlar:
                    if not akk.get("proxy_id"):
                        p = await db.get_random_proxy()
                        if p:
                            await db.set_account_proxy(akk["id"], p["id"])
                log.info(f"Auto proxy: {natija['ishlaydigan']} ta yangi")
        except Exception as e:
            log.error(f"proxy_auto_tsikl: {e}")
        await asyncio.sleep(3 * 3600)

async def ishga_tushish():
    await db.init_db()
    await db.add_admin(config.MAIN_ADMIN_ID, "main_admin", "main_admin")
    await _barcha_clientlarni_yukla()
    asyncio.create_task(_qayta_ulanish_tsikl())
    asyncio.create_task(_proxy_auto_tsikl())
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
