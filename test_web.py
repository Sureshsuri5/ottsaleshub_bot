"""Integration test for the Mini App server.

Runs against a throwaway database and a stub bot — safe to run any time.
    python test_web.py
"""
import asyncio, hashlib, hmac, json, os, time, urllib.parse, sys
os.environ["BOT_TOKEN"]="777:TESTTOKEN"; os.environ["ADMIN_IDS"]="42"
os.environ["DB_PATH"]="/tmp/w.db"; os.environ["ADMIN_PANEL_TOKEN"]="devtok"
os.environ["ENABLED_PROVIDERS"]="balance,stars,crypto,upi"
# rails now have to be *configured* to be usable, not merely listed — give the
# ones under test somewhere to receive money
os.environ.setdefault("TRON_ADDRESS", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
os.environ.setdefault("UPI_VPA", "test@upi")
os.environ.setdefault("UPI_PAYEE_NAME", "Test Payee")
os.environ.setdefault("INR_RATE", "88")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for _f in ("/tmp/w.db", "/tmp/w.db-wal", "/tmp/w.db-shm"):
    if os.path.exists(_f): os.remove(_f)

import db, webapp
from aiohttp import web, ClientSession
from config import cfg

class StubBot:
    async def send_message(self,*a,**k): pass
    async def create_invoice_link(self,**k): return "https://t.me/invoice/xyz"

def sign(user):
    d={"auth_date":str(int(time.time())),"query_id":"AAA","user":json.dumps(user)}
    dcs="\n".join(f"{k}={d[k]}" for k in sorted(d))
    secret=hmac.new(b"WebAppData",cfg.bot_token.encode(),hashlib.sha256).digest()
    d["hash"]=hmac.new(secret,dcs.encode(),hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(d)

async def main():
    await db.init(cfg.db_path)
    cid=await db.add_category("Ebooks"); pid=await db.add_product(cid,"Guide","PDF",249.0)
    await db.add_stock(pid,["K-1","K-2","K-3"])
    await db.upsert_user(42,"boss","Boss"); await db.add_balance(42,1000)

    app=webapp.build_app(StubBot())
    runner=web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner,"127.0.0.1",8099).start()
    B="http://127.0.0.1:8099"
    ok=lambda c: "\033[92mPASS\033[0m" if c else "\033[91mFAIL\033[0m"

    async with ClientSession() as s:
        # 1. the shopfront is public, but it must not leak an identity
        r=await s.get(f"{B}/api/me"); j=await r.json()
        print(ok(r.status==200 and j["signed_in"] is False),
              "anonymous /me is public and signed_in=false ->",r.status)
        r=await s.get(f"{B}/api/orders")
        print(ok(r.status==401),"personal data still needs auth ->",r.status)
        # 2. forged initData must not authenticate
        bad=sign({"id":42})+"x"
        r=await s.get(f"{B}/api/me",headers={"X-Init-Data":bad}); j=await r.json()
        print(ok(j["signed_in"] is False),"tampered initData does not sign anyone in")
        r=await s.get(f"{B}/api/orders",headers={"X-Init-Data":bad})
        print(ok(r.status==401),"tampered initData rejected on private data ->",r.status)
        # 3. valid initData
        H={"X-Init-Data":sign({"id":42,"username":"boss","first_name":"Boss"})}
        r=await s.get(f"{B}/api/me",headers=H); me=await r.json()
        print(ok(r.status==200 and me["admin"]),"valid initData ->",me["id"],"admin:",me["admin"])
        # 4. non-admin blocked from admin API
        H2={"X-Init-Data":sign({"id":999,"first_name":"Rando"})}
        r=await s.get(f"{B}/api/admin/stats",headers=H2)
        print(ok(r.status==403),"non-admin blocked ->",r.status)
        # 5. dev token
        D={"X-Admin-Token":"devtok"}
        r=await s.get(f"{B}/api/admin/stats",headers=D); st=await r.json()
        print(ok(r.status==200),"dev token ->",st["users"],"users")
        # 6. catalog
        r=await s.get(f"{B}/api/catalog",headers=H2); cat=await r.json()
        print(ok(cat[0]["products"][0]["stock"]==3),"catalog stock ->",cat[0]["products"][0]["stock"])
        # 7. checkout w/ balance (admin has 1000)
        r=await s.post(f"{B}/api/checkout",headers=H,json={"kind":"purchase","product_id":pid,"qty":2,"provider":"balance"})
        j=await r.json(); print(ok(j.get("instant")),"balance checkout ->",j)
        o=await db.order(j["order_id"]); print(ok(o["status"]=="delivered"),"delivered ->",o["status"],repr(o["delivered_text"]))
        # 8. price cannot be forged from client
        r=await s.post(f"{B}/api/checkout",headers=H,json={"kind":"purchase","product_id":pid,"qty":1,"provider":"balance","amount":1})
        j=await r.json(); o=await db.order(j["order_id"])
        print(ok(o["amount"]==249.0),"client price ignored -> charged",o["amount"])
        # 9. oversell blocked
        r=await s.post(f"{B}/api/checkout",headers=H,json={"kind":"purchase","product_id":pid,"qty":50,"provider":"balance"})
        print(ok(r.status==409),"oversell blocked ->",r.status)
        # 10. crypto invoice + QR
        r=await s.post(f"{B}/api/checkout",headers=H,json={"kind":"topup","amount":500,"provider":"crypto"})
        inv=await r.json(); print(ok(inv["qr"] and inv["pay_unit"]=="USDT"),"crypto invoice ->",inv["pay_amount"],inv["pay_unit"])
        q=urllib.parse.quote(sign({"id":42,"first_name":"Boss"}))
        r=await s.get(f"{B}/api/order/{inv['order_id']}/qr?_auth={q}")
        print(ok(r.status==200 and r.content_type=="image/png"),"QR png ->",r.content_type,len(await r.read()),"bytes")
        # 11. stars invoice link
        r=await s.post(f"{B}/api/checkout",headers=H,json={"kind":"topup","amount":100,"provider":"stars"})
        j=await r.json(); print(ok("invoice_link" in j),"stars link ->",j.get("invoice_link"))
        # 12. cannot read someone else's order
        r=await s.get(f"{B}/api/order/1",headers=H2)
        print(ok(r.status==404),"foreign order hidden ->",r.status)
        # 13. admin add stock + product patch
        r=await s.post(f"{B}/api/admin/product/{pid}/stock",headers=D,json={"lines":"K-4\nK-5"})
        print(ok((await r.json())["stock"]==2),"stock added ->",await db.stock_count(pid))
        r=await s.patch(f"{B}/api/admin/product/{pid}",headers=D,json={"price":99,"sold_count":9999})
        p=await r.json(); print(ok(p["price"]==99 and p["sold_count"]!=9999),"field allowlist -> price",p["price"],"sold",p["sold_count"])
        # 14. manual order: closing without a refund keeps the money, and the
        # buyer must never be left worse off by accident — so an unexplained
        # close is refused, and the held wallet share cannot come back later.
        moid=await db.create_order(user_id=42,product_id=None,product_name="Manual",
            qty=1,amount=300.0,balance_used=200.0,provider="upi",
            status="fulfilling",kind="purchase")
        await db.open_fulfilment(moid,42)
        r=await s.post(f"{B}/api/admin/fulfil/{moid}",headers=D,json={"action":"close"})
        print(ok(r.status==400),"close without a reason refused ->",r.status)
        before=(await db.get_user(42))["balance"]
        r=await s.post(f"{B}/api/admin/fulfil/{moid}",headers=D,
                       json={"action":"close","reason":"number was not yours"})
        mo=await db.order(moid)
        print(ok(r.status==200 and mo["status"]=="cancelled"
                 and (await db.get_user(42))["balance"]==before),
              "closed with no refund ->",mo["status"])
        print(ok(float(mo["balance_used"] or 0)==0),
              "held wallet share written off, not left for the pruner ->",mo["balance_used"])
        r=await s.post(f"{B}/api/admin/fulfil/{moid}",headers=D,
                       json={"action":"close","reason":"again"})
        print(ok(r.status==409),"second close refused ->",r.status)
        # a maker must not reach it — money stays the shop owner's decision
        _maker=open("webapp.py",encoding="utf-8").read() \
            .split("async def maker_action")[1].split("\nasync def ")[0]
        print(ok('act == "close"' not in _maker and 'act == "cancel"' not in _maker),
              "makers cannot close or cancel an order")
        # 15. active-user count tracks the 30-day window Telegram measures
        await db.upsert_user(4242,"lurker","Lurker")
        await db.ex("UPDATE users SET last_seen = datetime('now','-45 days') "
                    "WHERE tg_id = ?",(4242,))
        r=await s.get(f"{B}/api/admin/stats",headers=D); st=await r.json()
        print(ok(st["mau"]<st["users"]),
              f"30-day active excludes a dormant user -> {st['mau']} of {st['users']}")
        await db.upsert_user(4242,"lurker","Lurker")
        r=await s.get(f"{B}/api/admin/stats",headers=D); st2=await r.json()
        print(ok(st2["mau"]==st["mau"]+1),
              f"touching the bot counts them again -> {st2['mau']}")
        # 16. static files
        for path in ("/","/admin","/static/app.css","/static/tg.js"):
            r=await s.get(f"{B}{path}"); print(ok(r.status==200),f"serve {path} ->",r.status)
    await runner.cleanup(); await db.close()

asyncio.run(main())
