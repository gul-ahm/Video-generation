"""One stage of a video on Kaggle. The same file runs all three stages; push.py writes the stage and the job into RUN.

  research  (CPU)       web research, claims, article screenshots, script
  gpu       (2 x T4)    edit plan, your voice, the pictures
  render    (CPU)       effects, sound, render, quality check, YouTube pack; the video goes to Drive and Telegram

Each stage takes the video folder from Drive (job_<id>.tgz), does its part, puts it back and pushes the next stage,
so no computer of yours has to be on. Only the GPU stage uses GPU hours. A failure is sent to Telegram.
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
NEXT = {"research": "gpu", "gpu": "render", "render": None}
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
    if stage == "render":
        node = HOME / "fx" / ".node"
        if not (node / "bin" / "node").exists():
            sh(f"mkdir -p {node} && curl -sL {NODE} | tar xJ --strip-components=1 -C {node}")
        # every library a headless Chrome needs; with only a few of them the HyperFrames effects (marker, typewriter,
        # number, slam) were all skipped
        sh([sys.executable, "-m", "playwright", "install-deps", "chromium"], check=False)
        if sh([sys.executable, "fx.py", "setup"], cwd=HOME, check=False).returncode:
            with open(HOME / ".env", "a") as f:  # the film still renders, without the Remotion and HyperFrames effects
                f.write("FX=0\n")


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
    hook("ready", link=link)
    drive.tell(f"Video ready: {best}\n\nWatch: {link}{warn}\n\nCheck the screenshots and facts, then approve.",
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


def push(stage):
    sys.path.insert(0, str(HOME / "cloud"))
    import push as p
    p.push(stage, RUN)


def main():
    stage, t0 = RUN["stage"], time.time()
    try:
        unpack()
        hook("stage")
        install(stage)
        if stage != "research":
            state("get")
        work(stage)
        if stage == "render":
            deliver()
            import drive
            drive.delete(f"job_{RUN['id']}.tgz")
        else:
            state("put")
            push(NEXT[stage])
        hook("done", minutes=round((time.time() - t0) / 60))
        print(f"{stage} done in {(time.time() - t0) / 60:.0f} min", flush=True)
    except BaseException as err:
        sys.path.insert(0, str(HOME / "cloud"))
        try:
            import drive
            drive.tell(f"History Studio: the {stage} stage of \"{RUN['title']}\" failed: {str(err)[:300]}")
            hook("failed", error=str(err)[:300])
        finally:
            raise


main()
