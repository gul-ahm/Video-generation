"""One stage of a video on Kaggle. The same file runs every stage; the worker writes the stage and the job into RUN.

  research  (CPU)            web research, claims, article screenshots, script
  gpu       (2 x T4)         edit plan, your voice, the pictures, the picture check, for all the day's videos
  part      (CPU, one each)  one chapter rendered with its effects and sound; all chapters render at the same time
  join      (CPU)            the chapters put together, quality gate, YouTube pack; the film goes to Drive

Each stage takes the video folder from Drive (job_<id>.tgz), does its part and reports to the Cloudflare worker
(cloud/cloudflare/worker.js), which pushes the next stage and writes to Telegram. A kernel never pushes another one:
when they did, one broken Drive marker made every chapter push all six again. Only the GPU stage uses GPU hours.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections import deque
from pathlib import Path

RUN = json.loads(r"""__RUN__""")
# the work happens in /kaggle/temp, which Kaggle does not keep: the run's output is then only its log, a few kB to
# download when something has to be checked (from /kaggle/working it was the whole studio, 25 minutes to fetch)
W = Path("/kaggle/temp")
HOME = W / "studio"
NODE = "https://nodejs.org/dist/v22.12.0/node-v22.12.0-linux-x64.tar.xz"


def sh(cmd, **kw):
    print("$", cmd if isinstance(cmd, str) else " ".join(map(str, cmd)), flush=True)
    return subprocess.run(cmd, shell=isinstance(cmd, str), check=kw.pop("check", True), **kw)


def unpack():
    """Code, assets and secrets from the three private datasets; secrets also give Kaggle's own token."""
    HOME.mkdir(parents=True, exist_ok=True)
    for name in ("code", "assets", "secrets"):
        tgz = next(Path("/kaggle/input").rglob(f"{name}.tgz"), None)
        if tgz:
            with tarfile.open(tgz) as t:
                t.extractall(HOME, filter="fully_trusted")
        else:  # Kaggle unpacked the archive itself
            shutil.copytree(next(Path("/kaggle/input").glob(f"*{name}*")), HOME, dirs_exist_ok=True)
    made = (HOME / "VERSION").read_text().strip() if (HOME / "VERSION").exists() else "none"
    if RUN.get("code") and made != RUN["code"]:
        raise RuntimeError(f"Kaggle mounted code version {made}, the worker sent {RUN['code']}: "
                           "run `python cloud.py sync` again")
    kag = Path.home() / ".kaggle"
    kag.mkdir(exist_ok=True)
    shutil.copy(HOME / "secrets" / "kaggle.json", kag / "kaggle.json")
    (kag / "kaggle.json").chmod(0o600)
    # IMAGE_ENGINE: a picture's file name comes from the engine, which follows the device; without it the CPU render
    # looked for other names than the GPU stage had drawn and found none of the 114 pictures
    extra = {"CLOUD": "1", "KAGGLE_LOCAL": "1", "KAGGLE": "1", "MAC_PICTURES": "0", "CHATTER_BACKEND": "torch",
             "ASK_CLEAN": "0", "PREWRITE": "0", "IMAGE_ENGINE": "local", "STRICT": "1",
             **({"SMOKE": "1"} if RUN.get("smoke") else {})}
    lines = (HOME / "secrets" / "env.txt").read_text().splitlines()
    lines = [l for l in lines if l.split("=", 1)[0] not in extra] + [f"{k}={v}" for k, v in extra.items()]
    (HOME / ".env").write_text("\n".join(lines) + "\n")


def install(stage):
    pip = [sys.executable, "-m", "pip", "install", "-q"]
    # the Kaggle image has an older kaggle tool without "benchmarks ... --wait", so the director fell back to Gemini;
    # this is the version the Mac runs
    sh(pip + ["-r", str(HOME / "requirements.txt"), "kaggle==2.2.5", "soundfile", "yt-dlp"])
    if stage == "research":
        sh([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"])
    if stage == "gpu":
        voice_model(pip)
    if stage == "check":
        sh(pip + ["transformers>=5.10", "accelerate"])
    if stage == "part":
        node = HOME / "fx" / ".node"
        if not (node / "bin" / "node").exists():
            sh(f"mkdir -p {node} && curl -sL {NODE} | tar xJ --strip-components=1 -C {node}")
        # Chrome and every library it needs: the cards (titles, stats, labels) and the HyperFrames effects use it.
        # Missing, they were skipped one by one and the film came out with a tenth of its cards.
        sh([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"])
        sh([sys.executable, "fx.py", "setup"], cwd=HOME)
        # a card is drawn here once, so a broken browser stops the run now instead of making a film without cards
        sh([sys.executable, "-c", "import os, fx, hf; os.environ['STRICT'] = '1'; fx.ready(); p = hf.page(); "
            "p.set_content('<h1>ok</h1>'); p.close(); print('effects and cards ok')"], cwd=HOME)


def voice_model(pip):
    """The voice model on CUDA. chatterbox-tts pins torch 2.6, which broke Kaggle's own torchvision ("operator
    torchvision::nms does not exist") and with it transformers. So the same package the Mac runs goes in without its
    pins (its PyTorch path needs no MLX and works with today's transformers) and Kaggle's torch stays. If that torch is
    too new for it, the set the Mac runs (torch 2.8, torchaudio 2.8, torchvision 0.23) goes in as a matched trio.
    The import is tried here, so a broken voice stops the run in minutes, before any planning."""
    # the package says Python below 3.13 because of MLX; its PyTorch code is plain Python and Kaggle runs 3.13
    sh(pip + ["--no-deps", "--ignore-requires-python", "chatterbox-mlx==1.3.1", "s3tokenizer==0.3.0", "resemble-perth==1.0.1", "conformer==0.3.2"])
    sh(pip + ["onnx", "einops", "pyloudnorm", "librosa==0.11.0", "psutil", "tqdm", "transformers>=5.10",
              "diffusers==0.35.2"])
    probe = [sys.executable, "-c", "import torch, torchaudio, torchvision; from chatterbox.tts import ChatterboxTTS; "
             "print('voice model ok, torch', torch.__version__, 'cuda', torch.cuda.is_available())"]
    if sh(probe, check=False).returncode == 0:
        return
    torch = subprocess.run([sys.executable, "-c", "import torch; print(torch.__version__)"], capture_output=True,
                           text=True).stdout.strip()
    base, cuda = (torch.split("+") + [""])[:2]
    if cuda.startswith("cu"):  # first only torchaudio, built for the torch that is there
        sh(pip + ["--no-deps", f"torchaudio=={base}", "--index-url", f"https://download.pytorch.org/whl/{cuda}"],
           check=False)
        if sh(probe, check=False).returncode == 0:
            return
    sh(pip + ["torch==2.8.0", "torchaudio==2.8.0", "torchvision==0.23.0", "--index-url",
              "https://download.pytorch.org/whl/cu128"])
    sh(probe)


def folder():
    return HOME / "output" / RUN["slug"]


def checks(direction):
    """The picture check's verdicts (picture_check_cache.json) to or from Drive, a few kB: saved at every progress
    line and when a stage fails, so the next run looks only at the pictures this one had not reached."""
    import drive
    f, name = folder() / "picture_check_cache.json", f"job_{RUN['id']}_pictures.json"
    try:
        if direction == "get":
            drive.download(name, f)
        elif f.exists():
            drive.upload(f, name)
    except Exception as err:  # the verdicts are a saving, never a reason to stop
        print(f"  picture verdicts not {'loaded' if direction == 'get' else 'saved'} ({str(err)[:80]})", flush=True)


def state(direction):
    """The video folder to or from Drive between stages."""
    sys.path.insert(0, str(HOME / "cloud"))
    import drive
    tgz = W / f"job_{RUN['id']}.tgz"
    if direction == "get":
        if not drive.download(tgz.name, tgz):
            raise SystemExit(f"{tgz.name} is not on Drive")
        with tarfile.open(tgz) as t:
            t.extractall(HOME / "output", filter="fully_trusted")
    else:
        with tarfile.open(tgz, "w:gz") as t:
            t.add(folder(), arcname=RUN["slug"], filter=lambda i: None if "/chapters/" in i.name else i)
        drive.upload(tgz)
    tgz.unlink(missing_ok=True)


def work(stage, **extra):
    """documentary/doc.py for this stage. Its progress lines go to Telegram through the worker; when it fails, the
    error carries its last lines, so the message says what broke instead of only "exit status 1"."""
    env = {**os.environ, "STYLE": RUN.get("style", "documentary"), "DOC_STYLE": RUN.get("style", "documentary"), **extra}
    cmd = [sys.executable, "-u", "documentary/doc.py", RUN["title"], str(RUN["minutes"]), RUN.get("subject", "")]
    if stage == "check":  # cloud.py check: only the picture check, on a few pictures already made, nothing redrawn
        cmd = [sys.executable, "-u", "make.py", "check", str(folder())]
        env.update(DOC_SUBJECT=RUN.get("subject", ""), PICTURE_CHECK_LIMIT=str(RUN.get("limit", 10)), PICTURE_REDRAW="0")
    if stage == "research":
        env["RESEARCH_ONLY"] = "1"
    if stage == "gpu":
        env["STOP_AFTER"] = "pictures"
    if stage in ("part", "join"):
        env["RENDER_PART"] = str(RUN["part"]) if stage == "part" else "join"
    print("$", " ".join(cmd), flush=True)
    tail = deque(maxlen=40)
    with subprocess.Popen(cmd, cwd=HOME, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          bufsize=1) as p:
        for line in p.stdout:
            print(line, end="", flush=True)
            tail.append(line.rstrip())
            said_line = line.strip()
            if said_line.startswith(STEPS) and re.search(SAY, said_line):
                hook("progress", line=said_line)
            if said_line.startswith(("pictures: ", "vision: ")):
                checks("put")
    if p.returncode:
        said = [l for l in tail if l.strip() and not l.startswith(" " * 4)]
        busy = [l for l in said if l.startswith("service busy:")]
        if busy:
            raise Busy(busy[-1][:600])
        held = [l for l in said if l.startswith(("plan gate:", "SystemExit: plan gate:"))]
        if held:  # the same plan gives the same film: not run again, the reason goes to Telegram
            raise Quality(held[-1].removeprefix("SystemExit: ")[:600])
        raise RuntimeError(" | ".join(said[-3:])[-400:] or f"doc.py exit {p.returncode}")


# the lines of a build that say how far it is
SAY = r"took|done|drawn|fit their line|^screenshots:|^script:|^vision|^gemini:|before rendering"
STEPS = ("screenshots:", "script:", "voice ", "kaggle: ", "pictures: ", "vision models:", "gemini: ", "plan: ", "render ", "done:")

CHECKS = 14  # qa.py's checks; a film is offered for approval only when every one passes


def quality(f):
    """(summary, failed checks, what holds the film back) from qa.md: every failed check holds it, and so does a
    qa.md with fewer checks than expected."""
    qa = (f / "qa.md").read_text().splitlines() if (f / "qa.md").exists() else []
    rows = [[c.strip() for c in l.split("|")[1:4]] for l in qa if l.count("|") >= 4 and (" ok" in l or " FIX" in l)]
    fix = [f"{r[0]}: {r[1]} (target {r[2]})" for r in rows if r[1].endswith("FIX")]
    short = [f"only {len(rows)} of {CHECKS} checks ran"] if len(rows) < CHECKS else []
    return next((l for l in qa if "checks pass" in l), ""), fix, fix + short


def deliver():
    """The join: the film and its YouTube pack to Drive, then the worker tells Telegram with Yes and No. A film that
    fails the quality gate goes to Drive as _held and is reported instead, with what failed."""
    import drive
    f = folder()
    summary, fix, blocking = quality(f)
    if RUN.get("smoke"):  # a smoke film tests that every stage works; a one minute film is not judged as a video
        blocking = [b for b in blocking if b.startswith("only")]
    if not summary:
        raise RuntimeError("the quality check did not run (qa.md missing)")
    name = f"{RUN['slug']}{'_held' if blocking else ''}.mp4"
    _, link = drive.upload(f / "final.mp4", name)
    for extra in ("youtube_pack.json", "thumbnail.jpg", "subtitles.srt"):
        if (f / extra).exists():
            drive.upload(f / extra, f"{RUN['slug']}_{extra}")
    if blocking:
        raise Quality(f"the film was made but is held: {summary}. Failed: " + "; ".join(blocking) +
                      f". Look at it on Drive: {link}")
    pack = json.loads((f / "youtube_pack.json").read_text()) if (f / "youtube_pack.json").exists() else {}
    titles = pack.get("titles") or [{}]
    best = titles[pack.get("best", 0) if isinstance(pack.get("best"), int) else 0].get("title") or RUN["title"]
    check = f / "research" / "title_check.json"
    hook("ready", link=link, best=best, quality=summary, fix=fix,
         title_check=json.loads(check.read_text()) if check.exists() else None)


class Quality(Exception):
    """A film made but not good enough to offer: reported, not run again (the same input gives the same film)."""


class Busy(Exception):
    """An outside service (the vision API) down for ten minutes: nothing is wrong with the video, so the worker runs
    the stage again later instead of counting it as a failure. What was made is on Drive already."""


def hook(event, **extra):
    """Report to the worker, which decides everything else. Tried five times; the worker also notices a kernel that
    ended without a word. Without the worker set up (secrets/worker.env), Telegram is told directly."""
    import drive
    import requests
    w = drive.env("worker")
    body = {"event": event, "id": RUN["id"], "title": RUN["title"], "day": RUN.get("day"), "stage": RUN["stage"],
            "part": RUN.get("part"), "nonce": RUN.get("nonce"), **extra}
    if not w.get("WORKER_URL"):
        if event in ("failed", "ready"):
            drive.tell(f"{RUN['title']} ({RUN['stage']}): {event} {extra.get('error') or extra.get('link', '')}")
        return
    for wait in (0, 5, 15, 45, 120):
        time.sleep(wait)
        try:
            r = requests.post(f"{w['WORKER_URL']}/hook", json=body, headers={"X-Studio-Secret": w["HOOK_SECRET"]},
                              timeout=30)
            if r.ok:
                return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        except requests.RequestException:
            pass
    print(f"  the worker did not answer the {event} report", flush=True)
    if event in ("failed", "ready"):
        drive.tell(f"{RUN['title']} ({RUN['stage']}): {event}, and the worker did not answer. "
                   f"{extra.get('error') or extra.get('link', '')}"[:600])


def chapters():
    """How many chapters the film has: the script's chapter texts the edit plan was made from."""
    n = len(list((folder() / "parts").glob("ch*.txt")))
    if not n:
        raise RuntimeError("no chapter texts in parts/, the edit plan is missing")
    return n


def report(err):
    """A failure of the video in RUN to the worker, with its kind: quality (not run again), busy (later) or error."""
    sys.path.insert(0, str(HOME / "cloud"))
    if RUN["stage"] in ("gpu", "check") and (HOME / "cloud").exists():
        checks("put")
    if RUN["stage"] == "gpu" and folder().exists():  # the pictures drawn again so far go too: the next run starts there
        try:
            state("put")
        except Exception as lost:
            print(f"  the folder did not go to Drive ({str(lost)[:80]})", flush=True)
    try:
        kind = "quality" if isinstance(err, Quality) else "busy" if isinstance(err, Busy) else "error"
        wait = re.search(r"service busy: wait=(\d+)", str(err)) if kind == "busy" else None
        hook("failed", error=str(err)[:600], kind=kind, **({"wait": int(wait.group(1))} if wait else {}))
    except Exception as lost:  # the failure came before the code was unpacked: the worker's watch notices it
        print(f"  could not report the failure ({lost})", flush=True)


def video(stage):
    """This stage for the video in RUN."""
    if stage != "research":
        state("get")
    if stage in ("gpu", "check"):
        checks("get")
    if stage == "join":
        d = folder() / "chapters"
        d.mkdir(exist_ok=True)
        import drive
        for k in range(RUN["chapters"]):
            for ext in ("mp4", "json"):
                if not drive.download(f"job_{RUN['id']}_ch{k}.{ext}", d / f"{k:02d}_cloud.{ext}"):
                    raise RuntimeError(f"chapter {k + 1} ({ext}) is not on Drive")
    if stage == "gpu":  # voice and pictures go to Drive before the picture check: a failed check never costs them again
        work(stage, PICTURE_CHECK="0")
        state("put")
        hook("progress", line="voice and pictures saved, checking each picture against its line")
    if stage == "check" and (folder() / "picture_check.json").exists():  # to set the new verdicts beside
        print("earlier misses:", [m["file"] for m in json.loads((folder() / "picture_check.json").read_text())["misses"]])
    work(stage)
    if stage == "check":
        checks("put")
        print((folder() / "picture_check.json").read_text(), flush=True)
    elif stage in ("research", "gpu"):
        state("put")
        hook("done", **({"chapters": chapters()} if stage == "gpu" else {}))
    elif stage == "part":
        import drive
        k = RUN["part"]
        clip = max((folder() / "chapters").glob(f"{k:02d}_*.mp4"), key=os.path.getmtime)
        drive.upload(folder() / "chapters" / f"{k:02d}_plan.json", f"job_{RUN['id']}_ch{k}.json")
        drive.upload(clip, f"job_{RUN['id']}_ch{k}.mp4")
        hook("done")
    else:
        deliver()


def main():
    """One machine, one install, then the stage for each video it was given: the GPU stage takes the whole day's
    videos in one session (RUN["jobs"]), so Kaggle's GPU queue is waited out once a day, not once a video. Each video
    reports on its own: its chapters start rendering while the next one's pictures are drawn, and one that fails
    does not stop the others."""
    stage, t0 = RUN["stage"], time.time()
    jobs = RUN.pop("jobs", None) or [dict(RUN)]
    RUN.update(jobs[0])
    try:
        unpack()
        sys.path.insert(0, str(HOME / "cloud"))
        if (hook("stage") or {}).get("ignored"):  # replaced while it waited in Kaggle's queue: another run does this
            return print("the worker is not waiting for this run any more; stopping", flush=True)
        install(stage)
    except BaseException as err:
        for job in jobs:
            RUN.update(job)
            report(err)
        raise
    for n, job in enumerate(jobs):
        RUN.update(job)
        t1 = time.time()
        try:
            if n and (hook("stage") or {}).get("ignored"):
                print(f"{RUN['title']}: the worker is not waiting for it any more", flush=True)
                continue
            video(stage)
            print(f"{stage} of {RUN['title']} done in {(time.time() - t1) / 60:.0f} min", flush=True)
        except Exception as err:
            print(f"{stage} of {RUN['title']} failed: {str(err)[:300]}", flush=True)
            report(err)
    print(f"{stage} done in {(time.time() - t0) / 60:.0f} min", flush=True)

main()
