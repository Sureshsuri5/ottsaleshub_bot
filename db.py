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

CREATE TABLE IF NOT EXISTS admin_logins (
    email      TEXT PRIMARY KEY,
    pw_hash    TEXT NOT NULL,
    tg_id      INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_login TEXT
);

-- Outside suppliers who fulfil manual orders. Deliberately not rows in
-- admin_logins with a flag: a maker is a different kind of account, and a
-- shared table is one stray query away from a supplier reading the shop.
CREATE TABLE IF NOT EXISTS makers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT    NOT NULL UNIQUE,
    pw_hash    TEXT    NOT NULL,
    name       TEXT    NOT NULL DEFAULT '',
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS bank_sms (
    utr        TEXT PRIMARY KEY,
    amount     REAL NOT NULL,
    raw        TEXT NOT NULL DEFAULT '',
    order_id   INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
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

-- Orders for products that a human has to activate: the buyer supplies a
-- number, the operator triggers the service, the buyer relays the OTP back.
-- One row per order, holding only the current position in that conversation.
CREATE TABLE IF NOT EXISTS fulfilment (
    order_id    BIGINT  PRIMARY KEY REFERENCES orders(id) ON DELETE CASCADE,
    -- BIGINT written out rather than INTEGER: the SQLite->PG rewrite matches
    -- the users(tg_id) foreign key by exact whitespace, and a width this
    -- column got wrong silently becomes int4 on Postgres. Telegram ids above
    -- 2^31 then fail to insert, which kills the order mid-fulfilment.
    user_id     BIGINT  NOT NULL REFERENCES users(tg_id),
    stage       TEXT    NOT NULL DEFAULT 'awaiting_number',
        -- awaiting_number | awaiting_otp | working | done | cancelled
    number      TEXT    NOT NULL DEFAULT '',
    note        TEXT    NOT NULL DEFAULT '',
    nudged      INTEGER NOT NULL DEFAULT 0,   -- reminders already sent
    unread      INTEGER NOT NULL DEFAULT 0,   -- buyer messages the panel hasn't shown
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fulfil_stage ON fulfilment(stage, updated_at);

-- The transcript. `sender` is 'user', 'admin' or 'system'; system lines are
-- the state changes, so the thread reads as one story rather than needing a
-- separate audit log beside it.
CREATE TABLE IF NOT EXISTS fulfil_msgs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   BIGINT  NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    sender     TEXT    NOT NULL,
    body       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fulfil_msgs ON fulfil_msgs(order_id, id);
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


_RLS_LOCKDOWN = """
do $$
declare t record;
begin
  for t in select tablename from pg_tables where schemaname = 'public'
  loop
    execute format('alter table public.%I enable row level security', t.tablename);
  end loop;
end $$;
revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
alter default privileges in schema public revoke all on tables from anon, authenticated;
"""


async def _pg_lock_down() -> None:
    """Enable RLS on every public table and strip the PostgREST roles' grants.

    Supabase exposes the whole `public` schema over PostgREST using the anon
    key, which ships in client code and is therefore public. Without RLS, that
    key reads `stock` — the undelivered goods — straight out of the database.
    We connect as `postgres`, which bypasses RLS, so this costs us nothing.

    Idempotent, so it runs on every boot and covers tables added by future
    migrations. Never fatal: a managed instance may not grant us the rights to
    revoke, and losing the bot over a hardening step we cannot apply is a worse
    outcome than logging it and carrying on.
    """
    try:
        await _pg_run(lambda con: con.execute(_RLS_LOCKDOWN))
        log.info("RLS enabled on all public tables")
    except Exception as e:                              # pragma: no cover
        log.warning("could not apply RLS lockdown: %s", e)


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
        await _pg_lock_down()
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
        # conditions shown under the description — warranty, region limits,
        # anything a buyer should read before paying
        "ALTER TABLE products ADD COLUMN note TEXT NOT NULL DEFAULT ''",
        # what a unit costs you — used for profit reporting
        "ALTER TABLE products ADD COLUMN cost REAL NOT NULL DEFAULT 0",
        # the cost at the moment of sale. Snapshotted so that changing a
        # product's cost price later doesn't silently rewrite past profit.
        "ALTER TABLE orders ADD COLUMN unit_cost REAL NOT NULL DEFAULT 0",
        # a reseller's own reference for the request, used to make repeated
        # purchase calls safe to retry
        "ALTER TABLE orders ADD COLUMN client_ref TEXT NOT NULL DEFAULT ''",
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
        # manual=1 -> no stock line exists; an operator activates it by hand
        "ALTER TABLE products ADD COLUMN manual INTEGER NOT NULL DEFAULT 0",
        # which supplier fulfils this product, and which one owns a given order
        "ALTER TABLE products ADD COLUMN maker_id INTEGER",
        # snapshotted onto the order: reassigning a product later must not move
        # orders a maker is already part-way through, nor hide finished ones
        "ALTER TABLE fulfilment ADD COLUMN maker_id INTEGER",
        # Repair for databases created before the BIGINT fix above. On Postgres
        # these columns were made int4 because the type-rewrite rule missed
        # them, so any Telegram id past 2^31 failed to insert and the order
        # died silently after payment. No-ops on SQLite, where the statement
        # simply raises and is skipped like any already-applied migration.
        "ALTER TABLE fulfilment ALTER COLUMN user_id TYPE BIGINT",
        "ALTER TABLE fulfilment ALTER COLUMN order_id TYPE BIGINT",
        "ALTER TABLE fulfil_msgs ALTER COLUMN order_id TYPE BIGINT",
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


async def broadcast_targets() -> list[dict]:
    """Everyone a broadcast goes to: not banned, with the names it fills in."""
    rows = await q("SELECT tg_id, first_name, username FROM users "
                   "WHERE is_banned = 0")
    return [dict(r) for r in rows]


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
    """Units purchasable right now. Infinite products report a large number.

    Manual products report the same: they hold no stock rows because a person
    activates them, so counting the table would say 0 and every gate above this
    would refuse the sale. Reporting them as available is what makes them
    buyable — the callers that mean "how many are on the shelf" rather than
    "can this be bought" have to exclude them explicitly, and the two that do
    are the group restock announcer and the restock watchlist.
    """
    p = await product(pid)
    if not p:
        return 0
    if "manual" in p.keys() and p["manual"]:
        return 10**6
    return 10**6 if p["infinite"] else await stock_count(pid)


async def add_stock(pid: int, lines: Iterable[str]) -> tuple[int, int]:
    """Add stock, skipping anything this product already has.

    Returns (added, skipped). Duplicates are checked against sold rows too: a
    key that has already been delivered must never be handed to a second buyer,
    and pasting the same file twice is the ordinary way that happens.

    Within the batch as well as against the database — a list can repeat itself.
    """
    seen: set[str] = set()
    wanted: list[str] = []
    skipped = 0
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if ln in seen:
            skipped += 1
            continue
        seen.add(ln)
        wanted.append(ln)
    if not wanted:
        return 0, skipped

    existing = {r["payload"] for r in await q(
        "SELECT payload FROM stock WHERE product_id = ?", (pid,))}
    rows = [(pid, ln) for ln in wanted if ln not in existing]
    skipped += len(wanted) - len(rows)
    if not rows:
        return 0, skipped
    added = await ex_many(
        "INSERT INTO stock (product_id, payload) VALUES (?, ?)", rows)
    return added, skipped


async def stock_rows(pid: int, limit: int = 200) -> list:
    """Unsold stock with ids, so a single item can be removed."""
    return await q("SELECT id, payload FROM stock "
                   "WHERE product_id = ? AND is_sold = 0 ORDER BY id LIMIT ?",
                   (pid, limit))


async def delete_stock(pid: int, sid: int) -> bool:
    """Remove one unsold item.

    Sold rows are never deletable here: they are the record of what a buyer
    was given, and My Orders reads from them. The product_id is part of the
    condition so a stray id can't delete another product's stock.
    """
    n = await ex_count("DELETE FROM stock WHERE id = ? AND product_id = ? "
                       "AND is_sold = 0", (sid, pid))
    return n > 0


async def clear_unsold(pid: int) -> int:
    """Remove every unsold item for a product.

    Sold rows are untouched: they are what buyers were given, and My Orders
    reads from them. This empties the shelf, it doesn't erase the sales.
    """
    return await ex_count("DELETE FROM stock WHERE product_id = ? AND is_sold = 0",
                          (pid,))


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


async def has_fresh_order(minutes: int) -> bool:
    """Is anyone sitting at a payment screen right now?

    Used to decide how hard to poll. An order minutes old is one a buyer is
    probably paying this instant; one from half an hour ago is probably
    abandoned, and polling for it at the same rate spends RPC calls on nothing.
    """
    row = await q1(
        "SELECT 1 FROM orders WHERE status = 'pending' "
        "AND pay_address IS NOT NULL AND pay_address != '' "
        "AND created_at > datetime('now', ?) LIMIT 1", (f"-{max(1, minutes)} minutes",))
    return bool(row)


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
async def profit(days_list=(1, 7, 30), top: int = 10) -> dict:
    """Revenue, cost and profit over several windows, and per product.

    Cost comes from the snapshot taken when the order was delivered, falling
    back to the product's current cost for orders placed before cost tracking
    existed. Without the snapshot, editing a cost price would rewrite history.

    Revenue is amount + balance_used, as everywhere else: the wallet share of
    a part-paid order is still money the shop took.
    """
    rev = "COALESCE(o.amount, 0) + COALESCE(o.balance_used, 0)"
    cost = ("o.qty * CASE WHEN COALESCE(o.unit_cost, 0) > 0 "
            "THEN o.unit_cost ELSE COALESCE(p.cost, 0) END")
    base = ("FROM orders o LEFT JOIN products p ON p.id = o.product_id "
            "WHERE o.status = 'delivered' AND o.kind = 'purchase'")

    parts, args = [], []
    for d in days_list:
        parts += [f"(SELECT COALESCE(SUM({rev}),0) {base} "
                  f"AND COALESCE(o.paid_at, o.created_at) > datetime('now', ?)) r{d}",
                  f"(SELECT COALESCE(SUM({cost}),0) {base} "
                  f"AND COALESCE(o.paid_at, o.created_at) > datetime('now', ?)) c{d}"]
        args += [f"-{d} days", f"-{d} days"]
    parts += [f"(SELECT COALESCE(SUM({rev}),0) {base}) rall",
              f"(SELECT COALESCE(SUM({cost}),0) {base}) call"]
    row = await q1("SELECT " + ", ".join(parts), args)

    def win(rk, ck):
        r, c = round(float(row[rk] or 0), 2), round(float(row[ck] or 0), 2)
        return {"revenue": r, "cost": c, "profit": round(r - c, 2),
                "margin": round((r - c) / r * 100) if r else 0}

    windows = {str(d): win(f"r{d}", f"c{d}") for d in days_list}
    windows["all"] = win("rall", "call")

    rows = await q(
        f"SELECT o.product_name name, SUM(o.qty) units, "
        f"COALESCE(SUM({rev}),0) revenue, COALESCE(SUM({cost}),0) cost "
        f"{base} GROUP BY o.product_name "
        f"ORDER BY (COALESCE(SUM({rev}),0) - COALESCE(SUM({cost}),0)) DESC LIMIT ?",
        (top,))
    products = []
    for r in rows:
        rv, cs = round(float(r["revenue"] or 0), 2), round(float(r["cost"] or 0), 2)
        products.append({"name": r["name"], "units": r["units"],
                         "revenue": rv, "cost": cs,
                         "profit": round(rv - cs, 2),
                         "margin": round((rv - cs) / rv * 100) if rv else 0})
    missing = await q1(
        "SELECT COUNT(*) c FROM products WHERE is_active = 1 AND COALESCE(cost,0) = 0")
    return {"windows": windows, "products": products,
            "no_cost": int(missing["c"]) if missing else 0}


async def order_counts() -> dict:
    """How many orders sit under each Orders filter, in one query."""
    r = await q1(
        "SELECT "
        "(SELECT COUNT(*) FROM orders "
        "  WHERE status IN ('pending','awaiting_review')) open, "
        "(SELECT COUNT(*) FROM orders WHERE status = 'awaiting_review') review, "
        "(SELECT COUNT(*) FROM orders WHERE status = 'delivered') delivered, "
        "(SELECT COUNT(*) FROM orders WHERE status = 'fulfilling') fulfilling, "
        "(SELECT COALESCE(SUM(unread), 0) FROM fulfilment "
        "  WHERE stage IN ('awaiting_number','awaiting_otp','working')) fulfil_unread, "
        "(SELECT COUNT(*) FROM orders) all_orders")
    return {"open": r["open"], "review": r["review"],
            "delivered": r["delivered"], "fulfilling": r["fulfilling"],
            "fulfil_unread": r["fulfil_unread"],
            "all": r["all_orders"]}


async def alert_counts() -> dict:
    """Things waiting on an admin right now."""
    r = await q1(
        "SELECT "
        "(SELECT COUNT(*) FROM withdrawals WHERE status = 'pending') c, "
        "(SELECT COALESCE(SUM(amount),0) FROM withdrawals "
        "  WHERE status = 'pending') s, "
        "(SELECT COUNT(*) FROM orders WHERE status = 'awaiting_review') v")
    return {"withdrawals": r["c"], "withdraw_total": r["s"], "reviews": r["v"]}


async def set_admin_login(email: str, pw_hash: str, tg_id: int) -> None:
    """Create or replace the password for an email."""
    email = email.strip().lower()
    await ex("DELETE FROM admin_logins WHERE LOWER(email) = LOWER(?)", (email,))
    await ex("INSERT INTO admin_logins (email, pw_hash, tg_id) VALUES (?, ?, ?)",
             (email, pw_hash, tg_id))


async def admin_login(email: str):
    return await q1("SELECT * FROM admin_logins WHERE LOWER(email) = LOWER(?)",
                    (email.strip().lower(),))


async def touch_admin_login(email: str) -> None:
    await ex("UPDATE admin_logins SET last_login = datetime('now') "
             "WHERE LOWER(email) = LOWER(?)", (email.strip().lower(),))


async def admin_logins() -> list:
    return await q("SELECT email, tg_id, created_at, last_login "
                   "FROM admin_logins ORDER BY email")


async def drop_admin_login(email: str) -> int:
    return await ex_count("DELETE FROM admin_logins WHERE LOWER(email) = LOWER(?)",
                          (email.strip().lower(),))


async def record_sms(utr: str, amount: float, raw: str) -> bool:
    """Store one bank credit. False if this UTR was already recorded.

    The UTR is the primary key, so a forwarder that resends the same message —
    which they do, on retry or after a restart — cannot credit an order twice.
    """
    try:
        await ex("INSERT INTO bank_sms (utr, amount, raw) VALUES (?, ?, ?)",
                 (utr, round(float(amount), 2), raw[:500]))
        return True
    except Exception:
        return False


async def sms_for(utr: str):
    return await q1("SELECT * FROM bank_sms WHERE utr = ?", (utr,))


async def claim_sms(utr: str, oid: int) -> None:
    await ex("UPDATE bank_sms SET order_id = ? WHERE utr = ? AND order_id IS NULL",
             (oid, utr))


async def unclaimed_sms(amount: float, tol: float = 0.01):
    """Bank credits matching this amount that no order has taken yet."""
    return await q("SELECT * FROM bank_sms WHERE order_id IS NULL "
                   "AND ABS(amount - ?) <= ? ORDER BY created_at", (round(amount, 2), tol))


async def api_usage(tg_id: int) -> dict:
    """What this key has done — orders placed and spend, for the API screen."""
    r = await q1(
        "SELECT COUNT(*) c, COALESCE(SUM(COALESCE(amount, 0)), 0) s "
        "FROM orders WHERE user_id = ? AND kind = 'purchase' "
        "AND provider = 'balance' AND status = 'delivered'", (tg_id,))
    recent = await q1(
        "SELECT COALESCE(SUM(COALESCE(amount, 0)), 0) s FROM orders "
        "WHERE user_id = ? AND kind = 'purchase' AND provider = 'balance' "
        "AND status = 'delivered' AND created_at > datetime('now', '-30 days')",
        (tg_id,))
    return {"orders": int(r["c"] or 0), "spent": float(r["s"] or 0),
            "recent": float(recent["s"] or 0) if recent else 0.0}


async def order_by_client_ref(user_id: int, ref: str):
    """An order this buyer already created under the same reference."""
    return await q1("SELECT * FROM orders WHERE user_id = ? AND client_ref = ? "
                    "ORDER BY id DESC LIMIT 1", (user_id, ref))


async def order_by_code(user_id: int, code: str):
    return await q1("SELECT * FROM orders WHERE user_id = ? "
                    "AND LOWER(code) = LOWER(?) LIMIT 1", (user_id, code))


async def reset_sales(clear_balances: bool = False) -> dict:
    """Wipe trading history and start order numbering again from #1.

    Deliberately narrow. Products, categories, stock, prices, settings, texts,
    flair and users all survive — this is for clearing test trades, not for
    emptying the shop.

    Sold stock stays sold: those items were handed to somebody, and making them
    sellable again would send the same key to a second buyer. Use "Clear sold
    rows" on a product if you want them gone too.
    """
    counts = {
        "orders": (await q1("SELECT COUNT(*) c FROM orders"))["c"],
        "withdrawals": (await q1("SELECT COUNT(*) c FROM withdrawals"))["c"],
    }
    await ex("DELETE FROM orders")
    await ex("DELETE FROM withdrawals")
    await ex("DELETE FROM seen_tx")
    await ex("DELETE FROM waitlist")

    if clear_balances:
        counts["balances"] = (await q1(
            "SELECT COALESCE(SUM(balance), 0) c FROM users"))["c"]
        await ex("UPDATE users SET balance = 0, ref_earned = 0, "
                 "ref_available = 0, ref_transferred = 0")

    # start numbering from 1 again — the sequence is engine-specific
    for table in ("orders", "withdrawals"):
        try:
            if _PG:
                await ex(f"ALTER SEQUENCE {table}_id_seq RESTART WITH 1")
            else:
                await ex("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
        except Exception as e:
            log.warning("could not reset the %s id sequence: %s", table, e)

    # announcement bookkeeping refers to orders that no longer exist
    for key in ("restock:pinned:newproduct_group", "restock:pinned:restock_group",
                "restock:pinned:pricedrop_group"):
        await ex("DELETE FROM settings WHERE key = ?", (key,))
    return counts


async def dashboard(days_list=(1, 7, 30), top: int = 5) -> dict:
    """Revenue over several windows, best sellers and best buyers.

    Only delivered purchases count. Deposits are excluded: money moved into a
    wallet isn't revenue until it buys something, and counting both would
    report the same payment twice.

    `balance_used` is added back in — an order part-paid from wallet is still
    a sale at its full value, and using `amount` alone would quietly
    under-report every discounted or part-paid order.
    """
    money = ("COALESCE(SUM(COALESCE(amount, 0) + COALESCE(balance_used, 0)), 0)")
    where = "status = 'delivered' AND kind = 'purchase'"

    # every window in one round trip rather than one each
    parts, args = [], []
    for d in days_list:
        parts.append(f"(SELECT {money} FROM orders WHERE {where} "
                     f"AND COALESCE(paid_at, created_at) > datetime('now', ?)) s{d}")
        parts.append(f"(SELECT COUNT(*) FROM orders WHERE {where} "
                     f"AND COALESCE(paid_at, created_at) > datetime('now', ?)) c{d}")
        args += [f"-{d} days", f"-{d} days"]
    parts.append(f"(SELECT {money} FROM orders WHERE {where}) sall")
    parts.append(f"(SELECT COUNT(*) FROM orders WHERE {where}) call")
    row = await q1("SELECT " + ", ".join(parts), args)
    revenue = {str(d): {"revenue": round(float(row[f"s{d}"] or 0), 2),
                        "orders": int(row[f"c{d}"] or 0)} for d in days_list}
    revenue["all"] = {"revenue": round(float(row["sall"] or 0), 2),
                      "orders": int(row["call"] or 0)}

    products = await q(
        f"SELECT product_name AS name, SUM(qty) units, {money} revenue, "
        f"COUNT(*) orders FROM orders WHERE {where} "
        f"GROUP BY product_name ORDER BY units DESC LIMIT ?", (top,))
    buyers = await q(
        f"SELECT o.user_id, u.username, u.first_name, COUNT(*) orders, "
        f"SUM(o.qty) units, {money.replace('amount', 'o.amount').replace('balance_used', 'o.balance_used')} spent "
        f"FROM orders o LEFT JOIN users u ON u.tg_id = o.user_id "
        f"WHERE o.status = 'delivered' AND o.kind = 'purchase' "
        # every selected column has to be grouped or aggregated: SQLite is
        # relaxed about this, Postgres raises GroupingError
        f"GROUP BY o.user_id, u.username, u.first_name "
        f"ORDER BY spent DESC LIMIT ?", (top,))
    return {
        "revenue": revenue,
        "products": [dict(r) for r in products],
        "buyers": [dict(r) for r in buyers],
    }


async def stats() -> dict:
    """The Today figures, in one round trip.

    Eight scalar counts across five tables. Asked separately they were eight
    sequential round trips — nothing on local SQLite, most of a second on a
    hosted database in another region. Scalar subqueries let the engine answer
    all of them at once, which both SQLite and Postgres support.

    amount + balance_used, matching dashboard(): summing amount alone counts
    only the part paid on the rail, so an order part-paid from wallet would be
    under-reported by exactly the wallet share.
    """
    money = "COALESCE(SUM(COALESCE(amount, 0) + COALESCE(balance_used, 0)), 0)"
    r = await q1(
        "SELECT "
        "(SELECT COUNT(*) FROM users) users, "
        "(SELECT COUNT(*) FROM users WHERE is_banned = 1) banned, "
        "(SELECT COUNT(*) FROM orders WHERE status = 'delivered') orders, "
        f"(SELECT {money} FROM orders WHERE status = 'delivered' "
        "  AND kind = 'purchase') rev_all, "
        f"(SELECT {money} FROM orders WHERE status = 'delivered' "
        "  AND kind = 'purchase' "
        "  AND substr(paid_at, 1, 10) = substr(datetime('now'), 1, 10)) rev_today, "
        "(SELECT COUNT(*) FROM orders "
        "  WHERE status IN ('pending','awaiting_review')) pending, "
        "(SELECT COUNT(*) FROM products WHERE is_active = 1) products, "
        "(SELECT COUNT(*) FROM stock WHERE is_sold = 0) in_stock")
    return dict(r)


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
async def list_orders(status: str | None = None, limit: int = 50, offset: int = 0,
                      term: str = ""):
    """Orders, newest first, optionally filtered by status and search term.

    A search ignores the status filter: someone hunting for one order knows
    what they're looking for, and making them guess which tab it's under is a
    worse experience than showing it wherever it is.

    The term matches the order code, the row id, the buyer's id, the product
    name and the payment reference — whichever of those the person happens to
    have to hand.
    """
    term = (term or "").strip().lstrip("#")
    args: list[Any] = []
    if term:
        like = f"%{term.lower()}%"
        sql = ("SELECT * FROM orders WHERE LOWER(COALESCE(code, '')) LIKE ? "
               "OR CAST(id AS TEXT) = ? OR CAST(user_id AS TEXT) LIKE ? "
               "OR LOWER(COALESCE(product_name, '')) LIKE ? "
               "OR LOWER(COALESCE(external_ref, '')) LIKE ?")
        args += [like, term, like, like, like]
    else:
        sql = "SELECT * FROM orders"
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


# --------------------------------------------------------------- fulfilment
# Orders a human has to work by hand. Everything here is keyed on order_id:
# an order has at most one fulfilment, and the transcript hangs off the same id.

OPEN_STAGES = ("awaiting_number", "awaiting_otp", "working")


async def open_fulfilment(oid: int, uid: int) -> None:
    """Start the conversation. Idempotent, because settle() can be re-entered
    by a duplicate webhook and a second row would orphan the first thread."""
    # ON CONFLICT rather than INSERT OR IGNORE: the SQLite->PG translation
    # rewrites the latter to a bare INSERT, which raises on a duplicate key
    # instead of ignoring it. This form means the same thing on both engines.
    await ex("INSERT INTO fulfilment (order_id, user_id) VALUES (?, ?) "
             "ON CONFLICT DO NOTHING", (oid, uid))


async def fulfilment(oid: int):
    return await q1("SELECT * FROM fulfilment WHERE order_id = ?", (oid,))


async def set_fulfil(oid: int, **fields) -> None:
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    await ex(f"UPDATE fulfilment SET {cols} WHERE order_id = ?",
             (*fields.values(), oid))


async def active_fulfilment(uid: int):
    """The one open order this buyer's messages belong to.

    Newest first: if somebody has two manual orders running, their next message
    is far more likely to be about the one just started than the older one.
    """
    marks = ",".join("?" * len(OPEN_STAGES))
    return await q1(f"SELECT * FROM fulfilment WHERE user_id = ? "
                    f"AND stage IN ({marks}) ORDER BY order_id DESC LIMIT 1",
                    (uid, *OPEN_STAGES))


async def fulfil_say(oid: int, sender: str, body: str) -> None:
    """Append to the transcript. A buyer line also raises the unread flag, which
    is what puts a badge on the queue so a reply isn't left sitting for hours."""
    await ex("INSERT INTO fulfil_msgs (order_id, sender, body) VALUES (?, ?, ?)",
             (oid, sender, body))
    if sender == "user":
        await ex("UPDATE fulfilment SET unread = unread + 1, updated_at = ? "
                 "WHERE order_id = ?", (now(), oid))


async def fulfil_thread(oid: int, limit: int = 200):
    return await q("SELECT * FROM fulfil_msgs WHERE order_id = ? "
                   "ORDER BY id LIMIT ?", (oid, limit))


async def fulfil_seen(oid: int) -> None:
    await ex("UPDATE fulfilment SET unread = 0 WHERE order_id = ?", (oid,))


async def fulfil_queue(closed: bool = False, limit: int = 100):
    """The work list, oldest-touched first — the queue is a queue, so the order
    waiting longest for a reply sits at the top rather than the newest one."""
    marks = ",".join("?" * len(OPEN_STAGES))
    where = (f"f.stage NOT IN ({marks})" if closed else f"f.stage IN ({marks})")
    return await q(
        "SELECT f.*, o.code, o.product_name, o.qty, o.amount, o.status, "
        "       u.username, u.first_name "
        "FROM fulfilment f "
        "JOIN orders o ON o.id = f.order_id "
        "LEFT JOIN users u ON u.tg_id = f.user_id "
        f"WHERE {where} "
        "ORDER BY f.unread DESC, f.updated_at ASC LIMIT ?",
        (*OPEN_STAGES, limit))


async def fulfil_counts() -> dict:
    marks = ",".join("?" * len(OPEN_STAGES))
    r = await q1(f"SELECT COUNT(*) open, COALESCE(SUM(unread), 0) unread "
                 f"FROM fulfilment WHERE stage IN ({marks})", OPEN_STAGES)
    return {"open": r["open"], "unread": r["unread"]}


async def fulfil_stale(minutes: int, max_nudges: int = 1):
    """Waiting on the buyer, untouched for `minutes`, not yet chased.

    Only the two stages where the ball is in the buyer's court. An order in
    `working` is waiting on the operator, and reminding a customer about that
    would be blaming them for a delay that is ours.
    """
    return await q(
        "SELECT * FROM fulfilment WHERE stage IN ('awaiting_number', 'awaiting_otp') "
        "AND nudged < ? AND updated_at <= datetime('now', ?)",
        (max_nudges, f"-{int(minutes)} minutes"))


async def fulfil_scrub(oid: int) -> int:
    """Redact one-time codes once the order is done.

    An OTP is worthless after use but toxic in storage, and a support
    transcript is exactly the sort of table that gets exported and emailed
    around. The activation number and the wording of the conversation stay —
    only bare 4-to-8 digit runs in buyer messages go, which is what an OTP
    looks like and what a phone number, price or order code does not.
    """
    rows = await q("SELECT id, body FROM fulfil_msgs WHERE order_id = ? "
                   "AND sender = 'user'", (oid,))
    import re as _re
    n = 0
    for r in rows:
        new = _re.sub(r"(?<!\d)\d{4,8}(?!\d)", "[code redacted]", r["body"])
        if new != r["body"]:
            await ex("UPDATE fulfil_msgs SET body = ? WHERE id = ?", (new, r["id"]))
            n += 1
    return n


# ------------------------------------------------------------------- makers

async def maker_by_email(email: str):
    return await q1("SELECT * FROM makers WHERE LOWER(email) = LOWER(?)",
                    (email.strip().lower(),))


async def maker(mid: int):
    return await q1("SELECT * FROM makers WHERE id = ?", (mid,))


async def makers_list():
    return await q("SELECT id, email, name, is_active, created_at, last_login "
                   "FROM makers ORDER BY name, email")


async def add_maker(email: str, pw_hash: str, name: str) -> int:
    await ex("INSERT INTO makers (email, pw_hash, name) VALUES (?, ?, ?)",
             (email.strip().lower(), pw_hash, name.strip()[:80]))
    row = await maker_by_email(email)
    return int(row["id"]) if row else 0


async def set_maker_active(mid: int, active: bool) -> None:
    await ex("UPDATE makers SET is_active = ? WHERE id = ?",
             (1 if active else 0, mid))


async def set_maker_password(mid: int, pw_hash: str) -> None:
    await ex("UPDATE makers SET pw_hash = ? WHERE id = ?", (pw_hash, mid))


async def drop_maker(mid: int) -> int:
    """Remove the account. Products pointing at it fall back to unassigned,
    and orders already in flight keep their snapshot so nothing is orphaned
    mid-conversation — the admin still sees them in the main queue."""
    await ex("UPDATE products SET maker_id = NULL WHERE maker_id = ?", (mid,))
    return await ex_count("DELETE FROM makers WHERE id = ?", (mid,))


async def touch_maker_login(mid: int) -> None:
    await ex("UPDATE makers SET last_login = datetime('now') WHERE id = ?", (mid,))


async def maker_queue(mid: int, closed: bool = False, limit: int = 100):
    """One maker's work list. Scoped by maker_id in the query itself, not
    filtered after the fact, so there is no version of this that accidentally
    returns another supplier's orders."""
    marks = ",".join("?" * len(OPEN_STAGES))
    where = (f"f.stage NOT IN ({marks})" if closed else f"f.stage IN ({marks})")
    return await q(
        "SELECT f.order_id, f.stage, f.number, f.note, f.unread, f.updated_at, "
        "       o.code, o.product_name, o.qty "
        "FROM fulfilment f JOIN orders o ON o.id = f.order_id "
        f"WHERE f.maker_id = ? AND {where} "
        "ORDER BY f.unread DESC, f.updated_at ASC LIMIT ?",
        (mid, *OPEN_STAGES, limit))


async def maker_owns(mid: int, oid: int) -> bool:
    r = await q1("SELECT 1 AS ok FROM fulfilment WHERE order_id = ? AND maker_id = ?",
                 (oid, mid))
    return bool(r)


async def top_referrers(limit: int = 100, offset: int = 0):
    """Everyone who has invited at least one person, busiest first.

    A LEFT JOIN onto referrals-who-bought rather than a correlated subquery per
    row: the buyer count is the interesting column here — an inviter with forty
    signups and no purchases is farming the programme, and that only shows up
    when both numbers sit side by side.

    HAVING on the count is what enforces "at least one": users with no invites
    are the overwhelming majority and there is no reason to page through them.
    """
    return await q(
        "SELECT u.tg_id, u.username, u.first_name, u.balance, u.is_banned, "
        "       u.ref_earned, u.ref_available, u.ref_transferred, "
        "       COUNT(r.tg_id) AS invited, "
        "       COALESCE(SUM(CASE WHEN b.uid IS NOT NULL THEN 1 ELSE 0 END), 0) AS buyers "
        "FROM users u "
        "JOIN users r ON r.referred_by = u.tg_id "
        "LEFT JOIN (SELECT DISTINCT user_id AS uid FROM orders "
        "           WHERE status = 'delivered' AND kind = 'purchase') b "
        "       ON b.uid = r.tg_id "
        "GROUP BY u.tg_id, u.username, u.first_name, u.balance, u.is_banned, "
        "         u.ref_earned, u.ref_available, u.ref_transferred "
        "HAVING COUNT(r.tg_id) >= 1 "
        "ORDER BY invited DESC, u.ref_earned DESC "
        "LIMIT ? OFFSET ?", (limit, offset))


async def user_summary(tg_id: int) -> dict:
    r = await q1("SELECT COUNT(*) c, "
                 "COALESCE(SUM(COALESCE(amount,0) + COALESCE(balance_used,0)),0) s "
                 "FROM orders WHERE user_id = ? AND status = 'delivered'", (tg_id,))
    return {"orders": r["c"], "spent": r["s"]}


async def catalog() -> list[dict]:
    """Every category with its products and unsold stock counts.

    Three queries regardless of catalogue size. This used to ask for the stock
    count once per product — invisible on local SQLite, and a round trip each
    on a hosted database, which is what made the Catalog tab slow.
    """
    counts = {r["product_id"]: r["n"] for r in await q(
        "SELECT product_id, COUNT(*) n FROM stock WHERE is_sold = 0 "
        "GROUP BY product_id")}
    rows = await q("SELECT * FROM products ORDER BY id")
    by_cat: dict[int, list] = {}
    for p in rows:
        by_cat.setdefault(p["category_id"], []).append(
            {**dict(p), "stock": counts.get(p["id"], 0)})
    return [{**dict(c), "products": by_cat.get(c["id"], [])}
            for c in await categories()]


async def user_summaries(ids: list[int]) -> dict[int, dict]:
    """Order count and spend for many users at once.

    One query rather than one per user — the Users screen lists 60 at a time,
    and asking separately for each was 120 round trips.
    """
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = await q(
        f"SELECT user_id, COUNT(*) c, "
        f"COALESCE(SUM(COALESCE(amount,0) + COALESCE(balance_used,0)),0) s "
        f"FROM orders WHERE status = 'delivered' AND user_id IN ({marks}) "
        f"GROUP BY user_id", ids)
    return {r["user_id"]: {"orders": r["c"], "spent": r["s"]} for r in rows}


async def revenue_series(days: int = 14) -> list[dict]:
    rows = await q(
        "SELECT substr(paid_at, 1, 10) d, "
        "COALESCE(SUM(COALESCE(amount,0) + COALESCE(balance_used,0)),0) s, COUNT(*) n "
        "FROM orders WHERE status = 'delivered' AND kind = 'purchase' "
        "AND paid_at IS NOT NULL "
        "AND substr(paid_at, 1, 10) >= substr(datetime('now', ?), 1, 10) "
        "GROUP BY substr(paid_at, 1, 10) ORDER BY 1",
        (f"-{days} days",))
    return [dict(r) for r in rows]


async def all_settings() -> dict:
    return {r["key"]: r["value"] for r in await q("SELECT key, value FROM settings")}
