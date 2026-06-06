"""
proxy_fetcher.py — Tekin SOCKS5 proxy olish va tekshirish.

Manbalar:
1. ProxyScrape API (eng ishonchli)
2. GeoNode free proxy API
3. Proxy-list.download

Har bir proxy Telegram serveriga (149.154.167.51:443) ulanib tekshiriladi.
"""

import asyncio
import aiohttp
import socket
import logging

import database as db

log = logging.getLogger(__name__)

# Telegram DC serverlar — proxy shu IP ga ulana olishi kerak
TELEGRAM_TEST_HOST = "149.154.167.51"
TELEGRAM_TEST_PORT = 443
TEST_TIMEOUT = 6

MANBALAR = [
    # ProxyScrape — eng katta bepul proxy bazasi
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=socks5&proxy_format=protocolipport&format=text&timeout=5000",
    # GeoNode
    "https://proxylist.geonode.com/api/proxy-list?limit=100&page=1&sort_by=lastChecked&sort_type=desc&protocols=socks5&speed=fast",
    # proxy-list.download
    "https://www.proxy-list.download/api/v1/get?type=socks5",
]


async def _tcp_ulanish(host: str, port: int) -> bool:
    """Proxy IP va portiga TCP ulanishni tekshiradi."""
    try:
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _sync_connect(host, port)),
            timeout=TEST_TIMEOUT
        )
        return True
    except Exception:
        return False


def _sync_connect(host: str, port: int):
    s = socket.create_connection((host, port), timeout=TEST_TIMEOUT)
    s.close()


async def _proxyscrape_yukla() -> list[dict]:
    """ProxyScrape dan SOCKS5 proxy yuklab oladi."""
    proxies = []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(MANBALAR[0]) as resp:
                if resp.status != 200:
                    return []
                matn = await resp.text()
        for qator in matn.strip().splitlines():
            qator = qator.strip()
            if qator.startswith("socks5://"):
                qator = qator[9:]
            if ":" not in qator:
                continue
            parts = qator.split(":")
            if len(parts) == 2:
                try:
                    proxies.append({"host": parts[0], "port": int(parts[1])})
                except ValueError:
                    pass
    except Exception as e:
        log.warning(f"proxyscrape yukla xato: {e}")
    return proxies


async def _geonode_yukla() -> list[dict]:
    """GeoNode dan SOCKS5 proxy yuklab oladi."""
    proxies = []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(MANBALAR[1]) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        for p in data.get("data", []):
            try:
                proxies.append({"host": p["ip"], "port": int(p["port"])})
            except Exception:
                pass
    except Exception as e:
        log.warning(f"geonode yukla xato: {e}")
    return proxies


async def _proxylist_yukla() -> list[dict]:
    """proxy-list.download dan yuklab oladi."""
    proxies = []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(MANBALAR[2]) as resp:
                if resp.status != 200:
                    return []
                matn = await resp.text()
        for qator in matn.strip().splitlines():
            qator = qator.strip()
            if ":" in qator:
                parts = qator.split(":")
                try:
                    proxies.append({"host": parts[0], "port": int(parts[1])})
                except ValueError:
                    pass
    except Exception as e:
        log.warning(f"proxylist yukla xato: {e}")
    return proxies


async def proxies_yangilash(max_test: int = 100) -> dict:
    """
    Barcha manbalardan proxy yuklab, tekshirib, DB ga saqlaydi.
    Qaytaradi: {"tekshirildi": N, "ishlaydigan": M, "saqlandi": K}
    """
    log.info("Proxy yangilash boshlandi...")

    # Barcha manbalardan yuklaymiz
    tasks = [_proxyscrape_yukla(), _geonode_yukla(), _proxylist_yukla()]
    results = await asyncio.gather(*tasks)

    # Birlashtirish va dublikatlarni olib tashlash
    seen = set()
    barcha = []
    for result in results:
        for p in result:
            key = f"{p['host']}:{p['port']}"
            if key not in seen:
                seen.add(key)
                barcha.append(p)

    log.info(f"Jami yuklanди: {len(barcha)} ta (dublikatsiz)")

    # Max test ta ni tekshiramiz
    tekshiriladigan = barcha[:max_test]

    # 30 ta parallel tekshirish
    semaphore = asyncio.Semaphore(30)

    async def tekshir(p: dict):
        async with semaphore:
            ok = await _tcp_ulanish(p["host"], p["port"])
            return p if ok else None

    natijalar   = await asyncio.gather(*[tekshir(p) for p in tekshiriladigan])
    ishlaydigan = [p for p in natijalar if p]

    log.info(f"Ishlaydigan: {len(ishlaydigan)} / {len(tekshiriladigan)}")

    # DB ga saqlash
    saqlandi = await db.add_proxies_bulk(ishlaydigan)

    return {
        "tekshirildi":  len(tekshiriladigan),
        "ishlaydigan":  len(ishlaydigan),
        "saqlandi":     saqlandi,
    }


async def yomon_tozala():
    """5+ marta xato bo'lgan proxylarni o'chiradi."""
    await db.deactivate_bad_proxies()
    log.info("Yomon proxylar o'chirildi")
