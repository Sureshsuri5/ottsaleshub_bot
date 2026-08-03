# Deploying to Koyeb (no sleep, usually no card)

1. Push this folder to a GitHub repo.
2. koyeb.com → Create Service → GitHub → pick the repo.
3. Builder: **Dockerfile**. Instance: **Free**. Port: **8080**.
4. Environment variables (Koyeb calls these *Secrets* for sensitive ones):

   | Key | Value |
   |---|---|
   | `BOT_TOKEN` | from @BotFather |
   | `ADMIN_IDS` | your numeric id |
   | `BOT_MODE` | `webhook` |
   | `ENABLED_PROVIDERS` | `balance,stars` |
   | `ADMIN_PANEL_TOKEN` | any long random string |

5. Deploy. `WEBAPP_URL` is read automatically from `KOYEB_PUBLIC_DOMAIN`, so the
   Mini App buttons light up on their own.

The free instance has no persistent disk, so `shop.db` resets whenever the
service restarts. That's fine for evaluating — add a volume or move to Postgres
before taking real money.


## After the first deploy

Koyeb gives you a permanent URL like `https://your-app-org.koyeb.app`. Three
things to do with it, once each — unlike a tunnel, you never redo them:

1. **Mini App** — nothing to do. `WEBAPP_URL` is read from `KOYEB_PUBLIC_DOMAIN`
   automatically, so the buttons appear on their own.

2. **Browser sign-in** — @BotFather → `/setdomain` → your bot → send
   `your-app-org.koyeb.app` (hostname only, no https://).

3. **Check it** — send `/status` to the bot. You want:
   `📱 Mini App: ✅ live` and your rails listed as visible.

## Updating later

Push to GitHub and Koyeb rebuilds automatically. No re-configuration.

## The database caveat, again

The free instance has no persistent disk, so `shop.db` resets on every restart
and every deploy. Products, stock, orders and balances all go. That's fine while
you're testing; before taking real money either attach a volume (paid) or move
to Postgres — every query lives in `db.py`.
