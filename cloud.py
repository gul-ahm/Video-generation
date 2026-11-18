"""Run History Studio on Kaggle, so no computer has to stay on.

  python cloud.py sync                         upload the code, the assets and the secrets as private Kaggle datasets
  python cloud.py start "Title" [minutes]      start a video: research, then the GPU stage, then the render, each a
                                               Kaggle kernel that starts the next; the video arrives on Drive and Telegram
  python cloud.py start "Title" 22 gpu         start at a later stage, when the video folder is already on Drive
  python cloud.py day 1                        start day 1 of the plan in plan.json (title, length and its event)
  python cloud.py day 1 gpu                    the same, from a later stage (its folder is already on Drive)
  python cloud.py parts 3 6                    day 3 has 6 chapters: render the ones not on Drive yet, or join them
  python cloud.py send output/<folder>         put a folder made on the Mac on Drive, so a later stage can continue it
  python cloud.py worker                       put the Cloudflare worker up (daily start, Telegram Yes and No)

Run sync again after changing the code, the voice, the music or a key.
"""
import json
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


def dataset(name, tgz):
    me, slug = push.user(), f"history-studio-{name}"
    with tempfile.TemporaryDirectory() as d:
        Path(d, f"{name}.tgz").write_bytes(Path(tgz).read_bytes())
        Path(d, "dataset-metadata.json").write_text(json.dumps({"id": f"{me}/{slug}", "title": slug,
                                                                "licenses": [{"name": "unknown"}]}))
        k = push.kaggle()
        r = subprocess.run([k, "datasets", "version", "-p", d, "-m", "update", "-q"], capture_output=True, text=True)
        if r.returncode:  # the first time: the dataset does not exist yet
            r = subprocess.run([k, "datasets", "create", "-p", d, "-q"], capture_output=True, text=True)
    print(f"  {slug}: {'ok' if not r.returncode else (r.stderr or r.stdout).strip()[-200:]}", flush=True)


def sync():
    with tempfile.TemporaryDirectory() as d:
        pack(CODE, f"{d}/code.tgz")
        pack(ASSETS, f"{d}/assets.tgz")
        secrets = [(p, f"secrets/{p.name}") for p in (ROOT / "secrets").glob("*") if p.suffix in (".json", ".env")]
        pack([], f"{d}/secrets.tgz", secrets + [(Path.home() / ".kaggle" / "kaggle.json", "secrets/kaggle.json"),
                                                (ROOT / ".env.example", "secrets/env.txt")])
        for name in ("code", "assets", "secrets"):
            dataset(name, f"{d}/{name}.tgz")
    if (ROOT / "secrets" / "worker.env").exists():  # the daily start pushes the same kernel.py and plan
        import worker
        worker.kv_files()


def send(folder):
    import drive
    folder = Path(folder)
    job = push.job((folder / "topic.txt").read_text().strip())
    with tempfile.TemporaryDirectory() as d:
        tgz = Path(d) / f"job_{job['id']}.tgz"
        with tarfile.open(tgz, "w:gz") as t:
            t.add(folder, arcname=job["slug"], filter=lambda i: None if "/chapters/" in i.name or i.name.endswith(".mp4") else i)
        print("  on Drive:", drive.upload(tgz)[1])


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["sync"]:
        sync()
    elif args[:1] == ["send"] and len(args) == 2:
        send(args[1])
    elif args[:1] == ["day"] and len(args) in (2, 3):
        d = next(x for x in json.loads((ROOT / "plan.json").read_text())["days"] if x["day"] == int(args[1]))
        push.push(args[2] if len(args) == 3 else "research", push.job(d["title"], d.get("minutes", 22), subject=d["subject"], day=d["day"]))
    elif args[:1] == ["parts"] and len(args) == 3:
        import drive
        d = next(x for x in json.loads((ROOT / "plan.json").read_text())["days"] if x["day"] == int(args[1]))
        job, n = push.job(d["title"], d.get("minutes", 22), subject=d["subject"], day=d["day"]), int(args[2])
        missing = [k for k in range(n) if not drive.find(f"job_{job['id']}_ch{k}.mp4")]
        for k in missing:
            push.push("part", {**job, "part": k, "chapters": n})
        if not missing:
            push.push("join", {**job, "chapters": n})
    elif args[:1] == ["worker"]:
        import worker
        worker.deploy()
    elif args[:1] == ["start"] and len(args) >= 2:
        push.push(args[3] if len(args) > 3 else "research", push.job(args[1], int(args[2]) if len(args) > 2 else 22))
    else:
        sys.exit(__doc__.strip())
