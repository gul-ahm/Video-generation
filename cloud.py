"""Run History Studio on Kaggle, so no computer has to stay on.

  python cloud.py deploy [commit] [--no-sync]  the code of a GitHub commit onto this Mac, checked, tested, then synced
  python cloud.py sync                         upload the code, the assets and the secrets as private Kaggle datasets
  python cloud.py day 1                        start day 1 of plan.json now (the worker pushes every stage after it)
  python cloud.py day 1 gpu                    the same, from a later stage (its folder is already on Drive)
  python cloud.py parts 3 6                    day 3 has 6 chapters: render the ones not on Drive yet, then the join
  python cloud.py tick                         let the worker look at the running video now instead of in 30 minutes
  python cloud.py test                         an 8 minute film through every stage, to check the cloud
  python cloud.py test gpu                     the same film from the gpu stage, with the research already on Drive
  python cloud.py smoke                        a one minute film through every stage; passed, its code goes to the
                                               daily videos (which only ever run code a smoke film passed)
  python cloud.py check 10                     only the picture check, on the test film's first 10 pictures (minutes)
  python cloud.py send output/<folder>         put a folder made on the Mac on Drive, so a later stage can continue it
  python cloud.py worker                       put the Cloudflare worker up (daily start, Telegram Yes and No)

Run sync again after changing the code, the voice, the music or a key.
"""
import hashlib
import io
import json
import os
import re
import shutil
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
REPO = "gul-ahm/Video-generation"
# the Mac's own files, never replaced by a deploy: the keys, the channel plan, the topics, the runner that runs it
MINE = {".env.example", "plan.json", "topics.txt", "_runner.sh", "History Studio.command"}
SMOKE = ("Smoke Test: The Battle of Yarmouk in One Minute", "Battle of Yarmouk, 636")  # cloud.py smoke
TEST = ("The Battle of al-Qadisiyyah: Four Days That Ended an Empire", "Battle of al-Qadisiyyah, 636")  # cloud.py test


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


def dataset(name, tgz, file=None):
    """A new version of a dataset, checked until Kaggle lists it. The kaggle tool once said ok for a code version that
    never appeared, and the kernels of a whole test film ran the code from before."""
    me = push.user()
    slug, start = f"{me}/history-studio-{name}", datetime.now(timezone.utc) - timedelta(seconds=30)
    with tempfile.TemporaryDirectory() as d:
        Path(d, file or f"{name}.tgz").write_bytes(Path(tgz).read_bytes())
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


def stamp(patterns=CODE, extra=()):
    """A fingerprint of what a dataset holds. The code's goes into code.tgz as VERSION: a kernel compares it with the
    one the worker gave it, so a kernel that got an older code version stops at once and says so."""
    h = hashlib.sha1()
    files = [f for pattern in patterns for p in sorted(ROOT.glob(pattern)) for f in sorted([p] if p.is_file() else p.rglob("*"))]
    for f in files + [Path(x) for x in extra]:
        if f.is_file() and not any(x in str(f) for x in SKIP):
            h.update(str(f.name if f.is_absolute() and ROOT not in f.parents else f.relative_to(ROOT)).encode() + f.read_bytes())
    return h.hexdigest()[:12]


def deploy(ref="main", upload=True):
    """The code of one GitHub commit onto the Mac, instead of files copied by hand (a copied archive once put the
    empty .env.example of the repository over the keys, and a stale copy once ran instead of the new one). Every
    file of the commit but MINE is written, then read back and compared byte for byte; the files it replaced are
    kept in _ref/ and put back if the tests fail. Then sync, and the worker when worker.js changed. upload=False
    (deploy <commit> --no-sync) leaves the datasets as they are: a kernel waiting in Kaggle's queue checks the code
    version when it starts, and a sync in between would stop it."""
    import requests
    sha = requests.get(f"https://api.github.com/repos/{REPO}/commits/{ref}", timeout=60).json()["sha"]
    r = requests.get(f"https://codeload.github.com/{REPO}/tar.gz/{sha}", timeout=600)
    r.raise_for_status()
    backup = ROOT / "_ref" / f"before_{sha[:7]}_{time.strftime('%m%d_%H%M')}.tgz"
    backup.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        with tarfile.open(fileobj=io.BytesIO(r.content)) as t:
            t.extractall(d, filter="data")
        src = next(Path(d).iterdir())
        files = {str(f.relative_to(src)): f for f in src.rglob("*") if f.is_file()}
        files = {k: f for k, f in files.items() if k not in MINE}
        changed = [k for k, f in files.items() if not (ROOT / k).exists() or (ROOT / k).read_bytes() != f.read_bytes()]
        with tarfile.open(backup, "w:gz") as b:
            for k in changed:
                if (ROOT / k).exists():
                    b.add(ROOT / k, arcname=k)
        for k in changed:  # a new file renamed into place: a running script keeps reading the one it opened
            (ROOT / k).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(files[k], ROOT / f"{k}.new")
            os.replace(ROOT / f"{k}.new", ROOT / k)
        wrong = [k for k, f in files.items() if (ROOT / k).read_bytes() != f.read_bytes()]
        if wrong:
            raise SystemExit(f"deploy: {len(wrong)} files differ after writing: {wrong[:5]}")
    print(f"  deploy: {sha[:7]}, {len(changed)} of {len(files)} files changed, all match the commit", flush=True)
    tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "tests"], cwd=ROOT, capture_output=True,
                           text=True)
    print("  tests:", (tests.stderr.strip().splitlines() or ["none ran"])[-1], flush=True)
    if tests.returncode:
        with tarfile.open(backup) as b:
            before = set(b.getnames())
            b.extractall(ROOT, filter="data")
        for k in changed:
            if k not in before:
                (ROOT / k).unlink()
        raise SystemExit(f"deploy: the tests failed, the files from before are back\n{tests.stderr[-1500:]}")
    (ROOT / "DEPLOYED").write_text(f"{sha}\n")
    if upload:
        sync()
    if "cloud/cloudflare/worker.js" in changed:
        import worker
        worker.deploy()


def sync():
    """The three datasets, each only when what it holds changed since the last sync that Kaggle confirmed (the
    assets took 1.5 hours to upload and process when they had not changed). The fingerprints live in .sync.json."""
    env = dict(l.split("=", 1) for l in (ROOT / ".env.example").read_text().splitlines() if "=" in l and l[0] != "#")
    empty = [k for k in ("LLM_API_KEY", "HF_TOKEN") if not env.get(k, "").strip()]
    if empty:  # an empty key on Kaggle fails every video at research; stop here instead
        raise SystemExit(f"{', '.join(empty)} empty in .env.example; nothing uploaded")
    secrets = [(p, f"secrets/{p.name}") for p in (ROOT / "secrets").glob("*") if p.suffix in (".json", ".env")]
    secrets += [(Path.home() / ".kaggle" / "kaggle.json", "secrets/kaggle.json"), (ROOT / ".env.example", "secrets/env.txt")]
    version = stamp()
    now = {"code": version, "assets": stamp(ASSETS), "secrets": stamp([], [p for p, _ in secrets])}
    done = json.loads((ROOT / ".sync.json").read_text()) if (ROOT / ".sync.json").exists() else {}
    with tempfile.TemporaryDirectory() as d:
        Path(d, "VERSION").write_text(version)
        for name, paths, extra in (("code", CODE, [(Path(d, "VERSION"), "VERSION")]), ("assets", ASSETS, []),
                                   ("secrets", [], secrets)):
            if done.get(name) == now[name]:
                print(f"  history-studio-{name}: unchanged, not uploaded", flush=True)
                continue
            pack(paths, f"{d}/{name}.tgz", extra)
            dataset(name, f"{d}/{name}.tgz")
            done[name] = now[name]
            (ROOT / ".sync.json").write_text(json.dumps(done))
    if (ROOT / "secrets" / "worker.env").exists():  # the worker pushes the same kernel.py, plan and code version
        import worker
        worker.kv_files(version=version)


def smoke():
    """A one minute film through every stage on the code just synced; when the join delivers it, that code becomes
    the code of the daily videos (promote). A change reaches them only after a whole film went through: before,
    each new piece was first tried on a real day's video and a bug was found two hours in."""
    import worker
    title, subject = SMOKE
    for _ in range(36):  # a film still running goes first (six hours at most)
        if "still running" not in ask("/start", {"day": 0, "title": title, "subject": subject, "minutes": 1, "smoke": True}):
            break
        time.sleep(600)
    else:
        raise SystemExit("the smoke film could not start: other videos ran for six hours")
    cf, key = worker.Cloudflare(), f"job:{push.job(title)['id']}"
    space = cf.kv()
    for _ in range(180):  # three hours at most; a smoke film takes about forty minutes
        time.sleep(60)
        job = json.loads(cf.get(space, key) or "{}")
        if job.get("status") not in (None, "running"):
            break
    if job.get("status") != "ready":
        raise SystemExit(f"smoke film {job.get('status', 'not started')}: {job.get('error', '')[:600]}\n"
                         "The daily videos keep the code they had.")
    promote()


def promote():
    """The code that passed the smoke film into history-studio-code-good, which the daily videos run."""
    import worker
    cf = worker.Cloudflare()
    space, version = cf.kv(), stamp()
    if cf.get(space, "smoke_passed") != version:
        raise SystemExit(f"the code here ({version}) is not the code the last smoke film passed; run: python cloud.py smoke")
    with tempfile.TemporaryDirectory() as d:
        Path(d, "VERSION").write_text(version)
        pack(CODE, f"{d}/code.tgz", [(Path(d, "VERSION"), "VERSION")])
        dataset("code-good", f"{d}/code.tgz", file="code.tgz")
    cf.put(space, "good_version", version)
    print(f"  the daily videos now run code {version}", flush=True)


def send(folder):
    import drive
    folder = Path(folder)
    job = push.job((folder / "topic.txt").read_text().strip())
    with tempfile.TemporaryDirectory() as d:
        tgz = Path(d) / f"job_{job['id']}.tgz"
        with tarfile.open(tgz, "w:gz") as t:
            t.add(folder, arcname=job["slug"], filter=lambda i: None if "/chapters/" in i.name or i.name.endswith(".mp4") else i)
        print("  on Drive:", drive.upload(tgz)[1])


def check(n):
    """The picture check alone on the test film's first n pictures, already on Drive, on a Kaggle GPU: nothing is
    drawn and no film is made, so a change to the check is tried in minutes before a whole video depends on it."""
    with tempfile.TemporaryDirectory() as d:
        title, subject = TEST
        run = {**push.job(title, 8, subject=subject, day=0), "stage": "check", "limit": n,
               "code": json.loads((ROOT / ".sync.json").read_text())["code"]}
        Path(d, "kernel.py").write_text((ROOT / "cloud" / "kernel.py").read_text().replace("__RUN__", json.dumps(run)))
        ref = f"{push.user()}/studio-check"
        Path(d, "kernel-metadata.json").write_text(json.dumps({
            "id": ref, "title": "studio-check", "code_file": "kernel.py", "language": "python", "kernel_type": "script",
            "is_private": True, "enable_gpu": True, "enable_internet": True, "competition_sources": [],
            "kernel_sources": [], "dataset_sources": [f"{push.user()}/history-studio-{x}" for x in ("code", "assets", "secrets")]}))
        kg = push.kaggle()
        subprocess.run([kg, "kernels", "push", "-p", d], check=True)
        for _ in range(120):
            time.sleep(60)
            state = subprocess.run([kg, "kernels", "status", ref], capture_output=True, text=True).stdout
            if re.search(r"COMPLETE|ERROR|CANCEL", state):
                break
        print(" ", state.strip())
        subprocess.run([kg, "kernels", "output", ref, "-p", d], capture_output=True)
        log = next(Path(d).glob("*.log"), None)
        lines = "".join(e.get("data", "") for e in json.loads(log.read_text())) if log else ""
        print("\n".join(l for l in lines.splitlines() if re.search(r"vision|pictures|earlier|misses|problem|fit|Error|exit", l)))


def ask(path, body=None):
    """The worker decides what runs on Kaggle; cloud.py only asks it."""
    import drive
    import requests
    w = drive.env("worker")
    r = requests.post(w["WORKER_URL"] + path, json=body or {}, headers={"X-Studio-Secret": w["HOOK_SECRET"]}, timeout=120)
    r.raise_for_status()
    said = r.json().get("message", r.text)
    print(" ", said, flush=True)
    return said


def plan_day(n):
    return next(x for x in json.loads((ROOT / "plan.json").read_text())["days"] if x["day"] == n)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["deploy"] and 1 <= len(args) <= 3:
        deploy(next((a for a in args[1:] if not a.startswith("--")), "main"), upload="--no-sync" not in args)
    elif args[:1] == ["sync"]:
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
    elif args[:1] == ["test"] and len(args) in (1, 2):  # an 8 minute film through every stage, or from one (test gpu)
        ask("/start", {"day": 0, "title": TEST[0], "subject": TEST[1], "minutes": 8,
                       "from": args[1] if len(args) == 2 else "research"})
    elif args == ["smoke"]:
        smoke()
    elif args == ["promote"]:
        promote()
    elif args[:1] == ["check"] and len(args) in (1, 2):
        check(int(args[1]) if len(args) == 2 else 10)
    elif args[:1] == ["tick"]:
        ask("/tick")
    elif args[:1] == ["worker"]:
        import worker
        worker.deploy()
    else:
        sys.exit(__doc__.strip())
