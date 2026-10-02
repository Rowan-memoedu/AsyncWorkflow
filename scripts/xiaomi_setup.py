import argparse
import json
import time
from urllib.parse import urlsplit
from async_workflow.core import Settings, Attention, atomic
from async_workflow.xiaomi import connection, request, configure, status

p=argparse.ArgumentParser()
p.add_argument("action", choices=["status", "configure", "login"])
p.add_argument("--account-id", type=int, help="Explicit local account ID after cloud access verification")
args=p.parse_args()
cfg=Settings.load()
if args.action == "status":
    print(json.dumps(status(cfg)))
elif args.action == "configure":
    print(json.dumps(configure(cfg, account_id=args.account_id)))
else:
    with connection(cfg) as client:
        qr=request(client, "POST", "/api/account/qr-login")
        address=urlsplit(qr["qrUrl"])
        if address.scheme != "https" or address.hostname != "account.xiaomi.com":
            raise Attention("unexpected_xiaomi_qr_host")
        # This contains only the short-lived login QR URL, never account credentials.
        atomic(cfg.root / "xiaomi-login.json", {"qr_url":qr["qrUrl"], "status":"WAITING"})
        print(json.dumps({"qr_ready":True, "expires_seconds":300}), flush=True)
        for _ in range(65):
            result=request(client,"GET","/api/account/qr-login/" + qr["sessionId"])
            if result["status"] != "WAITING":
                atomic(cfg.root / "xiaomi-login.json", {"status":result["status"]})
                print(json.dumps({"status":result["status"]}), flush=True)
                break
            time.sleep(5)
        else:
            atomic(cfg.root / "xiaomi-login.json", {"status":"EXPIRED"})
            print(json.dumps({"status":"EXPIRED"}), flush=True)
