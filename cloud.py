"""Run History Studio on Kaggle, so no computer has to stay on.

  python cloud.py sync                         upload the code, the assets and the secrets as private Kaggle datasets
  python cloud.py day 1                        start day 1 of plan.json now (the worker pushes every stage after it)
  python cloud.py day 1 gpu                    the same, from a later stage (its folder is already on Drive)
  python cloud.py parts 3 6                    day 3 has 6 chapters: render the ones not on Drive yet, then the join
  python cloud.py tick                         let the worker look at the running video now instead of in 30 minutes
  python cloud.py test                         a 3 minute film through every stage, to check the cloud
  python cloud.py send output/<folder>         put a folder made on the Mac on Drive, so a later stage can continue it
  python cloud.py worker                       put the Cloudflare worker up (daily start, Telegram Yes and No)

Run sync again after changing the code, the voice, the music or a key.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "cloud"))
import push  # noqa: E402

CODE = ["*.py", "dashboard.html", "requirements.txt", "topics.txt", "samples", "documentary", "cloud", "fx/package.json",
        "fx/package-lock.json", "fx/remotion", "fx/hf"]
ASSETS = ["assets/my_voice.wav", "assets/my_voice.ref.wav", "assets/music", "assets/sfx", "assets/fonts",
          "assets/hyperframes", "assets/maps"]
SKIP = ("__pycache__", ".DS_Store", ".hyperframes", "node_modules")


def pack(paths, out, extra=()):
    with tarfile.open(out, "w:gz") as t:
        for pattern in paths:
            for p in sorted(ROOT.glob(pattern)):
                t.add(p, arcname=str(p.relative_to(ROOT)), filter=lambda i: None if any(x in i.name for x in SKIP) else i)
        for src, name in extra:
            t.add(src, arcname=name)


def live(slug):
    """When the dataset's file was made, as Kaggle lists it (UTC)."""
    out = subprocess.run([push.kaggle(), "datasets", "files", slug], capture_output=True, text=True).stdout
    found = re.findall(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", out)
    return datetime.strptime(found[-1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if found else None


def dataset(name, tgz):
    """A new version of a dataset, checked until Kaggle lists it. The kaggle tool once said ok for a code version that
    never appeared, and the kernels of a whole test film ran the code from before."""
    me = push.user()
    slug, start = f"{me}/history-studio-{name}", datetime.now(timezone.utc) - timedelta(seconds=30)
    with tempfile.TemporaryDirectory() as d:
        Path(d, f"{name}.tgz").write_bytes(Path(tgz).read_bytes())
        Path(d, "dataset-metadata.json").write_text(json.dumps({"id": slug, "title": slug.split("/")[1],
                                                                "licenses": [{"name": "unknown"}]}))
        k = push.kaggle()
        r = subprocess.run([k, "datasets", "version", "-p", d, "-m", "update"], capture_output=True, text=True)
        if "being created" not in r.stdout:  # the first time: the dataset does not exist yet
            r = subprocess.run([k, "datasets", "create", "-p", d], capture_output=True, text=True)
    for _ in range(40):
        made = live(slug)
        if made and made > start:
            return print(f"  {slug}: new version live ({made:%H:%M:%S} UTC)", flush=True)
        time.sleep(15)
    raise SystemExit(f"{slug}: Kaggle did not list the new version in 10 minutes ({(r.stdout + r.stderr).strip()[-300:]})")


def stamp():
    """A fingerprint of the code, written into code.tgz as VERSION: a kernel compares it with the one the worker gave
    it, so a kernel that got an older code version stops at once and says so."""
    h = hashlib.sha1()
    for pattern in CODE:
        for p in sorted(ROOT.glob(pattern)):
            for f in sorted([p] if p.is_file() else p.rglob("*")):
                if f.is_file() and not any(x in str(f) for x in SKIP):
                    h.update(str(f.relative_to(ROOT)).encode() + f.read_bytes())
    return h.hexdigest()[:12]


def sync():
    with tempfile.TemporaryDirectory() as d:
        version = stamp()
        Path(d, "VERSION").write_text(version)
        pack(CODE, f"{d}/code.tgz", [(Path(d, "VERSION"), "VERSION")])
        pack(ASSETS, f"{d}/assets.tgz")
        secrets = [(p, f"secrets/{p.name}") for p in (ROOT / "secrets").glob("*") if p.suffix in (".json", ".env")]
        pack([], f"{d}/secrets.tgz", secrets + [(Path.home() / ".kaggle" / "kaggle.json", "secrets/kaggle.json"),
                                                (ROOT / ".env.example", "secrets/env.txt")])
        for name in ("code", "assets", "secrets"):
            dataset(name, f"{d}/{name}.tgz")
    if (ROOT / "secrets" / "worker.env").exists():  # the worker pushes the same kernel.py, plan and code version
        import worker
        worker.kv_files(version=version)


def send(folder):
    import drive
    folder = Path(folder)
    job = push.job((folder / "topic.txt").read_text().strip())
    with tempfile.TemporaryDirectory() as d:
        tgz = Path(d) / f"job_{job['id']}.tgz"
        with tarfile.open(tgz, "w:gz") as t:
            t.add(folder, arcname=job["slug"], filter=lambda i: None if "/chapters/" in i.name or i.name.endswith(".mp4") else i)
        print("  on Drive:", drive.upload(tgz)[1])


def ask(path, body=None):
    """The worker decides what runs on Kaggle; cloud.py only asks it."""
    import drive
    import requests
    w = drive.env("worker")
    r = requests.post(w["WORKER_URL"] + path, json=body or {}, headers={"X-Studio-Secret": w["HOOK_SECRET"]}, timeout=120)
    r.raise_for_status()
    print(" ", r.json().get("message", r.text))


def plan_day(n):
    return next(x for x in json.loads((ROOT / "plan.json").read_text())["days"] if x["day"] == n)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["sync"]:
        sync()
    elif args[:1] == ["send"] and len(args) == 2:
        send(args[1])
    elif args[:1] == ["day"] and len(args) in (2, 3):
        ask("/start", {"day": int(args[1]), "from": args[2] if len(args) == 3 else "research"})
    elif args[:1] == ["parts"] and len(args) == 3:
        import drive
        d = plan_day(int(args[1]))
        job, n = push.job(d["title"]), int(args[2])
        done = [k for k in range(n) if drive.find(f"job_{job['id']}_ch{k}.mp4") and drive.find(f"job_{job['id']}_ch{k}.json")]
        print(f"  on Drive: chapters {[k + 1 for k in done]} of {n}")
        ask("/start", {"day": d["day"], "from": "join" if len(done) == n else "parts", "chapters": n, "done": done})
    elif args[:1] == ["test"]:  # a 3 minute film through every stage, to check the cloud before the plan goes on
        ask("/start", {"day": 0, "title": "The Battle of al-Qadisiyyah in Three Minutes",
                       "subject": "Battle of al-Qadisiyyah, 636", "minutes": 3})
    elif args[:1] == ["tick"]:
        ask("/tick")
    elif args[:1] == ["worker"]:
        import worker
        worker.deploy()
    else:
        sys.exit(__doc__.strip())
