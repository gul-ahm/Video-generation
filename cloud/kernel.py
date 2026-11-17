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
W = Path("/kaggle/working")
HOME = W / "studio"
NEXT = {"research": "gpu", "gpu": "render", "render": None}
NODE = "https://nodejs.org/dist/v22.12.0/node-v22.12.0-linux-x64.tar.xz"


def sh(cmd, **kw):
    print("$", cmd if isinstance(cmd, str) else " ".join(map(str, cmd)), flush=True)
    return subprocess.run(cmd, shell=isinstance(cmd, str), check=kw.pop("check", True), **kw)


def unpack():
    """Code, assets and secrets from the three private datasets; secrets also give Kaggle's own token."""
    for name in ("code", "assets", "secrets"):
        tgz = next(Path("/kaggle/input").rglob(f"{name}.tgz"), None)
        if tgz:
            with tarfile.open(tgz) as t:
                t.extractall(HOME)
        else:  # Kaggle unpacked the archive itself
            shutil.copytree(next(Path("/kaggle/input").glob(f"*{name}*")), HOME, dirs_exist_ok=True)
    kag = Path.home() / ".kaggle"
    kag.mkdir(exist_ok=True)
    shutil.copy(HOME / "secrets" / "kaggle.json", kag / "kaggle.json")
    (kag / "kaggle.json").chmod(0o600)
    extra = {"CLOUD": "1", "KAGGLE_LOCAL": "1", "KAGGLE": "1", "MAC_PICTURES": "0", "CHATTER_BACKEND": "torch",
             "ASK_CLEAN": "0", "PREWRITE": "0"}
    lines = (HOME / "secrets" / "env.txt").read_text().splitlines()
    lines = [l for l in lines if l.split("=", 1)[0] not in extra] + [f"{k}={v}" for k, v in extra.items()]
    (HOME / ".env").write_text("\n".join(lines) + "\n")


def install(stage):
    pip = [sys.executable, "-m", "pip", "install", "-q"]
    sh(pip + ["-r", str(HOME / "requirements.txt"), "kaggle", "soundfile", "yt-dlp"])
    if stage == "research":
        sh([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"])
    if stage == "gpu":
        sh(pip + ["chatterbox-tts"])  # the same voice model the Mac runs through MLX, here on CUDA
    if stage == "render":
        node = HOME / "fx" / ".node"
        if not (node / "bin" / "node").exists():
            sh(f"mkdir -p {node} && curl -sL {NODE} | tar xJ --strip-components=1 -C {node}")
        sh("apt-get install -y -q libnss3 libatk-bridge2.0-0 libgbm1 libxkbcommon0 libasound2 >/dev/null", check=False)
        if sh([sys.executable, "fx.py", "setup"], cwd=HOME, check=False).returncode:
            with open(HOME / ".env", "a") as f:  # the film still renders, without the Remotion and HyperFrames effects
                f.write("FX=0\n")


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
            t.extractall(HOME / "output")
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
    sh(cmd, cwd=HOME, env=env)


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
    drive.tell(f"Video ready: {best}\n\nWatch: {link}\n\nCheck the screenshots and facts, then approve.",
               [("Yes, publish", f"yes:{RUN['id']}"), ("No", f"no:{RUN['id']}")])


def push(stage):
    sys.path.insert(0, str(HOME / "cloud"))
    import push as p
    p.push(stage, RUN)


def main():
    stage, t0 = RUN["stage"], time.time()
    try:
        unpack()
        install(stage)
        if stage != "research":
            state("get")
        work(stage)
        if stage == "render":
            deliver()
        else:
            state("put")
            push(NEXT[stage])
        print(f"{stage} done in {(time.time() - t0) / 60:.0f} min", flush=True)
    except BaseException as err:
        sys.path.insert(0, str(HOME / "cloud"))
        try:
            import drive
            drive.tell(f"History Studio: the {stage} stage of \"{RUN['title']}\" failed: {str(err)[:300]}")
        finally:
            raise


main()
