"""One stage of a video on Kaggle. The same file runs every stage; push.py writes the stage and the job into RUN.

  research  (CPU)            web research, claims, article screenshots, script
  gpu       (2 x T4)         edit plan, your voice, the pictures
  part      (CPU, one each)  one chapter rendered with its effects and sound; all chapters render at the same time
  join      (CPU)            the chapters put together, quality check, YouTube pack; the video goes to Drive and Telegram
  render    (CPU)            all chapters one after another on one machine (the old way, over 4 hours)

Each stage takes the video folder from Drive (job_<id>.tgz), does its part and pushes the next stage, so no computer
of yours has to be on. Only the GPU stage uses GPU hours. A failure is sent to Telegram.
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
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
    kag = Path.home() / ".kaggle"
    kag.mkdir(exist_ok=True)
    shutil.copy(HOME / "secrets" / "kaggle.json", kag / "kaggle.json")
    (kag / "kaggle.json").chmod(0o600)
    # IMAGE_ENGINE: a picture's file name comes from the engine, which follows the device; without it the CPU render
    # looked for other names than the GPU stage had drawn and found none of the 114 pictures
    extra = {"CLOUD": "1", "KAGGLE_LOCAL": "1", "KAGGLE": "1", "MAC_PICTURES": "0", "CHATTER_BACKEND": "torch",
             "ASK_CLEAN": "0", "PREWRITE": "0", "IMAGE_ENGINE": "local"}
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
    if stage in ("part", "render"):
        node = HOME / "fx" / ".node"
        if not (node / "bin" / "node").exists():
            sh(f"mkdir -p {node} && curl -sL {NODE} | tar xJ --strip-components=1 -C {node}")
        # Chrome and every library it needs: the cards (titles, stats, labels) and the HyperFrames effects use it.
        # Missing, they were skipped one by one and the film came out with a tenth of its cards.
        sh([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"])
        sh([sys.executable, "fx.py", "setup"], cwd=HOME)
        # a card is drawn here once, so a broken browser stops the run now instead of making a film without cards
        sh([sys.executable, "-c", "import hf; p = hf.page(); p.set_content('<h1>ok</h1>'); p.close(); print('cards ok')"],
           cwd=HOME)


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


def work(stage):
    env = {**os.environ, "STYLE": RUN.get("style", "documentary"), "DOC_STYLE": RUN.get("style", "documentary")}
    cmd = [sys.executable, "-u", "documentary/doc.py", RUN["title"], str(RUN["minutes"]), RUN.get("subject", "")]
    if stage == "research":
        env["RESEARCH_ONLY"] = "1"
    if stage == "gpu":
        env["STOP_AFTER"] = "pictures"
    if stage in ("part", "join"):
        env["RENDER_PART"] = str(RUN["part"]) if stage == "part" else "join"
    print("$", " ".join(cmd), flush=True)
    with subprocess.Popen(cmd, cwd=HOME, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          bufsize=1) as p:
        for line in p.stdout:
            print(line, end="", flush=True)
            progress(stage, line.strip())
    if p.returncode:
        raise subprocess.CalledProcessError(p.returncode, cmd)


# the lines of a build that say how far it is, sent to Telegram as they come
STEPS = ("screenshots:", "script:", "voice ", "kaggle: ", "render ", "done:")


def progress(stage, line):
    if line.startswith(STEPS) and ("took" in line or "done" in line or "drawn" in line or line.startswith(("screenshots:", "script:"))):
        sys.path.insert(0, str(HOME / "cloud"))
        import drive
        drive.tell(f"{RUN['title'][:60]} ({stage}): {line[:200]}")


def deliver():
    """final.mp4 and its YouTube pack to Drive, and the link to Telegram with Yes and No."""
    sys.path.insert(0, str(HOME / "cloud"))
    import drive
    f = folder()
    _, link = drive.upload(f / "final.mp4", f"{RUN['slug']}.mp4")
    for extra in ("youtube_pack.json", "thumbnail.jpg", "subtitles.srt"):
        if (f / extra).exists():
            drive.upload(f / extra, f"{RUN['slug']}_{extra}")
    pack = json.loads((f / "youtube_pack.json").read_text()) if (f / "youtube_pack.json").exists() else {}
    titles = pack.get("titles") or [{}]
    best = titles[pack.get("best", 0) if isinstance(pack.get("best"), int) else 0].get("title") or RUN["title"]
    check = f / "research" / "title_check.json"
    check = json.loads(check.read_text()) if check.exists() else {}
    warn = ("" if check.get("ok", True) else
            f"\n\nTitle check: {'; '.join(check.get('problems', []))[:600]}\nSuggested title: {check.get('title')}")
    qa = (f / "qa.md").read_text().splitlines() if (f / "qa.md").exists() else []
    passed = next((l for l in qa if "checks pass" in l), "")
    fix = [l.split("|")[1].strip() + ": " + l.split("|")[2].strip() for l in qa if " FIX " in l]
    quality = f"\n\nQuality: {passed}" + "".join(f"\n- {x}" for x in fix) if passed else ""
    hook("ready", link=link)
    drive.tell(f"Video ready: {best}\n\nWatch: {link}{warn}{quality}\n\nCheck the screenshots and facts, then approve.",
               [("Yes, publish", f"yes:{RUN['id']}"), ("No", f"no:{RUN['id']}")])


def hook(event, **extra):
    """Tell the Cloudflare worker (Telegram buttons, daily start) how the video is doing; quiet when it is not set up."""
    sys.path.insert(0, str(HOME / "cloud"))
    import drive
    import requests
    w = drive.env("worker")
    if not w.get("WORKER_URL"):
        return
    body = {"event": event, "id": RUN["id"], "title": RUN["title"], "day": RUN.get("day"), "stage": RUN["stage"], **extra}
    try:
        requests.post(f"{w['WORKER_URL']}/hook", json=body, headers={"X-Studio-Secret": w["HOOK_SECRET"]}, timeout=30)
    except requests.RequestException:
        pass


def push(stage, **extra):
    sys.path.insert(0, str(HOME / "cloud"))
    import push as p
    p.push(stage, {**RUN, **extra})


def mark(name):
    """An empty file on Drive that says something happened (a chapter pushed, the join pushed)."""
    import drive
    f = W / name
    f.write_text("")
    drive.upload(f)
    f.unlink()


def parts(n):
    """Push every chapter that nobody pushed yet, each to its own CPU machine. Kaggle may refuse some when too many
    run at once; the chapters that finish push them later. Returns how many were pushed now."""
    import drive
    pushed = 0
    for k in range(n):
        if drive.find(f"job_{RUN['id']}_p{k}.pushed"):
            continue
        try:
            push("part", part=k, chapters=n)
            mark(f"job_{RUN['id']}_p{k}.pushed")
            pushed += 1
        except (Exception, SystemExit) as err:
            print(f"  chapter {k + 1} not pushed yet ({str(err)[:80]})", flush=True)
    return pushed


def chapters():
    """How many chapters the voice has (one words file per chapter)."""
    return len({p.name[:2] for p in (folder() / "audio").glob("*.words.json")})


def part_done():
    """This chapter's clip to Drive; the last chapter to finish pushes the join."""
    import drive
    k, n = RUN["part"], RUN["chapters"]
    clip = max((folder() / "chapters").glob(f"{k:02d}_*.mp4"), key=os.path.getmtime)
    drive.upload(clip, f"job_{RUN['id']}_ch{k}.mp4")
    parts(n)
    if all(drive.find(f"job_{RUN['id']}_ch{j}.mp4") for j in range(n)) and not drive.find(f"job_{RUN['id']}_join"):
        mark(f"job_{RUN['id']}_join")
        push("join")
        drive.tell(f"{RUN['title'][:60]}: all {n} chapters rendered, putting the film together")


def join_get():
    import drive
    d = folder() / "chapters"
    d.mkdir(exist_ok=True)
    for k in range(RUN["chapters"]):
        if not drive.download(f"job_{RUN['id']}_ch{k}.mp4", d / f"{k:02d}_cloud.mp4"):
            raise SystemExit(f"chapter {k + 1} is not on Drive")


def tidy():
    """The chapter clips and markers go once the film is on Drive; job_<id>.tgz stays for a fix or a re-render."""
    import drive
    for k in range(RUN["chapters"]):
        for name in (f"job_{RUN['id']}_ch{k}.mp4", f"job_{RUN['id']}_p{k}.pushed"):
            drive.delete(name)
    drive.delete(f"job_{RUN['id']}_join")


def main():
    stage, t0 = RUN["stage"], time.time()
    try:
        unpack()
        hook("stage")
        install(stage)
        sys.path.insert(0, str(HOME / "cloud"))
        if stage != "research":
            state("get")
        if stage == "join":
            join_get()
        work(stage)
        if stage == "research":
            state("put")
            push("gpu")
        elif stage == "gpu":
            state("put")
            n = chapters()
            if not parts(n):
                raise SystemExit("Kaggle took none of the chapter renders")
        elif stage == "part":
            part_done()
        else:  # join, or the old one machine render
            deliver()
            if stage == "join":
                tidy()
        hook("done", minutes=round((time.time() - t0) / 60))
        print(f"{stage} done in {(time.time() - t0) / 60:.0f} min", flush=True)
    except BaseException as err:
        sys.path.insert(0, str(HOME / "cloud"))
        try:
            import drive
            drive.tell(f"History Studio: the {stage} stage of \"{RUN['title']}\" failed: {str(err)[:300]}")
            hook("failed", error=str(err)[:300], run=RUN)
        finally:
            raise


main()
