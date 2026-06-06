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
            last_active    TIMESTAMP,
            created_at     TIMESTAMP NOT NULL DEFAULT NOW()
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
