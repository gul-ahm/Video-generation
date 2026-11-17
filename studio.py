"""History Studio control room.

python studio.py   opens a dashboard in the browser. Give it a title (Gemini writes the script) or upload your own
script (format: samples/TEMPLATE.md); videos are made one after another in the background, each in its own process,
with every step, live progress, time left and the latest picture on screen.
The queue lives in output/queue.json, so closing the window or restarting the Mac loses nothing.
While one video is being built, the script of the next one is written at the same time (PREWRITE=0 turns that off).
"""
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.chdir(Path(__file__).parent)
import clean  # noqa: E402

OUT = Path("output")
QUEUE, STATS, LOGS = OUT / "queue.json", OUT / "studio_stats.json", OUT / "logs"
PORT = int(os.getenv("STUDIO_PORT", "8765"))
STAGES = ("script", "voice", "visuals", "render")
STYLES = [
    {"id": "storybook", "name": "Storybook (cartoon)", "about": "Illustrated picture book scenes that move: characters breathe and act, birds and dust drift."},
    {"id": "sketch", "name": "Pencil sketch", "about": "Loose grey pencil drawings, like animation concept art."},
    {"id": "documentary", "name": "Documentary (real look)", "about": "Realistic photos and deep research: over 100 web sources are read, every claim is checked, and screenshots of the articles appear in the film. Give only the title."},
]
DOC = "documentary"  # the style whose title jobs go through documentary/doc.py (research first, then the film)
# minutes of work per minute of video, measured on the M2 with the pictures on Kaggle (Tigris crossing), replaced by
# real averages later. The edit plan and the Kaggle pictures run next to the voice, so they have no share of their own.
GUESS = {"script": 0.2, "voice": 2.8, "visuals": 0.8, "render": 2.3}  # render includes the fx clips
SPOKEN = 188  # words per minute of the chatterbox voice
sys.path.insert(0, str(Path(__file__).parent))

lock = threading.Lock()
jobs = json.loads(QUEUE.read_text()) if QUEUE.exists() else []
for j in jobs:
    if j["status"] == "running":
        j["status"] = "queued"
    if j.get("script") == "writing":
        del j["script"]
live, proc, writer_proc, paused = {}, None, None, False


def folder_for(topic):
    """Same folder name as make.py uses."""
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:60]
    return OUT / (slug or hashlib.md5(topic.encode()).hexdigest()[:10])


def env_for(job):
    return {**os.environ, "STYLE": job["style"], "POSES": "1" if job.get("poses") else "0",
            "PYTHONUNBUFFERED": "1", "PYTHONWARNINGS": "ignore"}


def save():
    OUT.mkdir(exist_ok=True)
    QUEUE.write_text(json.dumps(jobs, indent=1))


def stats():
    return {**GUESS, **(json.loads(STATS.read_text()) if STATS.exists() else {})}


def learn(spent, minutes):
    """Keep a running average of how long each stage takes per minute of video on this machine."""
    old = json.loads(STATS.read_text()) if STATS.exists() else {}
    n = old.get("videos", 0)
    for s in STAGES:
        if spent.get(s):
            old[s] = (old.get(s, GUESS[s]) * n + spent[s] / 60 / minutes) / (n + 1)
    old["videos"] = n + 1
    STATS.write_text(json.dumps(old, indent=1))


def memory():
    try:
        if sys.platform == "darwin":
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
            page = int(re.search(r"page size of (\d+)", vm).group(1))
            pages = lambda k: int(re.search(rf"{k}:\s+(\d+)", vm).group(1))
            free = (pages("Pages free") + pages("Pages inactive") + pages("Pages speculative")) * page
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout)
            swap = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
            used = float(re.search(r"used = ([\d.]+)M", swap).group(1)) / 1024
        else:
            info = dict(re.findall(r"(\w+):\s+(\d+)", Path("/proc/meminfo").read_text()))
            free, total = int(info["MemAvailable"]) * 1024, int(info["MemTotal"]) * 1024
            used = (int(info["SwapTotal"]) - int(info["SwapFree"])) / 1024 ** 2
        return {"free": round(free / 1024 ** 3, 1), "total": round(total / 1024 ** 3), "swap": round(used, 1)}
    except Exception:
        return {}


def words(folder):
    """Spoken words per chapter of the script (headings, picture and setting lines are not read out)."""
    f = Path(folder) / "script.md"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            out.append(0)
        elif out and line.strip() and not line.startswith(("#", ">", "card:", "visuals:", "thumb:")):
            out[-1] += len(line.split())
    return out


def spoken(i, n):
    """Share of the narration in chapters before chapter i (1 based), by words, so a long chapter counts more."""
    w = live.get("words") or words(live.get("folder", ""))
    live["words"] = w
    return sum(w[:i - 1]) / sum(w) if len(w) == n and sum(w) else (i - 1) / n


def track(line):
    """Turn make.py's printed lines into progress numbers for the dashboard."""
    now = time.time()

    def stage(name, done):
        if live.get("stage") != name:
            if live.get("stage"):
                live["spent"][live["stage"]] = now - live["since"]
                live["done"][live["stage"]] = 1
            live.update(stage=name, since=now)
        live["done"][name] = done

    if m := re.search(r"step (\d): .*?(\d+) of (\d+)", line):
        step, i, n = map(int, m.groups())
        stage("script", ((i - 1) * 2 + (step == 4)) / (2 * n))
        live["detail"] = f"writing chapter {i} of {n}" if step == 3 else f"picture list for chapter {i} of {n}"
    elif re.search(r"step [12]:", line):
        stage("script", 0)
        live["detail"] = line.split(": ", 1)[1].strip()
    elif m := re.search(r"(searching(?: what is missing)?): (.+)", line):
        stage("script", min(0.4, live["done"].get("script", 0) + 0.01))
        live["detail"] = f"research, {m.group(1)}: {m.group(2)[:70]}"
    elif m := re.search(r"(\d+) sources from (\d+) sites read", line):
        stage("script", 0.45)
        live["detail"] = f"{m.group(1)} sources read from {m.group(2)} sites, checking every claim"
    elif m := re.search(r"(\d+) checked claims", line):
        stage("script", 0.55)
        live["detail"] = f"{m.group(1)} claims checked against their sources, building the ledger"
    elif m := re.search(r"screenshots: (\d+) of (\d+)", line):
        stage("script", 0.65)
        live["detail"] = f"screenshots of {m.group(1)} of {m.group(2)} source articles taken"
    elif m := re.search(r"writing chapter (\d+) of (\d+)", line):
        i, n = map(int, m.groups())
        stage("script", 0.65 + 0.35 * (i - 1) / n)
        live["detail"] = f"writing chapter {i} of {n} from the checked claims"
    elif m := re.search(r"script ready: (.+)/script\.md", line):
        live["folder"] = m.group(1)
        live.pop("words", None)
    elif m := re.search(r"picture list for chapter (\d+) of (\d+) \(your script", line):
        live["detail"] = f"Gemini writes the picture list, chapter {m.group(1)} of {m.group(2)}"
    elif m := re.search(r"director: (\S+) (is planning|answered .+)", line):
        live["plan_detail"] = f"{m.group(1)} on Kaggle {m.group(2).replace('is planning', 'is planning the whole edit')}"
    elif m := re.search(r"(edit|beats) plan for chapter (\d+) of (\d+)", line):
        i, n = int(m.group(2)), int(m.group(3))
        live["plan"] = [i - (m.group(1) == "edit"), n]  # edit then beats for each chapter
        live["plan_detail"] = f"Gemini plans chapter {i} of {n} ({'cards' if m.group(1) == 'edit' else 'mood and sound'})"
    elif "plan failed" in line:
        live["plan_note"] = "Gemini was busy for one plan, the backup labels are used there"
    elif "all models busy" in line:
        live["plan_detail"] = "Gemini is busy, waiting and trying again"
    elif m := re.search(r"card (\S+) skipped", line):
        live["detail"] = f"a card was replaced by a caption ({m.group(1)})"
    elif "word times:" in line:
        live["detail"] = "exact word times " + line.split("word times:")[1].strip()
    elif m := re.match(r"\s*voice (\d+)/(\d+) took", line):
        i, n = map(int, m.groups())
        stage("voice", spoken(i + 1, n))
    elif m := re.match(r"\s*voice (\d+)/(\d+)", line):
        i, n = map(int, m.groups())
        stage("voice", spoken(i, n))
        live["detail"] = f"voice for chapter {i} of {n}"
    elif m := re.search(r"kaggle: (\d+) pictures go", line):
        live["kaggle"] = f"{m.group(1)} pictures sent to the Kaggle GPU"
    elif m := re.search(r"kaggle: (running|queued), (\d+) min", line):
        live["kaggle"] = f"Kaggle GPU {m.group(1)}, {m.group(2)} min"
    elif m := re.search(r"image (\d+)/(\d+) from kaggle", line):
        live["kaggle"] = f"{m.group(1)} of {m.group(2)} pictures back from Kaggle"
    elif "kaggle: finished" in line or "kaggle: skipped" in line:
        live["kaggle"] = line.split("kaggle: ", 1)[1][:80]
    elif m := re.match(r"\s*visuals (\d+)/(\d+)", line):
        i, n = map(int, m.groups())
        stage("visuals", (i - 1) / n)
        live.update(chapter=(i, n), detail=f"pictures for chapter {i} of {n}")
    elif m := re.search(r"image (\d+)/(\d+) in (\d+)s", line):
        k, total, secs = map(int, m.groups())
        i, n = live.get("chapter", (1, 1))
        stage("visuals", (i - 1 + k / total) / n)
        live["images"].append(secs)
        live["detail"] = f"chapter {i} of {n}, picture {k} of {total}"
    elif m := re.search(r"pose (\d+)/(\d+) in (\d+)s", line):
        k, total, secs = map(int, m.groups())
        i, n = live.get("chapter", (1, 1))
        live["detail"] = f"chapter {i} of {n}, movement frame {k} of {total}"
    elif m := re.search(r"footage '(.+)': (\d+)", line):
        live["detail"] = f"downloading clips: {m.group(1)}"
    elif m := re.match(r"\s*render (\d+)/(\d+)", line):
        i, n = map(int, m.groups())  # chapters render while the next chapter's pictures are drawn
        live["rendered"] = i
        if live.get("stage") == "render" or i == n:
            stage("render", i / n)
    elif m := re.search(r"quality: (\d+ of \d+) checks pass", line):
        live["quality"] = m.group(1)
        live["detail"] = "quality check done, writing the YouTube text"
    elif m := re.match(r"done: (.+) \(([\d.]+) min video", line):
        stage("render", 1)
        live["spent"]["render"] = now - live["since"]
        quality = f", quality {live['quality']} checks pass (qa.md)" if live.get("quality") else ""
        live.update(final=m.group(1), video_minutes=float(m.group(2)), stage="done", detail=f"finished{quality}")


def steps():
    """Every step of the current video with its state, for the checklist on the dashboard."""
    if not live:
        return []
    cur = live.get("stage")
    def state(name):
        if cur == "done" or live["done"].get(name) == 1 and cur != name:
            return "done"
        return "now" if cur == name else "wait"
    pics = len(live.get("images", []))
    i, n = live.get("chapter", (0, 0))
    plan = live.get("plan")
    rows = [
        {"name": "Script", "state": state("script"), "text": "ready" if state("script") == "done" else live.get("detail", "")},
        {"name": "Voice", "state": state("voice"), "text": f"{round(live['done'].get('voice', 0) * 100)}%"},
        {"name": "Edit plan (Gemini)", "state": "done" if plan and plan[0] >= plan[1] or cur in ("visuals", "render", "done")
         else "now" if plan else "wait", "text": live.get("plan_note") or live.get("plan_detail", "runs next to the voice")},
        {"name": "Pictures on Kaggle", "state": "done" if cur in ("visuals", "render", "done") else "now" if live.get("kaggle")
         else "wait", "text": live.get("kaggle", "drawn on Kaggle while the voice is made")},
        {"name": "Pictures and cards", "state": state("visuals"), "text": f"chapter {i} of {n}, {pics} pictures drawn" if n else ""},
        {"name": "Sound and render", "state": state("render") if cur in ("render", "done") else "now" if live.get("rendered") else "wait",
         "text": f"{live.get('rendered', 0)} chapters done"},
        {"name": "Quality check", "state": "done" if live.get("quality") else "wait", "text": live.get("quality", "") and f"{live['quality']} checks pass"},
    ]
    return rows


def progress():
    """Share done and minutes left. The length comes from the script's own words (not the slider), each stage from
    its measured speed on this Mac, and a running stage from its real speed so far. Kaggle draws the pictures while
    the voice is made, and each chapter renders while the next one is laid out, so those overlap."""
    if not live:
        return {}
    per = stats()
    w = sum(live.get("words") or words(live.get("folder", ""))) if live.get("folder") else 0
    minutes = live.get("video_minutes") or (w / SPOKEN if w else live["minutes"])
    expect, left = {}, {}
    for s in STAGES:
        frac = live["done"].get(s, 0)
        expect[s] = per[s] * minutes * 60
        if s in live["spent"]:
            expect[s] = live["spent"][s]
        elif s == live.get("stage") and frac > 0.05:
            expect[s] = (time.time() - live["since"]) / frac
        left[s] = expect[s] * (1 - frac)
    total_left = left["script"] + left["voice"] + max(left["visuals"], left["render"])
    total = expect["script"] + expect["voice"] + max(expect["visuals"], expect["render"])
    return {"share": max(0.0, 1 - total_left / total) if total else 0, "left_min": round(total_left / 60),
            "video_min": round(minutes, 1)}


def run(job):
    global proc
    LOGS.mkdir(parents=True, exist_ok=True)
    if job.get("kind") == "script":
        cmd = [sys.executable, "-u", "make.py", "build", str(folder_for(job["topic"]))]
    elif job["style"] == DOC:
        cmd = [sys.executable, "-u", "documentary/doc.py", job["topic"], str(job["minutes"])]
    else:
        cmd = [sys.executable, "-u", "make.py", "auto", job["topic"], "--minutes", str(job["minutes"])]
    with open(LOGS / f"{job['id']}.log", "ab") as log:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=env_for(job), start_new_session=True)
        if sys.platform == "darwin":
            subprocess.Popen(["caffeinate", "-dims", "-w", str(proc.pid)])
        for raw in proc.stdout:
            log.write(raw)
            log.flush()
            line = raw.decode("utf-8", "replace").rstrip().split("\r")[-1]
            if not line or re.match(r"\s*\d+%\|", line):
                continue
            with lock:
                live["log"] = (live["log"] + [line])[-300:]
                track(line)
        return proc.wait()


def worker():
    while True:
        with lock:
            job = None if paused or proc and proc.poll() is None else next((j for j in jobs if j["status"] == "queued"), None)
            if job and job.get("script") == "writing":
                job = None
            if job:
                job.update(status="running", started=time.time(), error="")
                live.clear()
                live.update(id=job["id"], minutes=job["minutes"], started=time.time(), done={}, spent={},
                            images=[], log=[], detail="starting")
                live["folder"] = str(folder_for(job["topic"]))
                if (folder_for(job["topic"]) / "script.md").exists():
                    live["done"]["script"], live["spent"]["script"] = 1, 0
                save()
        if not job:
            time.sleep(2)
            continue
        for attempt in range(3):
            code = run(job)
            if code == 0 or job["status"] != "running":
                break
            with lock:
                live["detail"] = f"crashed, trying again in 60 seconds (attempt {attempt + 2} of 3)"
            time.sleep(60)
        with lock:
            if job["status"] == "running":
                ok = code == 0 and live.get("final")
                tail = [l for l in live["log"] if l.strip()][-3:]
                job.update(status="done" if ok else "failed", finished=time.time(), final=live.get("final", ""),
                           error="" if ok else " | ".join(tail)[-400:])
                if ok:
                    learn(live["spent"], live.get("video_minutes") or job["minutes"])
                    job["junk"] = clean.junk_gb(Path(job["final"]).parent)
                    notify(f"{job['topic']} is ready. Check it, then press Clean up in the dashboard.")
            save()


def prewriter():
    """Writing a script only waits on Gemini, so it can run next to the build of the current video at no cost.
    The next video then starts straight at the voice."""
    global writer_proc
    while True:
        with lock:
            building = proc is not None and proc.poll() is None  # with nothing building, the worker writes it in view
            job = None if paused or not building or os.getenv("PREWRITE", "1") != "1" else next(
                (j for j in jobs if j["status"] == "queued" and not j.get("script")), None)
            if job and (folder_for(job["topic"]) / "script.md").exists():
                job["script"], job = "ready", None
                save()
            elif job:
                job["script"] = "writing"
                save()
        if not job:
            time.sleep(5)
            continue
        LOGS.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-u", "make.py", "script", job["topic"], "--minutes", str(job["minutes"])]
        env = env_for(job)
        if job["style"] == DOC:  # the research and its script, so the film starts straight at the voice
            cmd, env["RESEARCH_ONLY"] = [sys.executable, "-u", "documentary/doc.py", job["topic"], str(job["minutes"])], "1"
        with open(LOGS / f"{job['id']}.log", "ab") as log:
            writer_proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                           env=env, start_new_session=True)
            code = writer_proc.wait()
        with lock:
            job["script"] = "ready" if code == 0 and (folder_for(job["topic"]) / "script.md").exists() else "failed"
            save()
        if code:
            time.sleep(60)


def make_pack(job):
    """The YouTube pack of a finished video, made again on request (meta.py), next to whatever else runs."""
    folder = Path(job["final"]).parent
    with open(LOGS / f"{job['id']}.log", "ab") as log:
        code = subprocess.run([sys.executable, "-u", "meta.py", str(folder)], stdin=subprocess.DEVNULL, stdout=log,
                              stderr=subprocess.STDOUT, env=env_for(job)).returncode
    with lock:
        job["pack"] = "ready" if code == 0 and (folder / "youtube_pack.json").exists() else "failed"
        save()


def notify(text):
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f"display notification {json.dumps(text)} with title \"History Studio\" sound name \"Glass\""])


def state():
    with lock:
        for j in jobs:
            if j.get("final") and j.get("pack") != "making" and (Path(j["final"]).parent / "youtube_pack.json").exists():
                j["pack"] = "ready"
        view = {k: v for k, v in live.items() if k != "images"}
        if live.get("images"):
            shots = live["images"]
            view["images"] = {"count": len(shots), "avg": round(sum(shots) / len(shots)), "slow": sum(s > 60 for s in shots)}
        return {"jobs": jobs, "live": {**view, **progress(), "steps": steps()} if live else {}, "paused": paused,
                "memory": memory(),
                "styles": STYLES, "per_minute": round(sum(stats()[s] for s in STAGES), 2), "now": time.time()}


def latest_image():
    folder = live.get("folder")
    if not folder:
        return None
    pics = [p for p in Path(folder, "img").rglob("s*.jpg") if p.name.count(".") == 1]
    return max(pics, key=lambda p: p.stat().st_mtime, default=None)


def stop(p=None):
    p = p or proc
    if p and p.poll() is None:
        os.killpg(p.pid, signal.SIGTERM) if hasattr(os, "killpg") else p.terminate()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, body, kind="application/json", code=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.reply(Path("dashboard.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self.reply(state())
        elif path == "/api/template":
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="script_template.md"')
            self.end_headers()
            self.wfile.write(Path("samples/TEMPLATE.md").read_bytes())
        elif path == "/api/image":
            pic = latest_image()
            self.reply(pic.read_bytes(), "image/jpeg") if pic else self.reply({}, code=404)
        elif path == "/api/pack":
            job = next((j for j in jobs if j["id"] == parse_qs(urlparse(self.path).query).get("id", [""])[0]), None)
            f = Path(job["final"]).parent / "youtube_pack.json" if job and job.get("final") else None
            self.reply(json.loads(f.read_text(encoding="utf-8")) if f and f.exists() else {}, code=200 if f and f.exists() else 404)
        elif path == "/api/log":
            job = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            log = LOGS / f"{re.sub(r'[^a-f0-9]', '', job)}.log"
            self.reply(log.read_bytes() if log.exists() else b"", "text/plain; charset=utf-8")
        else:
            self.reply({}, code=404)

    def do_POST(self):
        global paused
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        action = urlparse(self.path).path.rsplit("/", 1)[-1]
        with lock:
            if action == "script":
                import writer
                text = str(body.get("script", "")).replace("\r\n", "\n")
                problems = writer.check_script(text)
                if problems:
                    return self.reply({"problems": problems}, code=400)
                title = re.search(r"^# (.+)$", text, re.M).group(1).strip()
                folder = folder_for(title)
                folder.mkdir(parents=True, exist_ok=True)
                old = folder / "script.md"
                if old.exists() and old.read_text(encoding="utf-8") != text:
                    old.rename(folder / f"script_{int(time.time())}.md")
                old.write_text(text, encoding="utf-8")
                words = len(re.sub(r"^(#|>|card:|visuals:|thumb:).*$", "", text, flags=re.M).split())
                style = body.get("style") if body.get("style") in {s["id"] for s in STYLES} else STYLES[0]["id"]
                jobs.append({"id": uuid.uuid4().hex[:10], "topic": title, "kind": "script", "style": style,
                             "minutes": max(1, round(words / SPOKEN)), "script": "ready", "status": "queued",
                             "added": time.time()})
            elif action == "add":
                style = body.get("style") if body.get("style") in {s["id"] for s in STYLES} else STYLES[0]["id"]
                minutes = max(5, min(120, int(body.get("minutes", 60))))
                for topic in [t.strip() for t in body.get("topics", "").splitlines() if t.strip()]:
                    jobs.append({"id": uuid.uuid4().hex[:10], "topic": topic, "style": style, "minutes": minutes,
                                 "poses": bool(body.get("poses")), "status": "queued", "added": time.time()})
            elif action in ("pause", "resume"):
                paused = action == "pause"
            else:
                job = next((j for j in jobs if j["id"] == body.get("id")), None)
                if job and action == "remove" and job["status"] != "running":
                    if job.get("script") == "writing":
                        stop(writer_proc)
                    jobs.remove(job)
                elif job and action == "retry" and job["status"] in ("failed", "stopped", "done"):
                    job.update(status="queued", error="")
                    job.pop("script", None)
                elif job and action == "up" and job["status"] == "queued":
                    jobs.remove(job)
                    first = next((i for i, j in enumerate(jobs) if j["status"] == "queued"), len(jobs))
                    jobs.insert(first, job)
                elif job and action == "pack" and job["status"] == "done" and job.get("final") and job.get("pack") != "making":
                    job["pack"] = "making"
                    threading.Thread(target=make_pack, args=(job,), daemon=True).start()
                elif job and action == "clean" and job["status"] == "done" and job.get("final"):
                    job["cleaned"], job["junk"] = clean.clean(Path(job["final"]).parent), 0
                elif job and action == "stop" and job["status"] == "running" and proc:
                    job["status"] = "stopped"
                    stop()
            save()
        self.reply(state())


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=prewriter, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"History Studio dashboard: {url}  (keep this window open, Ctrl+C to quit)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop()
        stop(writer_proc)
