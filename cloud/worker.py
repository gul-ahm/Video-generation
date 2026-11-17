"""Put the History Studio worker on Cloudflare and point the Telegram bot at it (run on the Mac).

    python cloud.py worker

Uses secrets/cloudflare.env, secrets/telegram.env and ~/.kaggle/kaggle.json. Makes the KV store, uploads
cloud/cloudflare/worker.js with the keys as secrets, turns on its workers.dev address and its half hourly timer, puts
cloud/kernel.py and plan.json in KV, sets the Telegram webhook and writes secrets/worker.env (the address and the
shared secret the Kaggle kernels use to report back). Safe to run again: the KV store, the state and the secret stay.
"""
import json
import secrets
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SECRETS = ROOT / "secrets"
NAME = "history-studio"
TICK = "*/30 * * * *"  # every half hour the worker starts the next video when nothing runs and the day has room


def env(name):
    f = SECRETS / f"{name}.env"
    return dict(l.split("=", 1) for l in f.read_text().split() if "=" in l) if f.exists() else {}


class Cloudflare:
    def __init__(self):
        c = env("cloudflare")
        self.base = f"https://api.cloudflare.com/client/v4/accounts/{c['CLOUDFLARE_ACCOUNT_ID']}"
        self.auth = {"Authorization": f"Bearer {c['CLOUDFLARE_API_TOKEN']}"}

    def call(self, method, path, ok=(200,), **kw):
        r = requests.request(method, self.base + path, headers=self.auth, timeout=60, **kw)
        if r.status_code not in ok:
            raise SystemExit(f"Cloudflare {method} {path}: {r.status_code} {r.text[:300]}")
        return r.json().get("result") if r.headers.get("content-type", "").startswith("application/json") else r.text

    def kv(self):
        spaces = self.call("GET", "/storage/kv/namespaces", params={"per_page": 100})
        found = next((s["id"] for s in spaces if s["title"] == NAME), None)
        return found or self.call("POST", "/storage/kv/namespaces", json={"title": NAME})["id"]

    def put(self, space, key, value):
        self.call("PUT", f"/storage/kv/namespaces/{space}/values/{key}", data=value.encode())

    def get(self, space, key):
        r = requests.get(f"{self.base}/storage/kv/namespaces/{space}/values/{key}", headers=self.auth, timeout=60)
        return r.text if r.ok else None

    def address(self):
        sub = requests.get(self.base + "/workers/subdomain", headers=self.auth, timeout=60).json().get("result") or {}
        if not sub.get("subdomain"):  # the account never had a workers.dev name
            try:
                sub = self.call("PUT", "/workers/subdomain", json={"subdomain": f"gulzar-{secrets.token_hex(3)}"})
            except SystemExit as err:
                raise SystemExit(f"{err}\nOpen dash.cloudflare.com, Workers & Pages, once: it makes the workers.dev "
                                 "name. Then run this again.")
        self.call("POST", f"/workers/scripts/{NAME}/subdomain", json={"enabled": True, "previews_enabled": False})
        return f"https://{NAME}.{sub['subdomain']}.workers.dev"


def kv_files(cf=None, space=None):
    """cloud/kernel.py and plan.json into KV, so the daily start uses the code and the plan you have now."""
    cf = cf or Cloudflare()
    space = space or cf.kv()
    cf.put(space, "kernel", (HERE / "kernel.py").read_text())
    if (ROOT / "plan.json").exists():
        cf.put(space, "plan", (ROOT / "plan.json").read_text())
    print("  worker: kernel.py and plan.json in KV", flush=True)


def deploy():
    cf, tg = Cloudflare(), env("telegram")
    kaggle = json.loads((Path.home() / ".kaggle" / "kaggle.json").read_text())
    old = env("worker")
    secret = old.get("HOOK_SECRET") or secrets.token_urlsafe(32)
    space = cf.kv()

    keys = {"TELEGRAM_BOT_TOKEN": tg["TELEGRAM_BOT_TOKEN"], "TELEGRAM_CHAT_ID": tg["TELEGRAM_CHAT_ID"],
            "KAGGLE_USERNAME": kaggle["username"], "KAGGLE_KEY": kaggle["key"], "HOOK_SECRET": secret}
    meta = {"main_module": "worker.js", "compatibility_date": "2025-06-01",
            "bindings": [{"type": "kv_namespace", "name": "KV", "namespace_id": space}] +
                        [{"type": "secret_text", "name": k, "text": v} for k, v in keys.items()]}
    code = (HERE / "cloudflare" / "worker.js").read_bytes()
    cf.call("PUT", f"/workers/scripts/{NAME}", files={
        "metadata": (None, json.dumps(meta), "application/json"),
        "worker.js": ("worker.js", code, "application/javascript+module")})
    print("  worker: uploaded", flush=True)
    url = cf.address()  # first: Cloudflare takes no timer before the account has its workers.dev name
    cf.call("PUT", f"/workers/scripts/{NAME}/schedules", json=[{"cron": TICK}])
    print(f"  worker: {url}, checks {TICK} (UTC)", flush=True)

    kv_files(cf, space)
    if cf.get(space, "state") is None:  # the first time: nothing starts by itself until you send /resume
        cf.put(space, "state", json.dumps({"next_day": 1, "paused": True, "running": None, "last_slot": None}))

    (SECRETS / "worker.env").write_text(f"WORKER_URL={url}\nHOOK_SECRET={secret}\n")
    (SECRETS / "worker.env").chmod(0o600)

    r = requests.post(f"https://api.telegram.org/bot{tg['TELEGRAM_BOT_TOKEN']}/setWebhook", timeout=30, json={
        "url": f"{url}/telegram", "secret_token": secret, "allowed_updates": ["message", "callback_query"]}).json()
    print(f"  telegram webhook: {r.get('description') or r}", flush=True)

    for _ in range(12):  # a new workers.dev address takes a minute to answer
        try:
            r = requests.get(f"{url}/check", headers={"X-Studio-Secret": secret}, timeout=30)
            if r.ok:
                print(f"  check: {r.text}", flush=True)
                return
        except requests.RequestException:
            pass
        time.sleep(10)
    print("  check: the address did not answer yet; run again in a few minutes", flush=True)


if __name__ == "__main__":
    deploy()
