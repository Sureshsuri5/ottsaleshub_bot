# Digital Goods Telegram Shop

Catalogue → checkout → automatic payment verification → instant delivery, with a
full admin panel inside Telegram. aiogram 3 + SQLite, no external services
required beyond the payment rails you choose to switch on.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in BOT_TOKEN and ADMIN_IDS at minimum
python bot.py
```

Get `BOT_TOKEN` from [@BotFather](https://t.me/BotFather) and your numeric ID
from [@userinfobot](https://t.me/userinfobot). Then send `/admin` to the bot →
**Categories → Add category → Add product → Add stock**. That's the whole setup.

## How payment verification actually works

| Provider | Verification | Setup needed |
|---|---|---|
| `balance` | Instant — funds already held in the wallet | none |
| `stars` | **Native.** Telegram sends a signed `successful_payment` update; nothing to poll, nothing to trust from the buyer | none |
| `crypto` | **On-chain.** Each order gets a unique USDT amount; TronGrid is polled for inbound TRC-20 transfers and matched by amount + timestamp + unused txid | `TRON_ADDRESS`, optional `TRONGRID_API_KEY` |
| `upi` | Webhook mode: PSP posts an HMAC-signed callback → automatic. Manual mode: buyer submits a UTR → one-tap admin approval | `UPI_VPA`, or a PSP + `WEBHOOK_*` |

Trim `ENABLED_PROVIDERS` to whichever of these you want to offer.

**Why the unique-amount trick for crypto:** one receiving address is far simpler
to operate than HD-derived addresses per order. Each pending order is assigned a
random sub-cent offset (e.g. `5.6136 USDT`), and `db.amount_taken()` guarantees
no two open orders share one. An incoming transfer therefore maps to exactly one
order. Amounts are released when the order expires (`ORDER_TTL_MINUTES`).

**Anti-replay:** every txid, UTR and PSP payment id goes into `seen_tx`, which
has a primary-key constraint. A replayed callback or a resubmitted UTR is
rejected before it can touch an order.

## Delivery

`delivery.settle()` is the one funnel every payment path goes through, and it is
idempotent — a second call on a delivered order is a no-op, so a duplicate
webhook can never double-deliver.

Products come in two shapes:

- **Per-unit stock** — one line of `stock` per unit. Lines are allocated under an
  asyncio lock, marked sold, and bound to the order, so two simultaneous buyers
  can never receive the same key.
- **Unlimited** (`♾ Unlimited mode`) — every buyer gets the same payload. Use for
  ebooks, course links, invite links.

Orders over 20 lines are delivered as a `.txt` file instead of a message. If a
product sells out between payment and delivery, the buyer is auto-refunded to
their wallet balance and admins are alerted.

## Deploying somewhere always-on

Running locally means the bot dies when your PC sleeps. To keep it up without
your machine, push this folder to a GitHub repo and deploy the `Dockerfile`.

Set `BOT_MODE=webhook` and the bot stops long-polling: Telegram pushes updates
to the same port that serves the Mini Apps, which is what every PaaS expects.
`WEBAPP_URL` and the port are read from whatever the platform injects
(`RENDER_EXTERNAL_URL`, `KOYEB_PUBLIC_DOMAIN`, `RAILWAY_PUBLIC_DOMAIN`,
`FLY_APP_NAME`, `PORT`), so the Mini App buttons light up with no manual URL.

**Koyeb** — free instance doesn't sleep, usually no card. Step-by-step in
`koyeb.md`. Best fit for a bot you want responsive.

**Render** — `render.yaml` is a ready Blueprint. The free plan sleeps after
~15 minutes idle; the next update wakes it and Telegram retries, so you lose
nothing but see one slow response. Free plans have no disk, so delete the `disk:`
block and expect the database to reset on redeploy.

Whatever you pick: set `BOT_TOKEN` and `ADMIN_IDS` in the platform's environment
variables, never in git. `.dockerignore` already excludes `.env` and `*.db`.

Locally, `docker compose up --build` runs the same image with `./data` mounted,
so you can rehearse the deploy before pushing.

### The catch worth knowing

Free tiers give you ephemeral disk. SQLite lives on that disk, so **products,
stock, orders and balances reset whenever the container restarts** unless you
attach a volume. That's fine for clicking through the flows; it is not fine once
real money is involved. Before going live, attach a persistent volume or move
`db.py` to Postgres — every query is in that one file.

Free tiers also change often. Check the platform's current terms rather than
trusting this table.

## Mini Apps

Two Telegram Mini Apps are served by `webapp.py` on one port:

| Route | Who | What |
|---|---|---|
| `/` | buyers | catalogue, quantity picker, checkout, live payment screen, order history, wallet |
| `/admin` | admins | revenue sparkline, catalogue + stock editing, order review, users, broadcast, shop messages |

### Turning them on

Telegram only opens a Mini App over **public HTTPS** — not `http`, not an IP,
not `localhost`. For development, tunnel your local port:

```bash
cloudflared tunnel --url http://localhost:8080     # or: ngrok http 8080
```

Paste the resulting URL into `.env` as `WEBAPP_URL` and restart. The bot swaps
its menu buttons for Mini App launchers automatically; until then it logs a
warning and falls back to the inline panel, which keeps working.

Optionally set the same URL as the bot's menu button in @BotFather
(`/mybots` → your bot → Bot Settings → Menu Button) so the shop opens from the
chat's attachment bar.

To open the admin panel in a desktop browser instead, set `ADMIN_PANEL_TOKEN`
and visit `http://localhost:8080/admin?token=YOUR_TOKEN`. The token is stored in
`localStorage` after the first visit. Leave it blank in production.

### Why this is safe

Every API call carries Telegram's `initData`, and the server recomputes its
HMAC-SHA256 with the bot token before trusting anything. A tampered payload is
rejected with 401, so the user id it contains cannot be forged — that id is what
authorises checkout, order access and admin rights.

Nothing else from the client is trusted. Prices, stock levels and balances are
always read server-side, product updates run through a field allowlist, and one
buyer cannot read another's order. All of this is covered by the test in
"Testing" below.

## Public sales feed

Every delivery is announced to a group of your choosing. The post carries the
product, quantity, amount, method and a short hashed buyer tag — and nothing
else. The buyer's id, username, name and the delivered items never leave the bot.

The tag is an HMAC of the user id keyed with the bot token, truncated to four
characters. Repeat buyers therefore look consistent in the feed without being
identifiable, and the tag can't be reversed to an account by anyone who doesn't
already hold the token.

Add the bot to the group as an admin, then set the group id under
**More → Sales feed** in the admin panel (or `SALES_CHAT_ID` in `.env`). There's
a **Send test post** button, and an option to hide the amount if you'd rather
publish volume than revenue.

## Button colours, icons and stickers

Bot API 9.4 added `style` and `icon_custom_emoji_id` to keyboard buttons, so
colour and premium emoji now live on the buttons themselves.

Colours are mapped by meaning, not decoration, and work on every account:

| Style | Used for |
|---|---|
| `success` (green) | Buy now, Approve, Add stock, Add balance, I've paid |
| `danger` (red) | Delete, Reject, Cancel order, Ban |
| `primary` (blue) | Open shop, Admin panel, Top up |
| none | back and navigation buttons |

**Custom emoji icons** on buttons, and premium emoji in message text, need the
account that owns the bot to have an active Telegram Premium subscription (or the
bot to own a Fragment username). To set them up: send the bot `/ids`, then a
sticker or a premium emoji, and it replies with the id to paste into
**More → Stickers & premium emoji**. **Send me a style test** posts one message
with all three colours and your icons, and reports the exact Telegram error if
icons are refused rather than letting a live Buy button break.

If Telegram ever rejects a custom emoji, `flair.send()` drops to plain Unicode
permanently for that run and logs why — the shop keeps working either way.

Stickers are optional per slot; a configured sale sticker is posted to the feed
just before the sale message.

## Shop layout

Buyers see one flat list of every active product — categories are an admin-side
filing device and never appear in the shop. Each product button is colour-coded
by stock: **blue** when it can be bought, **red** when it's sold out. A
**🔄 Refresh** button sits under the list for restock checks; pressing it when
nothing has changed answers "Already up to date" rather than redrawing.

The Mini App storefront mirrors this — flat list, blue rail on buyable cards,
red rail on sold-out ones, refresh at the bottom.

Categories still exist in the admin panel because products have to be filed
somewhere; they just aren't a navigation step for buyers.

## Navigation

The bot is **entirely inline** — there is no reply keyboard. Every screen edits
the same message in place, so the chat stays at one card instead of growing a
scroll history. `/start` and `/menu` open the main menu; `/admin` opens the panel.

The only points that accept typed input are the ones that genuinely need free
text (custom top-up amount, UTR, product name/price, broadcast body, admin
search). Each of those shows a **✖ Cancel** button, and any stray text sent
outside a form just re-opens the menu.

`show()` / `_show()` handle the edit, falling back to a fresh message when the
current one is a QR photo or when Telegram rejects an identical edit.

## Admin panel

`/admin`, or the **🛠 Admin panel** button on the main menu (admins only).

- **📊 Stats** — users, revenue today/all-time, open orders, low-stock warnings
- Destructive actions (delete product / category) require a confirmation tap
- **🗂 Categories / 📦 Products** — full CRUD, activate/hide, edit name, description, price
- **Stock** — paste lines or upload a `.txt`, export unsold stock, purge sold rows
- **👤 Users** — look up by ID or @username, credit/debit balance, ban/unban, order history
- **🧾 Reviews** — approve or reject manually-submitted UPI payments in one tap
- **📣 Broadcast** — rate-limited send to all non-banned users
- **⚙️ Settings** — edit the welcome and support texts without redeploying

## Files

```
bot.py             entrypoint: routers, ban middleware, watcher, web server
webapp.py          Mini App server: initData auth + JSON API for shop and admin
static/shop.html   buyer Mini App
static/admin.html  admin Mini App
static/app.css     shared theme (surfaces follow the Telegram client)
static/tg.js       shared client: API calls, back-button stack, formatting
config.py          .env loader
db.py              schema + every query (SQLite/WAL)
payments.py        provider classes — add a new rail by adding one class here
delivery.py        settlement + delivery (idempotent)
watcher.py         background poller + order expiry
webhook.py         optional PSP callback routes, mounted on the same port
flair.py           sales feed, buyer hashing, premium emoji + sticker helpers
handlers_user.py   catalogue, checkout, orders, wallet
handlers_admin.py  admin panel
keyboards.py       all inline/reply keyboards
```

## Adding a payment provider

Write a class with `code`, `title`, `quote()`, `create()` and `poll()`, register
it in `payments.REGISTRY`, add its code to `ENABLED_PROVIDERS`. Nothing else in
the bot changes — the keyboards, order table and settlement funnel are all
provider-agnostic.

## Testing

`test_web.py` boots the server against a temporary database and a stub bot, then
asserts the things that would actually cost you money if they broke: unauthorised
and tampered `initData` are rejected, non-admins get 403 from admin routes, a
client-supplied price is ignored in favour of the database price, overselling
returns 409, and one user cannot read another's order.

```bash
python test_web.py
```

## Production notes

- Run under systemd or `docker run --restart=always`; SQLite in WAL mode handles
  this workload comfortably into the thousands of orders.
- Back up `shop.db` (plus `-wal`/`-shm`) on a schedule — it holds undelivered stock.
- Put the server behind HTTPS (Caddy/nginx or a Cloudflare tunnel) — required for
  Mini Apps anyway — and keep `WEBHOOK_SECRET` long.
- Clear `ADMIN_PANEL_TOKEN` once you're done developing; `initData` is the real
  auth path and the token is only a browser convenience.
- Never commit `.env`.
- To scale past one process, move FSM storage from `MemoryStorage` to Redis and
  migrate the DB to Postgres — the query layer is isolated in `db.py`.
- Selling through a bot puts you under Telegram's ToS and your payment
  processor's acceptable-use rules; both prohibit certain goods and both will
  freeze accounts over chargebacks, so check what you're listing against them
  before you go live.
