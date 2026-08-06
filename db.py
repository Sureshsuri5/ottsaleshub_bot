from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import aiosqlite

try:                                   # only needed when running on Postgres
    import asyncpg
except ImportError:                    # pragma: no cover
    asyncpg = None

log = logging.getLogger("db")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    tg_id       INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    balance     REAL    NOT NULL DEFAULT 0,
    is_banned   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT    NOT NULL UNIQUE,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS products (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id    INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    name           TEXT    NOT NULL,
    description    TEXT    NOT NULL DEFAULT '',
    price          REAL    NOT NULL,
    is_active      INTEGER NOT NULL DEFAULT 1,
    -- infinite=1 -> every buyer receives static_payload (ebook link, course, etc.)
    -- infinite=0 -> one unique line is popped from `stock` per unit sold
    infinite       INTEGER NOT NULL DEFAULT 0,
    static_payload TEXT    NOT NULL DEFAULT '',
    sold_count     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stock (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    payload    TEXT    NOT NULL,
    is_sold    INTEGER NOT NULL DEFAULT 0,
    order_id   INTEGER,
    added_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_stock_avail ON stock(product_id, is_sold);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(tg_id),
    kind            TEXT    NOT NULL DEFAULT 'purchase',   -- purchase | topup
    product_id      INTEGER REFERENCES products(id) ON DELETE SET NULL,
    product_name    TEXT    NOT NULL DEFAULT '',
    qty             INTEGER NOT NULL DEFAULT 1,
    amount          REAL    NOT NULL,                      -- in fiat
    provider        TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',
        -- pending | awaiting_review | paid | delivered | cancelled | expired | rejected
    pay_amount      REAL,        -- amount in the provider's own unit (USDT / Stars / INR)
    pay_unit        TEXT,
    pay_address     TEXT,
    external_ref    TEXT,        -- txid / charge id / UTR
    delivered_text  TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT,
    paid_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, provider);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, id DESC);

-- guards against replaying the same on-chain tx / webhook event
CREATE TABLE IF NOT EXISTS seen_tx (
    ref      TEXT PRIMARY KEY,
    order_id INTEGER,
    seen_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tiers (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT    NOT NULL UNIQUE,
    discount REAL    NOT NULL DEFAULT 0,     -- percent off the list price
    position INTEGER NOT NULL DEFAULT 0
);

-- optional exact price for one product on one tier; overrides the discount
CREATE TABLE IF NOT EXISTS tier_prices (
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    tier_id    INTEGER NOT NULL REFERENCES tiers(id) ON DELETE CASCADE,
    price      REAL    NOT NULL,
    PRIMARY KEY (product_id, tier_id)
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(tg_id),
    amount       REAL    NOT NULL,
    method       TEXT    NOT NULL,
    address      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',  -- pending | paid | rejected
    note         TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wd_status ON withdrawals(status, id DESC);

-- buyers waiting for a sold-out product to come back
CREATE TABLE IF NOT EXISTS waitlist (
    user_id    INTEGER NOT NULL REFERENCES users(tg_id),
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, product_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_conn: aiosqlite.Connection | None = None
_stock_lock = asyncio.Lock()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def in_minutes(m: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------- dialect layer
# Queries are written once in SQLite's dialect. When DATABASE_URL points at
# Postgres they're translated on the way out, so there is a single source of
# truth for every query rather than two that can drift apart.
_PG = False
_pool = None

# Errors that mean "this connection is gone", as opposed to "your SQL is
# wrong". Only these are retried: a query that failed on its merits would
# fail again, and a write retried after it had actually landed would double.
# asyncpg raises all of these *before* the statement reaches the server.
def _retryable() -> tuple:
    if asyncpg is None:
        return ()
    names = ("ConnectionDoesNotExistError", "InterfaceError",
             "CannotConnectNowError", "ConnectionFailureError")
    out = [getattr(asyncpg.exceptions, n) for n in names
           if hasattr(asyncpg.exceptions, n)]
    return tuple(out) + (ConnectionResetError,)


_RETRY: tuple = ()


async def _pg_run(fn, retries: int = 2):
    """Run `fn(connection)` on a pooled connection, retrying a dead one.

    Supabase recycles connections and restarts its pooler; without this, each
    of those surfaces as an error message in a buyer's chat rather than a
    momentary pause nobody notices.
    """
    last: Exception | None = None
    for attempt in range(retries):
        try:
            async with _pool.acquire() as con:
                return await fn(con)
        except _RETRY as e:
            last = e
            log.warning("database connection lost (%s) — retrying", type(e).__name__)
            await asyncio.sleep(0.3 * (attempt + 1))
    raise last  # type: ignore[misc]

_PG_SCHEMA_FIXES = (
    ("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY"),
    ("tg_id       INTEGER PRIMARY KEY", "tg_id       BIGINT PRIMARY KEY"),
    # every foreign key onto users(tg_id) has to be 64-bit as well, or the
    # child column silently stays int4 and rejects modern account ids
    ("user_id         INTEGER NOT NULL REFERENCES users(tg_id)",
     "user_id         BIGINT  NOT NULL REFERENCES users(tg_id)"),
    ("user_id      INTEGER NOT NULL REFERENCES users(tg_id)",
     "user_id      BIGINT  NOT NULL REFERENCES users(tg_id)"),
    ("user_id    INTEGER NOT NULL REFERENCES users(tg_id)",
     "user_id    BIGINT  NOT NULL REFERENCES users(tg_id)"),
    ("REAL", "DOUBLE PRECISION"),
    ("(datetime('now'))", "(to_char(now() at time zone 'utc',"
                          " 'YYYY-MM-DD HH24:MI:SS'))"),
    ("PRAGMA journal_mode = WAL;", ""),
    ("PRAGMA foreign_keys = ON;", ""),
)


def _pg_sql(sql: str) -> str:
    """SQLite SQL -> Postgres SQL. Only the differences this schema uses."""
    import re as _re
    out = sql
    # ? placeholders become $1, $2 ...
    n = 0
    def sub(_m):
        nonlocal n
        n += 1
        return f"${n}"
    out = _re.sub(r"\?", sub, out)
    out = out.replace("INSERT OR IGNORE", "INSERT")
    # Postgres has no round(double precision, int) — the money columns are
    # double precision, so every rounding needs an explicit numeric cast
    out = _re.sub(r"ROUND\(([^,()]+(?:\([^()]*\))?[^,()]*),\s*(\d+)\)",
                  r"ROUND(CAST(\1 AS numeric), \2)", out, flags=_re.I)
    # datetime('now', '-7 days') -> now() - interval '7 days'
    out = _re.sub(r"datetime\('now',\s*'-(\d+) (day|days|minute|minutes)'\)",
                  lambda m: f"to_char(now() at time zone 'utc' - interval "
                            f"'{m.group(1)} {m.group(2)}', "
                            f"'YYYY-MM-DD HH24:MI:SS')", out)
    # keep the parameter typed as text: asyncpg would otherwise try to encode
    # a Python str as an interval and fail
    out = _re.sub(r"datetime\('now',\s*\$(\d+)\)",
                  lambda m: "to_char(now() at time zone 'utc' + "
                            f"(${m.group(1)})::text::interval, "
                            "'YYYY-MM-DD HH24:MI:SS')", out)
    out = out.replace("datetime('now')",
                      "to_char(now() at time zone 'utc', 'YYYY-MM-DD HH24:MI:SS')")
    return out


async def init(path: str) -> None:
    """SQLite by default; Postgres when DATABASE_URL is set.

    Postgres is what you want anywhere the disk isn't yours — a free PaaS
    instance wipes SQLite on every restart, taking balances with it.
    """
    global _conn, _PG, _pool
    import os
    url = os.getenv("DATABASE_URL", "").strip()
    if url.startswith(("postgres://", "postgresql://")):
        if asyncpg is None:
            raise RuntimeError("DATABASE_URL is Postgres but asyncpg isn't installed")
        _PG = True
        global _RETRY
        _RETRY = _retryable()
        # Supabase's transaction pooler (port 6543) multiplexes one server
        # connection across clients, so asyncpg's prepared statements collide
        # with "prepared statement __asyncpg_stmt_N__ already exists" under
        # load — intermittently, which is the worst way to find out. Turning
        # the statement cache off is the supported way to run behind pgbouncer
        # in transaction mode.
        opts: dict = dict(min_size=1, max_size=8, command_timeout=30,
                          # Supabase drops idle connections; recycle ours first
                          # so the pool never hands out a dead one.
                          max_inactive_connection_lifetime=300)
        if ":6543" in url or "transaction" in url.lower():
            opts["statement_cache_size"] = 0
            log.info("transaction pooler detected — prepared statements disabled")
        try:
            _pool = await asyncpg.create_pool(url, **opts)
        except OSError as e:
            # Supabase's direct host is IPv6-only and most PaaS egress is IPv4.
            # Crash rather than fall back to SQLite: a silent fallback would
            # look like it worked and quietly lose every order.
            host = url.split("@")[-1].split("/")[0]
            raise RuntimeError(
                f"Cannot reach the database at {host} ({e}).\n"
                "If this is Supabase, the direct connection is IPv6-only and "
                "most hosts can't route to it. Use the Session pooler string "
                "instead — Connect -> Session pooler — which looks like\n"
                "  postgresql://postgres.PROJECTREF:PASSWORD"
                "@aws-0-REGION.pooler.supabase.com:5432/postgres\n"
                "Use port 5432 (session mode), not 6543."
            ) from e
        schema = SCHEMA
        for a, b in _PG_SCHEMA_FIXES:
            schema = schema.replace(a, b)
        await _pg_run(lambda con: con.execute(schema))
        await _migrate()
        return

    _conn = await aiosqlite.connect(path)
    _conn.row_factory = aiosqlite.Row
    await _conn.executescript(SCHEMA)
    await _migrate()


def backend() -> str:
    """What we're actually talking to, not what was configured.

    A status line that reports the setting rather than the connection can't
    tell you whether a migration took effect — which is the only moment you
    ever look at it.
    """
    if _PG and _pool is not None:
        import os
        url = os.getenv("DATABASE_URL", "")
        host = url.split("@")[-1].split("/")[0] if "@" in url else "postgres"
        return f"PostgreSQL — {host}"
    if _conn is not None:
        import os
        return f"SQLite — {os.getenv('DB_PATH', 'shop.db')} (lost on redeploy)"
    return "not connected"


async def close() -> None:
    """Shut the connection down cleanly on either engine."""
    global _conn, _pool
    if _PG and _pool is not None:
        await _pool.close()
        _pool = None
        return
    if _conn is not None:
        await _conn.close()
        _conn = None


async def _migrate() -> None:
    """Additive migrations, safe to re-run and safe on both engines.

    Each statement is tried independently: a column that already exists raises,
    and that's the expected outcome on every start after the first.
    """
    for stmt in (
        "ALTER TABLE users ADD COLUMN referred_by BIGINT",
        "ALTER TABLE users ADD COLUMN ref_earned REAL NOT NULL DEFAULT 0",
        "ALTER TABLE products ADD COLUMN emoji TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE products ADD COLUMN icon_emoji_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE products ADD COLUMN unit TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN notify_orders INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE users ADD COLUMN notify_promos INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE users ADD COLUMN notify_stock INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE users ADD COLUMN notify_referral INTEGER NOT NULL DEFAULT 1",
        # which version of the terms this buyer accepted, empty until they do
        "ALTER TABLE users ADD COLUMN terms_version TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN api_key TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE users ADD COLUMN ref_available REAL NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN ref_transferred REAL NOT NULL DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN code TEXT",
        "ALTER TABLE users ADD COLUMN tier_id INTEGER REFERENCES tiers(id)",
        "ALTER TABLE users ADD COLUMN activated INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE products ADD COLUMN keywords TEXT NOT NULL DEFAULT ''",
        # what actually arrived, when that differs from what was asked for
        "ALTER TABLE orders ADD COLUMN received REAL NOT NULL DEFAULT 0",
        # the shortfall the buyer was last told about, so a partial payment is
        # reported once rather than on every poll
        "ALTER TABLE orders ADD COLUMN short_notified REAL NOT NULL DEFAULT 0",
        # wallet balance already taken for this order, so a cancellation can
        # hand it back and a receipt can show the real total
        "ALTER TABLE orders ADD COLUMN balance_used REAL NOT NULL DEFAULT 0",
        # Postgres-only: widen id columns created before this was fixed. These
        # are no-ops on SQLite, which ignores column widths entirely.
        "ALTER TABLE orders ALTER COLUMN user_id TYPE BIGINT",
        "ALTER TABLE withdrawals ALTER COLUMN user_id TYPE BIGINT",
        "ALTER TABLE users ALTER COLUMN tg_id TYPE BIGINT",
        "ALTER TABLE users ALTER COLUMN referred_by TYPE BIGINT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_code ON orders(code)",
    ):
        try:
            await ex(stmt)
        except Exception:
            pass                       # already applied

    # anyone who already transacted counts as activated
    try:
        await ex("UPDATE users SET activated = 1 WHERE activated = 0 AND ("
                 "  balance > 0 OR tg_id IN (SELECT DISTINCT user_id FROM orders))")
    except Exception:
        pass


async def q(sql: str, args: Sequence[Any] = ()) -> list:
    if _PG:
        return await _pg_run(lambda con: con.fetch(_pg_sql(sql), *args))
    cur = await _conn.execute(sql, args)
    rows = await cur.fetchall()
    await cur.close()
    return rows


async def q1(sql: str, args: Sequence[Any] = ()):
    rows = await q(sql, args)
    return rows[0] if rows else None


def _dirty(sql: str) -> None:
    """Any write that mentions `settings` drops the cache — some callers write
    to it with a raw statement rather than through set_setting()."""
    if "settings" in sql.lower():
        invalidate_settings()


async def ex_many(sql: str, rows: Sequence[Sequence[Any]]) -> int:
    """Batch insert. One round trip on either engine."""
    if not rows:
        return 0
    _dirty(sql)
    if _PG:
        await _pg_run(lambda con: con.executemany(_pg_sql(sql), rows))
        return len(rows)
    await _conn.executemany(sql, rows)
    await _conn.commit()
    return len(rows)


async def ex_count(sql: str, args: Sequence[Any] = ()) -> int:
    """Run a DELETE/UPDATE and report how many rows it touched."""
    _dirty(sql)
    if _PG:
        tag = await _pg_run(lambda con: con.execute(_pg_sql(sql), *args))
        # asyncpg returns a status like "DELETE 3"
        try:
            return int(str(tag).rsplit(" ", 1)[-1])
        except ValueError:
            return 0
    cur = await _conn.execute(sql, args)
    await _conn.commit()
    n = cur.rowcount
    await cur.close()
    return n


# Tables whose INSERTs return a generated id the caller needs back.
_ID_TABLES = {"categories", "products", "stock", "orders", "withdrawals", "tiers"}


async def ex(sql: str, args: Sequence[Any] = ()) -> int:
    """Run a statement. Returns the new row id for INSERTs that generate one."""
    _dirty(sql)
    if _PG:
        import re as _re
        sql_pg = _pg_sql(sql)
        m = _re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql_pg, _re.I)
        if m and m.group(1).lower() in _ID_TABLES \
                and "RETURNING" not in sql_pg.upper():
            row = await _pg_run(
                lambda con: con.fetchrow(sql_pg + " RETURNING id", *args))
            return row["id"] if row else 0
        await _pg_run(lambda con: con.execute(sql_pg, *args))
        return 0
    cur = await _conn.execute(sql, args)
    await _conn.commit()
    lastrow = cur.lastrowid
    await cur.close()
    return lastrow


# ---------------------------------------------------------------- users
async def upsert_user(tg_id: int, username: str | None, first_name: str | None) -> aiosqlite.Row:
    await ex(
        """INSERT INTO users (tg_id, username, first_name) VALUES (?, ?, ?)
           ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username,
                                            first_name = excluded.first_name""",
        (tg_id, username, first_name),
    )
    return await q1("SELECT * FROM users WHERE tg_id = ?", (tg_id,))


async def get_user(tg_id: int):
    return await q1("SELECT * FROM users WHERE tg_id = ?", (tg_id,))


async def find_user(term: str):
    term = term.strip().lstrip("@")
    if term.isdigit():
        return await q1("SELECT * FROM users WHERE tg_id = ?", (int(term),))
    return await q1("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (term,))


async def add_balance(tg_id: int, delta: float) -> None:
    await ex("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE tg_id = ?", (delta, tg_id))


async def set_ban(tg_id: int, banned: bool) -> None:
    await ex("UPDATE users SET is_banned = ? WHERE tg_id = ?", (1 if banned else 0, tg_id))


def ref_code(tg_id: int) -> str:
    """Public referral code. Derived from the id but not reversible to it, so a
    shared link doesn't hand out the referrer's Telegram account id."""
    import hashlib
    import hmac as _hmac
    import os as _os
    token = _os.getenv("BOT_TOKEN", "")
    return _hmac.new(token.encode(), f"ref{tg_id}".encode(),
                     hashlib.sha256).hexdigest()[:8]


async def user_by_ref_code(code: str):
    code = (code or "").strip().lower()
    if not code:
        return None
    if code.isdigit():                       # legacy links carried the raw id
        return await get_user(int(code))
    for r in await q("SELECT tg_id FROM users"):
        if ref_code(r["tg_id"]) == code:
            return await get_user(r["tg_id"])
    return None


async def set_referrer(uid: int, ref_id: int) -> bool:
    """Attach a referrer. Only ever set once, never to yourself."""
    if uid == ref_id:
        return False
    u = await get_user(uid)
    if not u or u["referred_by"]:
        return False
    if not await get_user(ref_id):
        return False
    await ex("UPDATE users SET referred_by = ? WHERE tg_id = ?", (ref_id, uid))
    return True


async def referral_stats(uid: int) -> dict:
    invited = (await q1("SELECT COUNT(*) c FROM users WHERE referred_by = ?", (uid,)))["c"]
    day = (await q1("SELECT COUNT(*) c FROM users WHERE referred_by = ? "
                    "AND created_at >= datetime('now', '-1 day')", (uid,)))["c"]
    week = (await q1("SELECT COUNT(*) c FROM users WHERE referred_by = ? "
                     "AND created_at >= datetime('now', '-7 days')", (uid,)))["c"]
    buyers = (await q1(
        "SELECT COUNT(DISTINCT o.user_id) c FROM orders o JOIN users u ON u.tg_id = o.user_id "
        "WHERE u.referred_by = ? AND o.status = 'delivered' AND o.kind = 'purchase'", (uid,)))["c"]
    u = await get_user(uid)
    return {"invited": invited, "day": day, "week": week, "buyers": buyers,
            "earned": u["ref_earned"] if u else 0,
            "available": u["ref_available"] if u else 0,
            "transferred": u["ref_transferred"] if u else 0}


async def credit_referral(uid: int, amount: float) -> None:
    """Referral money lands in its own pot, not the spendable wallet, so the
    referrer sees what the programme earned them separately from what they
    deposited."""
    await ex("UPDATE users SET ref_earned = ROUND(ref_earned + ?, 2), "
             "ref_available = ROUND(ref_available + ?, 2) WHERE tg_id = ?",
             (amount, amount, uid))


async def transfer_referral(uid: int) -> float:
    u = await get_user(uid)
    amount = round(u["ref_available"], 2) if u else 0.0
    if amount <= 0:
        return 0.0
    await ex("UPDATE users SET balance = ROUND(balance + ?, 2), ref_available = 0, "
             "ref_transferred = ROUND(ref_transferred + ?, 2) WHERE tg_id = ?",
             (amount, amount, uid))
    return amount


async def delivered_purchases(uid: int) -> int:
    row = await q1("SELECT COUNT(*) c FROM orders WHERE user_id = ? AND status = 'delivered' "
                   "AND kind = 'purchase'", (uid,))
    return row["c"] if row else 0


async def all_user_ids(promos_only: bool = False) -> list[int]:
    """Broadcast audience. Anyone who muted announcements is left out — an
    opt-out that is quietly ignored is worse than not offering one."""
    sql = "SELECT tg_id FROM users WHERE is_banned = 0"
    if promos_only:
        sql += " AND notify_promos = 1"
    return [r["tg_id"] for r in await q(sql)]


async def activate(tg_id: int) -> None:
    await ex("UPDATE users SET activated = 1 WHERE tg_id = ?", (tg_id,))


async def set_terms_version(uid: int, version: str) -> None:
    await ex("UPDATE users SET terms_version = ? WHERE tg_id = ?", (version, uid))


async def set_notify(tg_id: int, field: str, on: bool) -> None:
    if field not in {"notify_orders", "notify_promos", "notify_stock",
                     "notify_referral"}:
        return
    await ex(f"UPDATE users SET {field} = ? WHERE tg_id = ?", (1 if on else 0, tg_id))


# ------------------------------------------------------------- api keys
async def issue_api_key(tg_id: int) -> str:
    import secrets
    key = "sk_" + secrets.token_urlsafe(24)
    await ex("UPDATE users SET api_key = ? WHERE tg_id = ?", (key, tg_id))
    return key


async def user_by_api_key(key: str):
    if not key or not key.startswith("sk_"):
        return None
    return await q1("SELECT * FROM users WHERE api_key = ?", (key,))


# ---------------------------------------------------------- withdrawals
async def create_withdrawal(uid: int, amount: float, method: str, address: str) -> int:
    return await ex("INSERT INTO withdrawals (user_id, amount, method, address) "
                    "VALUES (?, ?, ?, ?)", (uid, amount, method, address))


async def withdrawal(wid: int):
    return await q1("SELECT * FROM withdrawals WHERE id = ?", (wid,))


async def user_withdrawals(uid: int, limit: int = 10):
    return await q("SELECT * FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                   (uid, limit))


async def pending_withdrawals():
    return await q("SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY id")


async def set_withdrawal(wid: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    await ex(f"UPDATE withdrawals SET {cols} WHERE id = ?", (*fields.values(), wid))


async def locked_balance(uid: int) -> float:
    """Funds already claimed by a pending payout — not spendable."""
    r = await q1("SELECT COALESCE(SUM(amount), 0) s FROM withdrawals "
                 "WHERE user_id = ? AND status = 'pending'", (uid,))
    return r["s"] if r else 0.0


# ----------------------------------------------------------- categories
async def categories() -> list[aiosqlite.Row]:
    return await q("SELECT * FROM categories ORDER BY position, name")


async def category(cid: int):
    return await q1("SELECT * FROM categories WHERE id = ?", (cid,))


async def add_category(name: str) -> int:
    return await ex("INSERT INTO categories (name) VALUES (?)", (name,))


async def del_category(cid: int) -> None:
    await ex("DELETE FROM categories WHERE id = ?", (cid,))


# ---------------------------------------------------------------- tiers
async def tiers() -> list:
    return await q("SELECT * FROM tiers ORDER BY position, discount, name")


async def tier(tid: int | None):
    return await q1("SELECT * FROM tiers WHERE id = ?", (tid,)) if tid else None


async def add_tier(name: str, discount: float) -> int:
    return await ex("INSERT INTO tiers (name, discount) VALUES (?, ?)", (name, discount))


async def personal_tier(uid: int) -> int:
    """A price list belonging to one buyer.

    Implemented as a tier with a single member, so per-user and per-group
    pricing share one code path — there's no second pricing system to keep
    in step with the first.
    """
    u = await get_user(uid)
    if u and u["tier_id"]:
        t = await tier(u["tier_id"])
        if t and t["name"].startswith("@"):
            return t["id"]
    label = f"@{u['username']}" if u and u["username"] else f"@{uid}"
    name, n = label, 1
    while await q1("SELECT 1 FROM tiers WHERE name = ?", (name,)):
        n += 1
        name = f"{label} ({n})"
    tid = await add_tier(name, 0)
    await set_user_tier(uid, tid)
    return tid


async def update_tier(tid: int, **f) -> None:
    cols = ", ".join(f"{k} = ?" for k in f)
    await ex(f"UPDATE tiers SET {cols} WHERE id = ?", (*f.values(), tid))


async def del_tier(tid: int) -> None:
    await ex("UPDATE users SET tier_id = NULL WHERE tier_id = ?", (tid,))
    await ex("DELETE FROM tiers WHERE id = ?", (tid,))


async def set_user_tier(uid: int, tid: int | None) -> None:
    await ex("UPDATE users SET tier_id = ? WHERE tg_id = ?", (tid, uid))


async def tier_members(tid: int) -> int:
    r = await q1("SELECT COUNT(*) c FROM users WHERE tier_id = ?", (tid,))
    return r["c"] if r else 0


async def set_tier_price(pid: int, tid: int, price: float | None) -> None:
    if price is None:
        await ex("DELETE FROM tier_prices WHERE product_id = ? AND tier_id = ?", (pid, tid))
        return
    await ex("INSERT INTO tier_prices (product_id, tier_id, price) VALUES (?, ?, ?) "
             "ON CONFLICT(product_id, tier_id) DO UPDATE SET price = excluded.price",
             (pid, tid, price))


async def tier_prices(pid: int) -> dict[int, float]:
    rows = await q("SELECT tier_id, price FROM tier_prices WHERE product_id = ?", (pid,))
    return {r["tier_id"]: r["price"] for r in rows}


# ------------------------------------------------------------- products
async def products(category_id: int | None = None, only_active: bool = True):
    sql = "SELECT * FROM products WHERE 1=1"
    args: list[Any] = []
    if category_id is not None:
        sql += " AND category_id = ?"
        args.append(category_id)
    if only_active:
        sql += " AND is_active = 1"
    return await q(sql + " ORDER BY id", args)


async def product(pid: int):
    return await q1("SELECT * FROM products WHERE id = ?", (pid,))


async def add_product(category_id: int, name: str, description: str, price: float) -> int:
    return await ex(
        "INSERT INTO products (category_id, name, description, price) VALUES (?, ?, ?, ?)",
        (category_id, name, description, price),
    )


async def update_product(pid: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    await ex(f"UPDATE products SET {cols} WHERE id = ?", (*fields.values(), pid))


async def del_product(pid: int) -> None:
    await ex("DELETE FROM products WHERE id = ?", (pid,))


async def stock_count(pid: int) -> int:
    row = await q1("SELECT COUNT(*) c FROM stock WHERE product_id = ? AND is_sold = 0", (pid,))
    return row["c"] if row else 0


async def available(pid: int) -> int:
    """Units purchasable right now. Infinite products report a large number."""
    p = await product(pid)
    if not p:
        return 0
    return 10**6 if p["infinite"] else await stock_count(pid)


async def add_stock(pid: int, lines: Iterable[str]) -> int:
    rows = [(pid, ln.strip()) for ln in lines if ln.strip()]
    if not rows:
        return 0
    return await ex_many("INSERT INTO stock (product_id, payload) VALUES (?, ?)", rows)


async def purge_sold(pid: int) -> int:
    return await ex_count("DELETE FROM stock WHERE product_id = ? AND is_sold = 1", (pid,))


async def allocate_stock(pid: int, qty: int, order_id: int) -> list[str] | None:
    """Atomically reserve `qty` unsold lines. Returns payloads or None if short."""
    async with _stock_lock:
        rows = await q(
            "SELECT id, payload FROM stock WHERE product_id = ? AND is_sold = 0 ORDER BY id LIMIT ?",
            (pid, qty),
        )
        if len(rows) < qty:
            return None
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        await ex(
            f"UPDATE stock SET is_sold = 1, order_id = ? WHERE id IN ({marks})",
            (order_id, *ids),
        )
        return [r["payload"] for r in rows]


# --------------------------------------------------------------- orders
def order_code(oid: int, salt: int = 0) -> str:
    """Short public reference for an order.

    Derived from the row id with the bot token, so it isn't a sequential number
    that reveals how many orders the shop has taken, and can't be enumerated by
    someone guessing neighbours.
    """
    import hashlib
    import hmac as _hmac
    import os as _os
    mac = _hmac.new(_os.getenv("BOT_TOKEN", "").encode(),
                    f"order{oid}:{salt}".encode(), hashlib.sha256)
    return mac.hexdigest()[:4].upper()


async def create_order(**f) -> int:
    cols = ", ".join(f)
    marks = ",".join("?" * len(f))
    oid = await ex(f"INSERT INTO orders ({cols}) VALUES ({marks})", tuple(f.values()))
    for salt in range(50):                     # widen on the rare collision
        code = order_code(oid, salt) if salt < 25 else order_code(oid, salt) + str(salt)
        try:
            await ex("UPDATE orders SET code = ? WHERE id = ?", (code, oid))
            break
        except Exception:
            continue
    return oid


async def order_by_code(code: str, user_id: int | None = None):
    code = (code or "").strip().lstrip("#").upper()
    if not code:
        return None
    sql = "SELECT * FROM orders WHERE code = ?"
    args: list[Any] = [code]
    if user_id is not None:
        sql += " AND user_id = ?"
        args.append(user_id)
    return await q1(sql, args)


async def backfill_codes() -> int:
    """Give pre-existing orders a code so old history stays openable."""
    rows = await q("SELECT id FROM orders WHERE code IS NULL OR code = ''")
    for r in rows:
        for salt in range(50):
            try:
                await ex("UPDATE orders SET code = ? WHERE id = ?",
                         (order_code(r["id"], salt), r["id"]))
                break
            except Exception:
                continue
    return len(rows)


async def order(oid: int):
    return await q1("SELECT * FROM orders WHERE id = ?", (oid,))


async def set_order(oid: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    await ex(f"UPDATE orders SET {cols} WHERE id = ?", (*fields.values(), oid))


# What "my orders" means to a buyer: things they actually received. A pending
# or cancelled row is checkout state, not history.
DELIVERED = "status = 'delivered' AND kind = 'purchase'"


async def user_orders(uid: int, limit: int = 15, offset: int = 0,
                      delivered_only: bool = False):
    where = f"user_id = ?{' AND ' + DELIVERED if delivered_only else ''}"
    return await q(f"SELECT * FROM orders WHERE {where} ORDER BY id DESC "
                   "LIMIT ? OFFSET ?", (uid, limit, offset))


async def count_user_orders(uid: int, delivered_only: bool = False) -> int:
    where = f"user_id = ?{' AND ' + DELIVERED if delivered_only else ''}"
    r = await q1(f"SELECT COUNT(*) c FROM orders WHERE {where}", (uid,))
    return r["c"] if r else 0


async def prune_dead_orders(days: int) -> int:
    """Drop abandoned checkout rows after a while.

    Only ever touches cancelled / expired / rejected orders — anything
    delivered, paid or still open is left alone, so nothing a buyer or the
    accounts depend on can disappear.
    """
    if days <= 0:
        return 0
    # Last chance to hand back anything still held. Every path that closes an
    # order releases its balance already, but deleting the row destroys the
    # only record that it was owed — so check once more before it goes.
    stuck = await q(
        "SELECT id FROM orders WHERE status IN ('cancelled','expired','rejected') "
        "AND COALESCE(balance_used, 0) > 0 AND created_at < datetime('now', ?)",
        (f"-{days} days",))
    for row in stuck:
        log.warning("order %s still held wallet balance at prune time", row["id"])
        await release_balance(row["id"])
    return await ex_count(
        "DELETE FROM orders WHERE status IN ('cancelled','expired','rejected') "
        "AND created_at < datetime('now', ?)", (f"-{days} days",))


async def open_orders(provider: str | None = None):
    """Oldest first: when two orders ask for the same amount, the one that has
    been waiting longest is credited first."""
    sql = "SELECT * FROM orders WHERE status = 'pending'"
    args: list[Any] = []
    if provider:
        sql += " AND provider = ?"
        args.append(provider)
    return await q(sql + " ORDER BY id", args)


async def late_orders(provider: str | None = None, hours: int = 48):
    """Recently closed orders whose deposit address is still worth watching.

    A buyer who pays after their order expired has sent real money to an
    address only they were ever shown. Nothing else would notice it: the
    watcher polls pending orders, and this one isn't pending any more. Keeping
    the address in the sweep for a couple of days turns a support ticket into
    an automatic wallet credit.
    """
    sql = ("SELECT * FROM orders WHERE status IN ('expired', 'cancelled') "
           "AND pay_address IS NOT NULL AND pay_address != '' "
           "AND created_at > datetime('now', ?)")
    args: list[Any] = [f"-{max(1, hours)} hours"]
    if provider:
        sql += " AND provider = ?"
        args.append(provider)
    return await q(sql + " ORDER BY id", args)


async def pending_reviews():
    return await q("SELECT * FROM orders WHERE status = 'awaiting_review' ORDER BY id")


async def expire_stale() -> list[int]:
    rows = await q(
        "SELECT id FROM orders WHERE status = 'pending' AND expires_at IS NOT NULL "
        "AND expires_at < datetime('now') AND COALESCE(received, 0) = 0"
    )
    ids = [r["id"] for r in rows]
    if ids:
        marks = ",".join("?" * len(ids))
        await ex(f"UPDATE orders SET status = 'expired' WHERE id IN ({marks})", ids)
    return ids


_deriv_lock = asyncio.Lock()


async def release_balance(oid: int) -> float:
    """Give back any wallet balance an unfinished order was holding.

    Zeroed in the same breath as the refund: an order that gets cancelled and
    then expires must not pay the buyer twice.
    """
    o = await q1("SELECT balance_used FROM orders WHERE id = ?", (oid,))
    used = float(o["balance_used"] or 0) if o else 0.0
    if used < 0.01:
        return 0.0
    row = await q1("SELECT user_id FROM orders WHERE id = ?", (oid,))
    await ex("UPDATE orders SET balance_used = 0 WHERE id = ?", (oid,))
    if row:
        await add_balance(row["user_id"], used)
    return used


async def next_deriv_index() -> int:
    """Hand out the next HD derivation index, once and only once.

    Kept in `settings` rather than derived from the orders table: pruning
    abandoned orders would otherwise lower the maximum and start reissuing
    addresses that buyers had already been shown.
    """
    async with _deriv_lock:
        try:
            cur = int(await setting("hd:next_index", "0") or 0)
        except ValueError:
            cur = 0
        await set_setting("hd:next_index", str(cur + 1))
        return cur


async def amount_taken(amount: float, unit: str) -> bool:
    """Is another live order already waiting on this exact amount?"""
    row = await q1(
        "SELECT 1 FROM orders WHERE status = 'pending' AND pay_unit = ? "
        "AND ABS(pay_amount - ?) < 0.0000005",
        (unit, amount),
    )
    return row is not None


# --------------------------------------------------------------- seen tx
async def mark_seen(ref: str, order_id: int | None = None) -> bool:
    """Returns True if this ref is new (and now recorded)."""
    try:
        await ex("INSERT INTO seen_tx (ref, order_id) VALUES (?, ?)", (ref, order_id))
        return True
    except Exception as e:
        # SQLite raises IntegrityError, asyncpg UniqueViolationError — either
        # way a duplicate ref means this payment was already counted
        if "unique" in type(e).__name__.lower() or "unique" in str(e).lower() \
                or "duplicate" in str(e).lower() or "integrity" in type(e).__name__.lower():
            return False
        raise


# -------------------------------------------------------------- waitlist
async def watch_product(uid: int, pid: int) -> bool:
    """Ask to be told when this product is back. Returns False if already on
    the list, so the caller can say so rather than claiming a second success."""
    try:
        await ex("INSERT INTO waitlist (user_id, product_id) VALUES (?, ?)", (uid, pid))
        return True
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            return False
        raise


async def unwatch_product(uid: int, pid: int) -> None:
    await ex("DELETE FROM waitlist WHERE user_id = ? AND product_id = ?", (uid, pid))


async def is_watching(uid: int, pid: int) -> bool:
    return bool(await q1("SELECT 1 FROM waitlist WHERE user_id = ? AND product_id = ?",
                         (uid, pid)))


async def watched_products() -> list[int]:
    rows = await q("SELECT DISTINCT product_id FROM waitlist")
    return [r["product_id"] for r in rows]


async def take_watchers(pid: int) -> list[int]:
    """Everyone waiting on this product, removed from the list as they're read.

    Taken rather than read: the notification goes out once, and a buyer who
    misses it can join the list again. Leaving rows behind would mean a second
    restock re-notifies people who already had their chance.
    """
    rows = await q("SELECT user_id FROM waitlist WHERE product_id = ?", (pid,))
    await ex("DELETE FROM waitlist WHERE product_id = ?", (pid,))
    return [r["user_id"] for r in rows]


# -------------------------------------------------------------- settings
# The settings table is read constantly — every message rendered walks the
# whole flair slot list and every editable string is a lookup — and it changes
# only when an admin saves something. One SELECT per lookup is invisible on
# local SQLite and ruinous on a managed Postgres, where each one is a network
# round trip. So the table is held in memory and dropped whenever anything
# writes to it. The TTL is a backstop for the day this runs on more than one
# instance, where another process could be the writer.
_settings: dict[str, str] | None = None
_settings_at: float = 0.0
SETTINGS_TTL = 60.0


def invalidate_settings() -> None:
    """Force the next read to go back to the database."""
    global _settings
    _settings = None


async def _settings_map() -> dict[str, str]:
    global _settings, _settings_at
    import time
    if _settings is None or time.monotonic() - _settings_at > SETTINGS_TTL:
        rows = await q("SELECT key, value FROM settings")
        _settings = {r["key"]: r["value"] for r in rows}
        _settings_at = time.monotonic()
    return _settings


async def setting(key: str, default: str = "") -> str:
    return (await _settings_map()).get(key, default)


async def settings_prefix(prefix: str) -> dict[str, str]:
    """Every setting under one prefix, keys stripped of it. One read, not one
    per key — `flair:emoji:` alone has 116 of them."""
    n = len(prefix)
    return {k[n:]: v for k, v in (await _settings_map()).items()
            if k.startswith(prefix)}


async def set_setting(key: str, value: str) -> None:
    await ex(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    invalidate_settings()


# ----------------------------------------------------------------- stats
async def alert_counts() -> dict:
    """Things waiting on an admin right now."""
    r = await q1("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM withdrawals "
                 "WHERE status = 'pending'")
    v = await q1("SELECT COUNT(*) c FROM orders WHERE status = 'awaiting_review'")
    return {"withdrawals": r["c"], "withdraw_total": r["s"], "reviews": v["c"]}


async def stats() -> dict:
    def one(sql, args=()):
        return q1(sql, args)

    users = (await one("SELECT COUNT(*) c FROM users"))["c"]
    banned = (await one("SELECT COUNT(*) c FROM users WHERE is_banned = 1"))["c"]
    orders_total = (await one("SELECT COUNT(*) c FROM orders WHERE status = 'delivered'"))["c"]
    rev_all = (await one(
        "SELECT COALESCE(SUM(amount), 0) s FROM orders "
        "WHERE status = 'delivered' AND kind = 'purchase'"))["s"]
    rev_today = (await one(
        "SELECT COALESCE(SUM(amount), 0) s FROM orders WHERE status = 'delivered' "
        "AND kind = 'purchase' "
        "AND substr(paid_at, 1, 10) = substr(datetime('now'), 1, 10)"))["s"]
    pending = (await one("SELECT COUNT(*) c FROM orders WHERE status IN ('pending','awaiting_review')"))["c"]
    prods = (await one("SELECT COUNT(*) c FROM products WHERE is_active = 1"))["c"]
    in_stock = (await one("SELECT COUNT(*) c FROM stock WHERE is_sold = 0"))["c"]
    return dict(users=users, banned=banned, orders=orders_total, rev_all=rev_all,
                rev_today=rev_today, pending=pending, products=prods, in_stock=in_stock)


async def low_stock(threshold: int):
    return await q(
        "SELECT p.id, p.name, COUNT(s.id) c FROM products p "
        "LEFT JOIN stock s ON s.product_id = p.id AND s.is_sold = 0 "
        "WHERE p.is_active = 1 AND p.infinite = 0 "
        # repeat the aggregate rather than referencing the alias: SQLite
        # allows an alias in HAVING, Postgres doesn't
        "GROUP BY p.id, p.name HAVING COUNT(s.id) <= ? ORDER BY COUNT(s.id)",
        (threshold,),
    )


# ------------------------------------------------------ web panel queries
async def list_orders(status: str | None = None, limit: int = 50, offset: int = 0):
    sql = "SELECT * FROM orders"
    args: list[Any] = []
    if status and status != "all":
        if status == "open":
            sql += " WHERE status IN ('pending','awaiting_review')"
        else:
            sql += " WHERE status = ?"
            args.append(status)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    return await q(sql, (*args, limit, offset))


async def count_users() -> int:
    row = await q1("SELECT COUNT(*) c FROM users")
    return int(row["c"]) if row else 0


async def list_users(term: str = "", limit: int = 50, offset: int = 0):
    term = (term or "").strip().lstrip("@")
    if not term:
        return await q("SELECT * FROM users ORDER BY created_at DESC "
                       "LIMIT ? OFFSET ?", (limit, offset))
    if term.isdigit():
        return await q("SELECT * FROM users WHERE CAST(tg_id AS TEXT) LIKE ? LIMIT ?",
                       (f"%{term}%", limit))
    return await q("SELECT * FROM users WHERE LOWER(username) LIKE LOWER(?) "
                   "OR LOWER(first_name) LIKE LOWER(?) LIMIT ?",
                   (f"%{term}%", f"%{term}%", limit))


async def user_summary(tg_id: int) -> dict:
    r = await q1("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM orders "
                 "WHERE user_id = ? AND status = 'delivered'", (tg_id,))
    return {"orders": r["c"], "spent": r["s"]}


async def catalog() -> list[dict]:
    out = []
    for c in await categories():
        prods = []
        for p in await products(c["id"], only_active=False):
            prods.append({**dict(p), "stock": await stock_count(p["id"])})
        out.append({**dict(c), "products": prods})
    return out


async def revenue_series(days: int = 14) -> list[dict]:
    rows = await q(
        "SELECT substr(paid_at, 1, 10) d, COALESCE(SUM(amount),0) s, COUNT(*) n "
        "FROM orders WHERE status = 'delivered' AND kind = 'purchase' "
        "AND paid_at IS NOT NULL "
        "AND substr(paid_at, 1, 10) >= substr(datetime('now', ?), 1, 10) "
        "GROUP BY substr(paid_at, 1, 10) ORDER BY 1",
        (f"-{days} days",))
    return [dict(r) for r in rows]


async def all_settings() -> dict:
    return {r["key"]: r["value"] for r in await q("SELECT key, value FROM settings")}
