# Start here

Everything is in this folder. Three commands and the bot is running.

## 1. Open PowerShell in this folder

Right-click the folder in File Explorer → **Open in Terminal**.
(Or in VS Code: **Terminal → New Terminal**.)

## 2. Run setup once

```powershell
.\setup.ps1
```

If Windows blocks the script with "running scripts is disabled", run this once
and try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

`setup.ps1` checks every file is present, builds the virtual environment,
installs the dependencies, creates `.env`, and opens it in Notepad so you can
fill in two values:

| Key | Where to get it |
|---|---|
| `BOT_TOKEN` | message [@BotFather](https://t.me/BotFather) → `/newbot` |
| `ADMIN_IDS` | message [@userinfobot](https://t.me/userinfobot) → it replies with your number |

Save and close Notepad. While you're in the file, these two are worth setting:

```
ENABLED_PROVIDERS=balance,stars
DB_PATH=C:\shopbot\shop.db
```

`balance,stars` are the two payment methods that need zero extra setup. The
`DB_PATH` keeps the database out of any OneDrive-synced folder — OneDrive
corrupts live SQLite files.

## 3. Start it

```powershell
.\run.ps1
```

Working looks like this:

```
INFO shopbot | running as @YourBot | providers: balance, stars | mode: polling
WARNING shopbot | WEBAPP_URL is not a public https:// address ...
INFO webapp  | mini apps on http://0.0.0.0:8080 (public: not set)
INFO watcher | payment watcher started (every 25s)
```

**The warning is normal.** Telegram won't open a Mini App from `localhost`, so
the bot uses its inline menus instead — everything works. Send `/start` to your
bot.

Stop it with `Ctrl+C`. Start it again any time with `.\run.ps1`.

## First things to try

1. Send `/admin` to your bot.
2. **Categories → Add category** → name it anything.
3. Open it → **Add product** → follow the three prompts.
4. **Add stock** → paste a few lines, one per line.
5. **Users** → find yourself → **Add balance** → 1000.
6. Send `/start` → **Shop** → buy the product with wallet balance.

You should receive the stock lines instantly, and the stock count should drop.

## Admin panel in a browser

Add a token to `.env`:

```
ADMIN_PANEL_TOKEN=pick-any-long-random-string
```

Restart, then open:

```
http://localhost:8080/admin?token=pick-any-long-random-string
```

## Mini Apps inside Telegram

These need a public HTTPS address. In a **second** PowerShell window, leaving the
bot running in the first:

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8080
```

Copy the `https://....trycloudflare.com` address it prints into `.env` as
`WEBAPP_URL`, restart the bot, and `/start` now shows **Open shop** and
**Admin panel** as Mini App buttons. The tunnel address changes every restart.

## Check everything without Telegram

```powershell
.\.venv\Scripts\python.exe test_web.py
```

20 checks against a throwaway database. Useful after any edit.

## When something goes wrong

| Message | Fix |
|---|---|
| `Unauthorized` | `BOT_TOKEN` in `.env` is wrong or still the placeholder |
| `ModuleNotFoundError` | use `.\run.ps1`, not a bare `python bot.py` |
| `address already in use` | an old copy is running — `Get-Process python \| Stop-Process` |
| Admin panel says "No access" | `ADMIN_IDS` doesn't match the account you're messaging from |
| `&&` errors | PowerShell doesn't support `&&` — run lines one at a time |

Full documentation is in `README.md`. Deployment to a free always-on host is in
`koyeb.md`.
