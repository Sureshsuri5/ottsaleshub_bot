# Read this first

Extract so the files land **directly** in your project folder — you should see
`bot.py` next to a `static` folder, with no nested `shopbot\shopbot`.

## Confirm you got the new files

Open PowerShell in the folder containing `bot.py`:

```powershell
Select-String -Path static\shop.html -Pattern "__buildStamp" -Quiet
```

Must print **True**. If it prints False or errors, the extract went to the wrong
place — check with `Get-ChildItem -Directory` for a nested `shopbot` folder.

## Then run

```powershell
python bot.py
```

In a second terminal:

```powershell
curl http://localhost:8080/build
```

Expect: `{"build":"20260802-1431","files":["admin.html","app.css","shop.html","tg.js"]}`

A 404 here means an older copy of the bot is running. Stop every python process
(`Get-Process python | Stop-Process -Force`) and start again from this folder.

## Mini App

```powershell
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:8080
```

Copy the printed `https://….trycloudflare.com` into `.env` as `WEBAPP_URL`,
restart the bot. The URL changes every time the tunnel restarts.

## If a screen is blank

It won't be silent any more — this build shows a red panel naming the exact
file, line and error. Send that text on.
