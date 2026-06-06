import asyncpg
from config import DATABASE_URL

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            ssl="disable",
            command_timeout=60,
            server_settings={"application_name": "autobot"},
        )
    return _pool


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id          SERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE NOT NULL,
            username    TEXT,
            role        TEXT NOT NULL DEFAULT 'admin',
            is_active   BOOL NOT NULL DEFAULT TRUE,
            created_at  TIMESTAMP NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS accounts (
            id             SERIAL PRIMARY KEY,
            phone          TEXT UNIQUE NOT NULL,
            display_name   TEXT,
            username       TEXT,
            status         TEXT NOT NULL DEFAULT 'idle',
            session_string TEXT,
            proxy_id       INT,
            last_active    TIMESTAMP,
            created_at     TIMESTAMP NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS proxies (
            id         SERIAL PRIMARY KEY,
            host       TEXT NOT NULL,
            port       INT  NOT NULL,
            username   TEXT,
            password   TEXT,
            protocol   TEXT NOT NULL DEFAULT 'socks5',
            is_active  BOOL NOT NULL DEFAULT TRUE,
            fail_count INT  NOT NULL DEFAULT 0,
            last_check TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            UNIQUE(host, port)
        );

        CREATE TABLE IF NOT EXISTS wordlists (
            id         SERIAL PRIMARY KEY,
            word       TEXT UNIQUE NOT NULL,
            is_active  BOOL NOT NULL DEFAULT TRUE,
            added_by   BIGINT,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS logs (
            id         SERIAL PRIMARY KEY,
            account_id INT,
            action     TEXT NOT NULL,
            detail     TEXT,
            status     TEXT DEFAULT 'ok',
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS monitored_groups (
            id         SERIAL PRIMARY KEY,
            group_id   BIGINT UNIQUE NOT NULL,
            group_name TEXT,
            link       TEXT,
            added_at   TIMESTAMP NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_accounts_status  ON accounts(status);
        CREATE INDEX IF NOT EXISTS idx_logs_created_at  ON logs(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_proxies_active   ON proxies(is_active);

        CREATE TABLE IF NOT EXISTS avto_sozlamalar (
            id           INT  PRIMARY KEY DEFAULT 1,
            min_interval INT  NOT NULL DEFAULT 30,
            max_interval INT  NOT NULL DEFAULT 120,
            guruh_aktiv  BOOL NOT NULL DEFAULT TRUE,
            lichka_aktiv BOOL NOT NULL DEFAULT FALSE
        );
        INSERT INTO avto_sozlamalar(id) VALUES(1) ON CONFLICT(id) DO NOTHING;

        CREATE TABLE IF NOT EXISTS reply_shablonlar (
            id         SERIAL PRIMARY KEY,
            trigger    TEXT NOT NULL,
            javob      TEXT NOT NULL,
            tur        TEXT NOT NULL DEFAULT 'both',
            is_active  BOOL NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """)


# ── Admins ────────────────────────────────────────────────────────────────────

async def get_admin(telegram_id: int):
    pool = await get_pool()
    return await pool.fetchrow(
        "SELECT * FROM admins WHERE telegram_id=$1 AND is_active=TRUE", telegram_id
    )

async def get_all_admins():
    pool = await get_pool()
    return await pool.fetch("SELECT * FROM admins WHERE is_active=TRUE ORDER BY id")

async def add_admin(telegram_id: int, username: str, role: str = "admin"):
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO admins(telegram_id, username, role) VALUES($1,$2,$3) "
        "ON CONFLICT(telegram_id) DO UPDATE SET is_active=TRUE, role=$3",
        telegram_id, username, role
    )

async def remove_admin(telegram_id: int):
    pool = await get_pool()
    await pool.execute(
        "UPDATE admins SET is_active=FALSE WHERE telegram_id=$1", telegram_id
    )


# ── Accounts ──────────────────────────────────────────────────────────────────

async def get_all_accounts():
    pool = await get_pool()
    return await pool.fetch("SELECT * FROM accounts ORDER BY id")

async def get_accounts_by_status(status: str):
    pool = await get_pool()
    return await pool.fetch("SELECT * FROM accounts WHERE status=$1", status)

async def get_account(account_id: int):
    pool = await get_pool()
    return await pool.fetchrow("SELECT * FROM accounts WHERE id=$1", account_id)

async def add_account(phone: str, display_name: str):
    pool = await get_pool()
    return await pool.fetchrow(
        "INSERT INTO accounts(phone, display_name) VALUES($1,$2) "
        "ON CONFLICT(phone) DO UPDATE SET display_name=$2 RETURNING id",
        phone, display_name
    )

async def update_account_status(account_id: int, status: str):
    pool = await get_pool()
    await pool.execute(
        "UPDATE accounts SET status=$1, last_active=NOW() WHERE id=$2",
        status, account_id
    )

async def update_account_session(account_id: int, session_string: str, username: str = None):
    pool = await get_pool()
    await pool.execute(
        "UPDATE accounts SET session_string=$1, username=$2, status='idle', last_active=NOW() WHERE id=$3",
        session_string, username, account_id
    )

async def delete_account(account_id: int):
    pool = await get_pool()
    await pool.execute("DELETE FROM accounts WHERE id=$1", account_id)

async def set_account_proxy(account_id: int, proxy_id: int | None):
    pool = await get_pool()
    await pool.execute("UPDATE accounts SET proxy_id=$1 WHERE id=$2", proxy_id, account_id)


# ── Proxies ───────────────────────────────────────────────────────────────────

async def get_all_proxies():
    pool = await get_pool()
    return await pool.fetch("SELECT * FROM proxies ORDER BY id")

async def get_active_proxies():
    pool = await get_pool()
    return await pool.fetch(
        "SELECT * FROM proxies WHERE is_active=TRUE AND fail_count < 5 ORDER BY fail_count ASC"
    )

async def get_random_proxy():
    """Eng kam xatolikli aktiv proxy qaytaradi."""
    pool = await get_pool()
    return await pool.fetchrow(
        "SELECT * FROM proxies WHERE is_active=TRUE AND fail_count < 5 "
        "ORDER BY fail_count ASC, RANDOM() LIMIT 1"
    )

async def get_proxy_by_id(proxy_id: int):
    pool = await get_pool()
    return await pool.fetchrow("SELECT * FROM proxies WHERE id=$1", proxy_id)

async def add_proxy(host: str, port: int, username: str = None,
                    password: str = None, protocol: str = "socks5"):
    pool = await get_pool()
    return await pool.fetchrow(
        "INSERT INTO proxies(host, port, username, password, protocol) "
        "VALUES($1,$2,$3,$4,$5) ON CONFLICT(host, port) DO UPDATE "
        "SET is_active=TRUE, fail_count=0 RETURNING id",
        host, port, username, password, protocol
    )

async def add_proxies_bulk(proxy_list: list[dict]) -> int:
    """Ko'p proxy bir vaqtda qo'shish. Qaytaradi: qo'shilganlar soni."""
    pool = await get_pool()
    count = 0
    for p in proxy_list:
        try:
            await pool.execute(
                "INSERT INTO proxies(host, port, username, password, protocol) "
                "VALUES($1,$2,$3,$4,$5) ON CONFLICT(host, port) DO UPDATE "
                "SET is_active=TRUE, fail_count=0",
                p["host"], p["port"], p.get("username"), p.get("password"),
                p.get("protocol", "socks5")
            )
            count += 1
        except Exception:
            pass
    return count

async def delete_proxy(proxy_id: int):
    pool = await get_pool()
    await pool.execute("DELETE FROM proxies WHERE id=$1", proxy_id)

async def mark_proxy_failed(proxy_id: int):
    pool = await get_pool()
    await pool.execute(
        "UPDATE proxies SET fail_count=fail_count+1, last_check=NOW() WHERE id=$1",
        proxy_id
    )

async def mark_proxy_ok(proxy_id: int):
    pool = await get_pool()
    await pool.execute(
        "UPDATE proxies SET fail_count=0, last_check=NOW() WHERE id=$1",
        proxy_id
    )

async def deactivate_bad_proxies():
    """5+ xato bo'lgan proxylarni o'chiradi."""
    pool = await get_pool()
    return await pool.execute(
        "UPDATE proxies SET is_active=FALSE WHERE fail_count >= 5"
    )

async def get_proxy_count() -> dict:
    pool = await get_pool()
    jami   = await pool.fetchval("SELECT COUNT(*) FROM proxies")
    aktiv  = await pool.fetchval("SELECT COUNT(*) FROM proxies WHERE is_active=TRUE AND fail_count < 5")
    yomon  = await pool.fetchval("SELECT COUNT(*) FROM proxies WHERE fail_count >= 5 OR is_active=FALSE")
    return {"jami": jami, "aktiv": aktiv, "yomon": yomon}


# ── Wordlists ─────────────────────────────────────────────────────────────────

async def get_active_words():
    pool = await get_pool()
    return await pool.fetch("SELECT word FROM wordlists WHERE is_active=TRUE")

async def get_all_words():
    pool = await get_pool()
    return await pool.fetch("SELECT * FROM wordlists ORDER BY id")

async def add_word(word: str, added_by: int):
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO wordlists(word, added_by) VALUES($1,$2) ON CONFLICT(word) DO NOTHING",
        word, added_by
    )

async def delete_word(word_id: int):
    pool = await get_pool()
    await pool.execute("DELETE FROM wordlists WHERE id=$1", word_id)

async def toggle_word(word_id: int, active: bool):
    pool = await get_pool()
    await pool.execute("UPDATE wordlists SET is_active=$1 WHERE id=$2", active, word_id)


# ── Monitored groups ──────────────────────────────────────────────────────────

async def get_monitored_groups():
    pool = await get_pool()
    return await pool.fetch("SELECT * FROM monitored_groups ORDER BY id")

async def add_monitored_group(group_id: int, group_name: str, link: str):
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO monitored_groups(group_id, group_name, link) VALUES($1,$2,$3) "
        "ON CONFLICT(group_id) DO UPDATE SET group_name=$2",
        group_id, group_name, link
    )

async def remove_monitored_group(group_id: int):
    pool = await get_pool()
    await pool.execute("DELETE FROM monitored_groups WHERE group_id=$1", group_id)


# ── Logs ──────────────────────────────────────────────────────────────────────

async def add_log(account_id: int | None, action: str, detail: str = "", status: str = "ok"):
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO logs(account_id, action, detail, status) VALUES($1,$2,$3,$4)",
        account_id, action, detail, status
    )


# ── Avto sozlamalar ───────────────────────────────────────────────────────────

async def get_avto_sozlamalar():
    pool = await get_pool()
    return await pool.fetchrow("SELECT * FROM avto_sozlamalar WHERE id=1")

async def update_avto_sozlamalar(**kwargs):
    pool = await get_pool()
    sets = ", ".join(f"{k}=${i+1}" for i, k in enumerate(kwargs))
    vals = list(kwargs.values()) + [1]
    await pool.execute(f"UPDATE avto_sozlamalar SET {sets} WHERE id=${len(vals)}", *vals)


# ── Reply shablonlar ──────────────────────────────────────────────────────────

async def get_all_replies():
    pool = await get_pool()
    return await pool.fetch("SELECT * FROM reply_shablonlar ORDER BY id")

async def get_active_replies():
    pool = await get_pool()
    return await pool.fetch("SELECT * FROM reply_shablonlar WHERE is_active=TRUE ORDER BY id")

async def add_reply(trigger: str, javob: str, tur: str = "both"):
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO reply_shablonlar(trigger, javob, tur) VALUES($1,$2,$3)",
        trigger.lower().strip(), javob, tur
    )

async def delete_reply(reply_id: int):
    pool = await get_pool()
    await pool.execute("DELETE FROM reply_shablonlar WHERE id=$1", reply_id)

async def toggle_reply(reply_id: int, active: bool):
    pool = await get_pool()
    await pool.execute("UPDATE reply_shablonlar SET is_active=$1 WHERE id=$2", active, reply_id)
