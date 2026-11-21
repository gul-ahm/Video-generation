import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import accumulate
from pathlib import Path

from dotenv import load_dotenv

os.chdir(Path(__file__).parent)
load_dotenv() or load_dotenv(".env.example")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import clean
import footage
import fx
import hf
import kaggle_gpu
import meta
import motion
import qa
import render
import sound
import visuals
import voice
import writer

for key, value in visuals.STYLES.get(os.getenv("STYLE", ""), ("", "", {}))[2].items():
    os.environ[key] = value
OUT = Path("output")
# in the cloud nobody watches the log: a card, an effect or a picture that cannot be made stops the stage with its
# reason (the worker runs it once more, then reports), instead of a film quietly made without it
STRICT = os.getenv("STRICT") == "1"
PLAN_ONLY = lambda: os.getenv("PLAN_ONLY") == "1"  # plan_gate: shots and sound worked out, nothing drawn


def folder_for(topic):
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:60]
    return OUT / (slug or hashlib.md5(topic.encode()).hexdigest()[:10])


def script(topic, minutes):
    folder = folder_for(topic)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "topic.txt").write_text(topic, encoding="utf-8")
    if not (folder / "script.md").exists():
        print(f"script: {topic}")
        writer.write_script(topic, folder, minutes)
    print(f"script ready: {folder / 'script.md'}")
    return folder


def card_spec(e, folder, pics):
    """Write a full screen card spec next to the pictures. A document card first finds a public domain picture on
    Wikimedia Commons; without one it becomes a caption. Returns (path or caption, credit)."""
    e = dict(e)
    if e["kind"] == "document":
        words = str(e.get("query", "")).split()
        dst = folder / f"doc{hashlib.md5(str(e.get('query')).encode()).hexdigest()[:8]}.jpg"
        # the file name must carry most of the query's words, so "Joseph Moore" never brings back another Joseph Moore
        key = [w.lower() for w in words if len(w) > 3]
        fits = lambda r: sum(w in r[1].lower() for w in key) >= max(1, round(len(key) * 0.6))
        try:
            found = next((r for r in visuals.commons(" ".join(words), 8) if fits(r)), None)
            if found and not dst.exists():
                visuals.download(found[0], dst)
        except Exception:
            found = None
        if not dst.exists():
            return {**e, "kind": "caption"}, None
        e["image"] = str(dst)
    if e["kind"] == "card":  # a HyperFrames card: its frames are drawn now, on this thread (the browser is not shared)
        slots = {k: v for k, v in (e.get("slots") or {}).items() if k not in hf.image_slots(e["card"])}
        slots.update({k: str(pics[n % len(pics)]) for n, k in enumerate(hf.image_slots(e["card"]))} if pics else {})
        out = folder / "hf" / hashlib.md5(json.dumps([e["card"], slots], sort_keys=True).encode()).hexdigest()[:10]
        try:
            card_frames(e["card"], slots, out)
        except Exception as err:  # on the Mac the words still show as a caption; in the cloud the build stops
            print(f"  card {e['card']} skipped ({str(err)[:60]})")
            return {"kind": "caption", "title": writer.headline(e)}, None
        e = {"kind": "hf", "card": e["card"], "frames": str(out)}
    if e["kind"] == "list":
        e["images"] = [str(p) for p in pics[:len(e.get("items", []))]]
    path = folder / f"card_{hashlib.md5(json.dumps(e, sort_keys=True).encode()).hexdigest()[:8]}.json"
    path.write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")
    return path, found[1] if e["kind"] == "document" else None


CARDS = ("number", "list", "bars", "map", "document", "timeline", "compare", "quote", "stamp", "calendar", "card")
HF_SECONDS = 3.0  # a card's entrance is drawn; after it the last frame holds under a slow push
OVERLAYS = ("caption", "title", "headline", "hfo")
# sounds of a card: (seconds after the card starts, effect); the stamp lands at 0.9 s, the marker circles at 0.7 s
CARD_SOUND = {"number": [(0, "whoosh"), (0.1, "hit")], "map": [(0, "whoosh")], "list": [(0, "whoosh")],
              "bars": [(0, "whoosh"), (0.4, "tick")], "document": [(0, "paper")], "timeline": [(0, "whoosh")],
              "compare": [(0, "whoosh")], "quote": [(0, "paper")], "stamp": [(0, "paper"), (0.9, "hit")],
              "calendar": [(0, "whoosh"), (0.1, "paper"), (0.7, "tick")], "hf": [(0, "paper"), (0.35, "pop")]}


def cut_in(shots, t, card, hold):
    """A full screen card from exactly t to t + hold. The shots it covers are trimmed; a piece left shorter than
    1 s goes to its neighbour so there is no flash cut."""
    head, tail, pos = [], [], 0.0
    for s in shots:
        a, b = pos, pos + s[1]
        pos = b
        if b <= t + 1e-6:
            head.append(s)
        elif a >= t + hold - 1e-6:
            tail.append(s)
        else:
            if a < t:
                head.append([s[0], t - a, s[2], *s[3:]])
            if b > t + hold:
                extra = [{k: v for k, v in s[3].items() if k not in ("intro", "caption")}] if len(s) > 3 else []
                tail.append([s[0], b - t - hold, s[2] + t + hold - a, *extra])
    if head and head[-1][1] < 1.0:  # too short to read: the shot before takes it, or the card starts with the chapter
        short = head.pop()
        if head:
            head[-1][1] += short[1]
        else:
            hold, t = hold + short[1], t - short[1]
    if tail and tail[0][1] < 1.0:
        short = tail.pop(0)[1]
        if tail:
            tail[0][1] += short
        else:
            hold += short
    return head + [[card, hold, 0.0, {"card_t": t}]] + tail


_labels = {"n": 0}


def card_frames(card, slots, out):
    """A HyperFrames card's frames, tried twice: a busy machine can miss one browser start."""
    if PLAN_ONLY():
        return out
    try:
        return hf.render(card, slots, HF_SECONDS, out)
    except Exception:
        try:
            return hf.render(card, slots, HF_SECONDS, out)
        except Exception as err:
            if STRICT:
                raise SystemExit(f"card {card} could not be drawn: {str(err)[:200]}")
            raise


def tier2(e, folder):
    """Every second caption becomes a HyperFrames tier2 card, so the lower third is not the same box all video: a
    name gets a lower third (tag, name, role), anything else a label (term, detail). Variants go in turn."""
    _labels["n"] += 1
    if _labels["n"] % 2 or os.getenv("HF_LABELS", "1") != "1":
        return e
    title, sub = str(e.get("title", "")), str(e.get("sub", ""))
    is_name = re.fullmatch(r"(?:[A-Z][a-z]+\.? ){1,3}[A-Z][a-z]+", title.strip()) and not re.search(r"\d", title)
    pool = hf.LOWER if is_name else hf.LABELS
    if not pool:  # the kit is not in assets/hyperframes
        return e
    card = pool[(_labels["n"] // 2) % len(pool)]
    slots = {"tag": "", "name": title, "role": sub} if is_name else {"term": title, "detail": sub}
    out = folder / "hf" / hashlib.md5(json.dumps([card, slots]).encode()).hexdigest()[:10]
    try:
        card_frames(card, slots, out)
    except Exception as err:  # on the Mac the plain caption still works; in the cloud the build stops
        print(f"  card {card} skipped ({str(err)[:60]})")
        return e
    return {**e, "kind": "hfo", "card": card, "frames": str(out)}


MONTHS = r"January|February|March|April|May|June|July|August|September|October|November|December"
FX_SOUND = {"typewriter": [(0.25, "tick")], "number": [(0.25, "pop")], "marker": [(0.15, "pop"), (0.8, "paper")],
            "slam": [(0.2, "whoosh"), (0.45, "hit")]}
_kinetic = {"n": 0}


def kinetic(e, folder, want=None):
    """A caption as a HyperFrames kinetic caption (fx.py): a date types itself out, a number pops in digit by digit,
    a big line slams in, and every other plain caption gets a marker circle on its key word. Names stay lower thirds.
    want: the director's choice (typewriter, number, slam, marker or plain); it wins when the caption can carry it.
    Returns the caption with its frames, or None to keep the caption as it was."""
    if os.getenv("FX_CAPTIONS", "1") != "1":
        return None
    title, sub = str(e.get("title", "")).strip(), str(e.get("sub", "")).strip()
    if not title or len(title) > 48:
        return None
    num = re.fullmatch(r"(?:about |over |nearly )?([$£]?[\d][\d,.]*)\s*(%|[A-Za-z]+)?(?: .*)?", title, re.I)
    if want == "plain" and e.get("kind") != "title":
        return None
    if want == "slam" and e.get("kind") != "title":
        kind, values = "slam", {"text": title}
    elif want == "typewriter":
        kind, values = "typewriter", {"text": title.upper()}
    elif want == "number" and num:
        kind, values = "number", {"value": num.group(1), "unit": num.group(2) or "", "sub": sub}
    elif want == "marker" and e.get("kind") != "title":
        words = re.findall(r"[A-Za-z']+", title)
        said = str(e.get("word", "")).strip(".,;:!?")
        word = said if said.lower() in title.lower() and len(said) > 3 else max(words, key=len, default="")
        kind, values = "marker", {"text": title, "word": word}
    elif e.get("kind") == "title":
        kind, values = "slam", {"text": title}
    elif re.search(rf"\b1\d{{3}}\b|\b({MONTHS})\b", title):
        kind, values = "typewriter", {"text": title.upper()}
    elif num and not re.search(r"\b1\d{3}\b", title):
        kind, values = "number", {"value": num.group(1), "unit": num.group(2) or "", "sub": sub}
    elif re.fullmatch(r"(?:[A-Z][a-z]+\.? ){1,3}[A-Z][a-z]+", title):
        return None  # a name: the lower third says it better
    else:
        _kinetic["n"] += 1
        if _kinetic["n"] % 2:
            return None
        words = re.findall(r"[A-Za-z']+", title)
        said = str(e.get("word", "")).strip(".,;:!?")
        word = said if said.lower() in title.lower() and len(said) > 3 else max(words, key=len, default="")
        kind, values = "marker", {"text": title, "word": word}
    out = folder / "fx" / f"{kind}_{fx.key(kind, values)}"
    got = out if PLAN_ONLY() else fx.frames(kind, values, out)
    return {**e, "kind": "hfo", "fx": kind, "card": f"hyperframes {kind}", "frames": str(got), "step": 1} if got else None


def put_caption(shots, t, e):
    """A caption or title on the picture shot that is showing at t, appearing on the word. If the shot ends within
    1.8 s, the cut moves a little later so the caption can be read; "_t" keeps the word time for later."""
    pos = 0.0
    for j, s in enumerate(shots):
        if pos + s[1] > t:
            break
        pos += s[1]
    else:
        return
    if s[0].suffix == ".json":  # the word falls on a card: the caption waits for the picture after it
        pos += s[1]
        j, s = j + 1, shots[j + 1] if j + 1 < len(shots) else None
        if not s or pos - t > 2.5:
            return
    if len(s) < 4 or s[0].suffix == ".json" or "caption" in s[3]:
        return
    short = 1.8 - (pos + s[1] - max(t, pos))
    nxt = shots[j + 1] if j + 1 < len(shots) else None
    if short > 0 and nxt and nxt[0].suffix != ".json" and nxt[1] - short >= 1.2:
        s[1] += short
        nxt[1] -= short
    s[3]["caption"] = {**e, "_t": t}


def lit(ch, plan):
    """The picture prompts of a chapter, each with the light of its sentence's mood."""
    prompts = [p if os.getenv("AI_IMAGES", "1") == "1" else None for p, *_ in ch["shots"]]
    moods, word = sorted((plan or {}).get("moods", [])), 0
    for i, (_, n, *_) in enumerate(ch["shots"]):
        mood = next((m for w, m in reversed(moods) if w <= word), None)
        if prompts[i] and mood in visuals.MOOD_LIGHT and not prompts[i].lower().startswith("source:"):
            prompts[i] = f"{prompts[i]}, {visuals.MOOD_LIGHT[mood]}"
        word += n
    return prompts


# a picture stays on screen for about this many spoken words (188 a minute: about 6 seconds); a longer beat gets a
# second and third view of its scene. One picture per 40 word beat held 13 seconds and drifted off the sentence.
WORDS_PER_PICTURE = int(os.getenv("WORDS_PER_PICTURE", "18"))
VIEWS = ("the same moment from another angle, closer", "the same scene from further away, wide")
SOURCE_MAX = float(os.getenv("SOURCE_MAX", "4.5"))  # an article fills the screen this long, then the beat's picture


def source_parts(line):
    """'source: <screenshot> | <outlet> | <date> | <picture prompt>' as (screenshot, outlet, date, prompt)."""
    return tuple(([x.strip() for x in line[7:].split("|")] + ["", "", ""])[:4])


def pictures_of(ch, plan, skip=()):
    """Every picture a chapter draws, in order: (prompt, beat, pose steps). A clip line is drawn like a picture line
    when it gets no real footage (in the cloud always: the render machine has no GPU); a beat that shows an article
    draws its own picture for the time after the article."""
    clips = os.getenv("CLIPS", "1") == "1" and os.getenv("CLOUD") != "1"
    out = []
    for i, (p, shot) in enumerate(zip(lit(ch, plan), ch["shots"])):
        if not p or i in skip:
            continue
        if p.lower().startswith("clip:"):
            if clips:
                continue
            p = f"{p[5:].strip()}, showing: {shot[2][:150]}"
        if p.lower().startswith("source:"):
            p = source_parts(p)[3]
            if not p:
                continue
        views = max(1, min(3, -(-shot[1] // WORDS_PER_PICTURE)))
        poses = shot[3] if os.getenv("POSES", "1") == "1" else []
        out += [(p if v == 0 else f"{p}, {VIEWS[v - 1]}", i, poses if v == 0 else []) for v in range(views)]
    return out


def on_kaggle(s, plans, folder):
    """Every picture of the video drawn in one Kaggle run while the Mac speaks; the chapters later find them done.
    A clip line may become real footage, so it stays with the Mac."""
    if not kaggle_gpu.ready() or os.getenv("AI_IMAGES", "1") != "1":
        return 0
    todo, moves = [], []
    for i, (ch, plan) in enumerate(zip(s["chapters"], plans)):
        flat = pictures_of(ch, plan)
        _, t, m = visuals.pending([f[0] for f in flat], folder / "img" / f"{i:02d}", [f[2] for f in flat])
        todo, moves = todo + t, moves + m
    try:
        return kaggle_gpu.pictures(todo, moves)
    except (Exception, SystemExit) as err:  # Kaggle down or the account not set up: the Mac draws them as before
        if STRICT:
            raise
        print(f"  kaggle: skipped ({str(err)[:120]}), pictures stay on the Mac")
        return 0


# Only faults a viewer would notice count. The first check asked "does it fit" against the whole picture prompt and
# failed half the pictures for a sunny sky where the prompt said stormy, two archers instead of one, or a flashback
# to Uhud in a film about Qadisiyyah; the real faults (folding chairs, a cannon, writing on a Sasanian arch) were
# hidden among them. Each fault has its own field and the verdict is made here, not by the model.
FIT = """A documentary about {subject}. This picture is on screen while the narrator says:
"{line}"
It was drawn from: "{prompt}"
Look for these faults only and return JSON {{"later": "", "writing": "", "body": "", "unrelated": ""}}, each field
empty when the fault is not there, otherwise a few words on what you see:
- later: anything that did not exist in that time and place: firearms, cannons, modern furniture, tents, buildings,
  vehicles, clothes or tools from a later age, craters from explosives
- writing: letters, words, calligraphy or numbers anywhere in the picture
- body: a deformed person or animal: extra or missing limbs, melted faces, impossible poses
- unrelated: the picture has nothing to do with what the narrator says (an earlier event the line speaks of is fine)
Not faults: light, weather, colours, camera angle, how many people, or a detail of the prompt that is missing."""
FAULTS = ("later", "writing", "body", "unrelated")


def faults(answer):
    """The faults named in a model's answer (FIT): empty when the picture fits."""
    got = writer.objects(answer) if isinstance(answer, str) else answer
    got = got if isinstance(got, dict) else {}
    return "; ".join(f"{k}: {got[k]}" for k in FAULTS if str(got.get(k) or "").strip())[:200]


# what to keep out of a picture drawn again, by the fault the check found in the one before
AVOID = {"later": "{what}, modern objects, modern furniture, firearms, cannons, machinery, vehicles",
         "writing": "{what}, text, letters, words, writing, calligraphy, inscription, signs, numbers",
         "body": "deformed, extra limbs, extra fingers, missing limbs, melted face, bad anatomy"}


def avoid_of(fault):
    """The negative prompt for a redraw from a verdict like "later: folding chairs; writing: arabic letters": the
    thing the check saw, by name, and its kind. Before, every redraw got the same "nothing from a later age" and
    the same chairs came back."""
    out = []
    for part in fault.split("; "):
        key, _, what = part.partition(": ")
        if key in AVOID:
            out.append(AVOID[key].format(what=re.sub(r"[^\w ,'-]", " ", what)[:80]).strip(", "))
    return ", ".join(out)


def judge(todo, items, subject, cache_file):
    """An answer for every picture in todo, written to cache_file as it comes: first by the open model on this
    machine's GPUs (vision_local.py, no API calls), then by the Gemini key for any it could not answer. A picture
    already in the cache with the same bytes is never looked at again, in this run or the next: a run that stops
    goes on from the picture it reached instead of from the first one."""
    question = lambda x: FIT.format(subject=subject, line=items[x][1][:400], prompt=items[x][0][:300])
    if todo and os.getenv("VISION_LOCAL", "1") == "1" and shutil.which("nvidia-smi"):
        job = cache_file.with_name("vision_todo.json")
        job.write_text(json.dumps([{"path": str(x), "md5": md5(x), "question": question(x)} for x in todo]))
        run = subprocess.run([sys.executable, "vision_local.py", job, cache_file], check=False)
        if run.returncode:
            print(f"  vision: the GPU model stopped (exit {run.returncode}), the Gemini key looks at the rest", flush=True)
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    rest = [x for x in todo if cache.get(x.name, {}).get("md5") != md5(x)]
    lock = threading.Lock()

    def look(path):
        answer = writer.see(question(path), path)
        with lock:
            cache[path.name] = {"md5": md5(path), "answer": json.dumps(answer), "by": "gemini"}
            cache_file.write_text(json.dumps(cache, ensure_ascii=False))
    # the first picture the API cannot answer ends the check: before, the pool went on with every other picture,
    # ten minutes each, and a GPU machine stayed busy for hours after the answer was already known
    ex = ThreadPoolExecutor(3)
    try:
        list(ex.map(look, rest))
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return cache


def md5(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def check_pictures(s, plans, folder):
    """Every picture looked at against its own sentence and its era (judge); one with a fault is drawn again with
    another seed, twice at most, and only the new picture is looked at again. The verdicts are kept per picture in
    picture_check_cache.json, so a stopped check goes on where it stopped. The result goes to picture_check.json for
    the quality check, so a film with pictures that miss their line is held, not offered.
    PICTURE_CHECK_LIMIT=10 looks at the first ten pictures only and PICTURE_REDRAW=0 draws nothing: a test of the
    check itself in minutes, on pictures already made."""
    subject = os.getenv("DOC_SUBJECT") or s["title"]
    items = {}
    for i, (ch, plan) in enumerate(zip(s["chapters"], plans)):
        flat = pictures_of(ch, plan)
        paths = visuals.pending([f[0] for f in flat], folder / "img" / f"{i:02d}", [f[2] for f in flat])[0]
        for k, ((p, beat, _), path) in enumerate(zip(flat, paths)):
            if path and path.exists():
                items[path] = (p, ch["shots"][beat][2], 101 + k)
    if os.getenv("PICTURE_CHECK_LIMIT"):
        items = dict(list(items.items())[:int(os.getenv("PICTURE_CHECK_LIMIT"))])
    cache_file = folder / "picture_check_cache.json"
    todo, verdict = list(items), {}
    for attempt in range(3):
        cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
        here = [x for x in todo if x.exists()]
        cache = judge([x for x in here if cache.get(x.name, {}).get("md5") != md5(x)], items, subject, cache_file)
        verdict.update({x: faults(cache[x.name]["answer"]) if x in here else "the redraw did not arrive" for x in todo})
        todo = [x for x in todo if verdict[x]]
        print(f"  pictures: {len(items) - len(todo)} of {len(items)} fit their line", flush=True)
        if not todo or attempt == 2 or os.getenv("PICTURE_REDRAW", "1") == "0":
            break
        for path in todo:  # the picture and everything made from it (cut out layers, pose frames)
            for f in path.parent.glob(f"{path.stem}.*"):
                f.unlink()
        kaggle_gpu.pictures([(f"{items[x][0]}, true to {subject}", items[x][2] + 1000 * (attempt + 1), x,
                              avoid_of(verdict[x])) for x in todo], [])
    (folder / "picture_check.json").write_text(json.dumps({
        "checked": len(items), "fit": len(items) - len(todo),
        "misses": [{"file": x.name, "line": items[x][1][:160], "problem": verdict[x],
                    "by": cache.get(x.name, {}).get("by", "")} for x in todo]},
        indent=1, ensure_ascii=False), encoding="utf-8")


def plan_gate(s, plans, voices, folder):
    """The twelve checks the edit plan decides (qa.measure: cuts, cards, captions, sound, pictures, pages), worked out
    on the GPU machine before any chapter is rendered: every chapter's shots as its chapter machine will make them,
    with nothing drawn. A film that would be held is stopped here, with the checks it misses, instead of after an
    hour of rendering on five machines; only loudness and movement still need the finished film."""
    os.environ["PLAN_ONLY"] = "1"
    try:
        chapters = []
        for i, (ch, (_, dur, words)) in enumerate(zip(s["chapters"], voices)):
            _labels["n"] = _kinetic["n"] = 0  # each chapter machine starts these counts again
            shots, _, mix = timeline(ch, dur, words, folder / "img" / f"{i:02d}", plans[i], first=i == 0)
            chapters.append((shots, mix))
    finally:
        os.environ.pop("PLAN_ONLY")
    got = qa.measure(folder, chapters, sum(dur + render.PAD for _, dur, _ in voices))
    miss = qa.misses(got)
    (folder / "plan_gate.json").write_text(json.dumps({"measured": got, "misses": miss}, indent=1), encoding="utf-8")
    print(f"  plan: {len(got) - len(miss)} of {len(got)} checks pass before rendering", flush=True)
    if miss and STRICT:
        raise SystemExit("plan gate: the film would be held, so nothing is rendered. " + "; ".join(miss))


def source_card(line, folder):
    """A picture line "source: <screenshot> | <outlet> | <date>" (documentary/) as a full screen card, or None when
    the screenshot is missing (the beat then keeps the picture before it)."""
    shot, outlet, date, _ = source_parts(line)
    if not Path(shot).exists():
        return None
    spec = {"kind": "source", "image": shot, "title": outlet, "sub": date}
    path = folder / f"card_{hashlib.md5(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:8]}.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return path


def timeline(ch, dur, words, folder, plan=None, first=False):
    """Shots for one chapter: every narration beat gets its picture with a few camera moves, so the shot changes every
    few seconds, then the edit plan lands on the exact words: captions on the picture, full screen cards cut in.
    Cut points snap to the music's beat. Returns shots, credits and the sound plan (effects, music, ambience)."""
    plan = plan or {}
    secs = float(os.getenv("SECS_PER_IMAGE", "4"))
    times = [w[0] for w in words] or [0.0]
    at = lambda k: times[min(max(k, 0), len(times) - 1)]
    cum, starts = 0, []
    for _, n, *_ in ch["shots"]:
        starts.append(at(cum) if starts else 0.0)
        cum += n
    lens = [b - a for a, b in zip(starts, starts[1:] + [dur])]
    direct = sorted(plan.get("direct", []), key=lambda x: x[0])
    choice = lambda w: next((d for s, d in reversed(direct) if s <= w), {})
    beat_word = list(accumulate((n for _, n, *_ in ch["shots"]), initial=0))
    looks = [choice(w) for w in beat_word[:len(lens)]]
    pace = {"quick": 0.65, "normal": 1.0, "slow": 1.5}
    parts = [max(1, round(x / (secs * 1.2 * pace.get(d.get("pace"), 1.0)))) for x, d in zip(lens, looks)]

    use_clips = os.getenv("CLIPS", "1") == "1"
    prompts = lit(ch, plan)
    real, credits = {}, []
    for i, p in enumerate(prompts):
        if p and p.lower().startswith("clip:") and use_clips:
            got = footage.clips(p[5:].strip(), min(3, parts[i]), folder.parent / "clips")
            if got:
                real[i] = got
                credits += [c for _, c in got]
    proof = {i: source_card(p, folder) for i, p in enumerate(prompts) if p and p.lower().startswith("source:")}
    flat = pictures_of(ch, plan, skip=real)
    views = {i: [] for i in range(len(prompts))}
    for (p, i, _), pic in zip(flat, visuals.scenes([f[0] for f in flat], folder, [f[2] for f in flat])):
        if pic:
            views[i].append(pic)
            for x in [pic, *visuals.pose_files(pic)] if not PLAN_ONLY() else []:
                motion.prepare(x, p)
    base = [views[i][0] if views[i] else None for i in range(len(prompts))]
    pool, stock_credits = ([], []) if any(base) or real else visuals.stock(ch["visuals"], sum(parts), folder)

    shots, k, last = [], 0, next((b for b in base if b), None)
    for i, (img, length, n) in enumerate(zip(base, lens, parts)):
        look = looks[i]
        air = look.get("air") or motion.air(f"{prompts[i] or ''} {ch['shots'][i][2]}")
        air = None if air == "none" else air
        if i in real:
            for j in range(n):
                clip, _ = real[i][j % len(real[i])]
                shots.append([clip, length / n, (j // len(real[i])) * length / n])
            continue
        if proof.get(i):  # the article fills the screen briefly, then the beat's own picture takes over
            card = min(length, SOURCE_MAX) if length - SOURCE_MAX >= 1.5 else length
            shots.append([proof[i], card, 0.0])
            length -= card
            if length <= 0:
                continue
            n = max(1, round(length / secs))
        pics = views[i]
        n = max(n, len(pics))
        for j in range(n):
            if not img and pool:
                last, k = pool[k % len(pool)], k + 1
            last = pics[j * len(pics) // n] if pics else img or last
            if last is None:
                raise RuntimeError(f"no visuals for chapter '{ch['title']}', check internet or the visuals line")
            # the first shot of a beat plays its poses (render does that), the next ones hold the final pose
            moved = visuals.pose_files(last) if j or not pics else []
            move = look.get("move")  # the director's move opens the beat, the next framings vary from it
            move = writer.MOVES[(writer.MOVES.index(move) + 2 * j) % 7] if move and j else move
            shots.append([moved[-1] if moved else last, length / n, 0.0, {"air": air, **({"move": move} if move else {})}])
    if not first and len(shots[0]) > 3:
        shots[0][3]["intro"] = "sparkle"

    pics = list(dict.fromkeys(x for v in views.values() for x in v))
    for word, e in sorted(plan.get("elements", []), key=lambda x: x[0]):
        t = at(word)
        if e.get("kind") in CARDS and dur - t > 2.0:
            e, credit = card_spec(e, folder, pics)
            credits += [credit] if credit else []
            if isinstance(e, Path):
                shots = cut_in(shots, t, e, min(3.5, dur - t))
                continue
        if e.get("kind") in OVERLAYS:
            if e["kind"] in ("caption", "title"):
                e = kinetic(e, folder, choice(word).get("caption_fx")) or (tier2(e, folder) if e["kind"] == "caption" else e)
            put_caption(shots, t, e)

    sections, beats = sound.plan([(at(w), m) for w, m in plan.get("moods", [])], dur, ch["title"])
    edges = list(accumulate(x[1] for x in shots))[:-1]
    for i, b in enumerate(edges):
        near = min(beats, key=lambda g: abs(g - b), default=b)
        card_start = shots[i + 1][0].suffix == ".json"  # a card stays on its word, the music adapts around it
        if not card_start and abs(near - b) < 0.25 and shots[i][1] + near - b > 1 and shots[i + 1][1] - (near - b) > 1:
            shots[i][1] += near - b
            shots[i + 1][1] -= near - b
    cuts = [(at(w), d["cut"]) for w, d in direct if d.get("cut")]
    fx_events = plan_fx(shots, ch, folder, [at(w) for w in plan.get("turns", [])], first, cuts,
                        [at(w) for w, d in direct if d.get("leak")][:2], plan.get("open"))
    t = 0.0
    for shot in shots:  # a caption keeps its word after the cuts moved: "at" is seconds into the shot
        cap = shot[3].get("caption") if len(shot) > 3 else None
        if cap:
            cap["at"] = round(min(max(0.0, cap["_t"] - t), max(0.0, shot[1] - 1.2)), 3)
        t += shot[1]

    events, t = [], 0.0
    for shot in shots:
        extra = shot[3] if len(shot) > 3 else {}
        if extra.get("intro"):
            events.append((t, "sparkle"))
        if extra.get("caption"):
            c = t + extra["caption"].get("at", 0)
            kind = extra["caption"].get("kind")
            fx_kind = extra["caption"].get("fx")
            events += [(c + d, n) for d, n in FX_SOUND[fx_kind]] if fx_kind else {
                "title": [(c, "swell"), (c + 0.35, "hit")], "headline": [(c, "paper"), (c + 0.1, "hit")]}.get(
                kind, [(c + 0.15, "pop")])
        if shot[0].suffix == ".json":
            spec = json.loads(shot[0].read_text(encoding="utf-8"))
            events += [(t + d, name) for d, name in CARD_SOUND.get(spec["kind"], [(0, "whoosh")])]
            events += [(t + 1.0, "pop")] if spec.get("circle") else []
            events += [(t + 0.3 + 0.35 * k, "pin") for k in range(len(spec.get("places", [])))]
            events += [(t + 0.3 + 0.35 * k, "pop") for k in range(len(spec.get("items", spec.get("events", []))))]
        t += shot[1]
    for word in plan.get("turns", []):
        if all(abs(at(word) - x) > 1.5 for x, name in events if name == "hit"):
            events += [(at(word) - 0.35, "swell"), (at(word), "hit")]  # the riser ends on the turn
    events += [(at(w), f"moments/{m}") for w, m in plan.get("sounds", [])]  # a scream, a heartbeat, swords
    for w, name in plan.get("accents", []):  # the director's hits, kept clear of the ones already there
        if all(abs(at(w) - x) > 1.2 for x, _ in events):
            events.append((at(w), name))
    if plan.get("drop") is not None:
        events.append((at(plan["drop"]), "drop"))
    events += fx_events
    scenes = [(at(w), a) for w, a in plan.get("ambience", [])]
    return shots, credits + stock_credits, (sorted(events), sections, scenes)


def plan_fx(shots, ch, folder, turns, first, cuts=(), glow=(), opening=None):
    """Where the Remotion transitions and HyperFrames titles go (fx.py), written into the shots for render.py:
    the chapter name written on by hand over the opening, a page flip from the last chapter into this one, and at a
    turn of the story a transition on the nearest cut (or a light leak when no cut is near). When the director chose
    them (cuts: [(time, kind)], glow: light leak times, opening: the chapter's transition), its choices are used
    instead of the turns. Returns their sounds."""
    if not shots or len(shots[0]) < 4 or os.getenv("FX", "1") != "1":
        return []
    style = fx.KINDS.get(os.getenv("STYLE", ""), fx.KINDS["default"])
    picture = lambda s: len(s) > 3 and s[0].suffix.lower() in (".jpg", ".png")
    lead, events = shots[0][3], []
    if os.getenv("FX_TITLES", "1") == "1":
        look = "title" if os.getenv("STYLE") == "storybook" else "prism"
        name = (ch.get("card") or ch["title"]).strip()
        got = fx.frames(look, {"text": name}, folder / "fx" / f"{look}_{fx.key(look, name)}")
        if got:
            lead["fx_title"] = str(got)
    if not first and picture(shots[0]):
        if opening == "book-flip" and os.getenv("STYLE") != "storybook":  # a page turn only suits the storybook look
            opening = None
        lead["fx_open"] = list(dict.fromkeys([opening, *style["open"]])) if opening else style["open"]
        events.append((0.0, fx.SOUND.get(lead["fx_open"][0]) or "whoosh"))
    edges = list(accumulate(s[1] for s in shots))[:-1]
    leaks = [round(max(0.3, t - 0.3), 2) for t in glow]
    wanted = [(t, [k, *style["turn"]]) for t, k in cuts] or [(t, style["turn"]) for t in turns]
    last_cut = -9.0
    for tt, kinds in wanted:
        if tt - last_cut < 6:  # two transitions close together feel like a slideshow
            continue
        near = [i for i, b in enumerate(edges, 1) if abs(b - tt) < 1.2 and picture(shots[i - 1]) and picture(shots[i])
                and shots[i - 1][1] > 1.2 and shots[i][1] > 1.2 and "fx_turn" not in shots[i][3]]
        if near:
            i = min(near, key=lambda i: abs(edges[i - 1] - tt))
            shots[i][3]["fx_turn"] = list(dict.fromkeys(kinds))
            events.append((edges[i - 1] - 0.4, fx.SOUND.get(kinds[0]) or "whoosh"))
            last_cut = tt
        elif tt > 1.0 and not cuts:
            leaks.append(round(tt - 0.6, 2))
    if leaks:
        lead["fx_leaks"] = leaks
    return [(t, n) for t, n in events if n]


def name(e):
    """A short readable name of an element for edit_plan.md."""
    if e.get("places"):
        return ", ".join(p.get("name", "") for p in e["places"])
    if e.get("kind") == "card":
        return f"{str(e.get('card', '')).split('.', 1)[-1]}: {writer.headline(e)}"
    if e.get("kind") == "calendar":
        return f"{e.get('month', '')} {e.get('day', '')} {e.get('year', '')}".replace("  ", " ").strip()
    return str(e.get("title") or e.get("value") or e.get("text") or e.get("query") or "")[:60]


def write_plan(folder, script, plans, voices):
    """edit_plan.md: what lands on screen and in the sound for every sentence, when and why."""
    out = [f"# Edit plan: {script['title']}", ""]
    for ch, plan, (_, _, words) in zip(script["chapters"], plans, voices):
        times = [w[0] for w in words] or [0.0]
        at = lambda k: times[min(k, len(times) - 1)]
        out += [f"## {ch['title']}", "", "| time | sentence | on screen | why | mood | sound |", "|---|---|---|---|---|---|"]
        elements = dict(plan.get("elements", []))
        moods, places = dict(plan.get("moods", [])), dict(plan.get("ambience", []))
        sounds = plan.get("sounds", [])
        for k, (word, line) in enumerate(plan.get("lines", [])):
            nxt = plan["lines"][k + 1][0] if k + 1 < len(plan["lines"]) else 10 ** 9
            hits = [e for w, e in elements.items() if word <= w < nxt]
            shown = "; ".join(f"{e['kind']}: {name(e)}".strip() for e in hits)
            why = "; ".join(e.get("why", "") for e in hits if e.get("why"))
            turn = " (turn)" if word in plan.get("turns", []) else ""
            heard = ", ".join([places.get(word, "")] + [m for w, m in sounds if word <= w < nxt]).strip(", ")
            out.append(f"| {at(word):.1f}s | {line[:80]} | {shown} | {why} | {moods.get(word, '')}{turn} | {heard} |")
        out.append("")
    (folder / "edit_plan.md").write_text("\n".join(out), encoding="utf-8")


def build(folder):
    t0 = time.time()
    if os.getenv("LLM_API_KEY"):
        writer.add_pictures(folder)
    s = writer.read_script(folder / "script.md")
    print(f"build: {s['title']}")
    # the edit plan only needs the script text, so Gemini plans while the Mac speaks (a busy Gemini cost an hour
    # when the two ran one after the other)
    with ThreadPoolExecutor(2) as ex:
        planning = ex.submit(writer.edits, folder, s) if os.getenv("EDIT", "1") == "1" and os.getenv("LLM_API_KEY") else None
        get_plans = lambda: planning.result() if planning else [{}] * len(s["chapters"])
        if os.getenv("CLOUD") == "1":  # on Kaggle the voice and the pictures share the GPUs, so one after the other
            voices = voice.narrate(s["chapters"], folder)
            plans = get_plans()
            on_kaggle(s, plans, folder)
            # only the GPU stage checks: a chapter machine has no GPU to redraw on, and five of them checking the same
            # pictures at once deleted the ones they disliked and spent the vision quota five times over
            if os.getenv("PICTURE_CHECK", "1") == "1" and not os.getenv("RENDER_PART"):
                check_pictures(s, plans, folder)
                plan_gate(s, plans, voices, folder)
        else:  # the pictures need only the prompts and the moods, so Kaggle draws them while the Mac is still speaking
            drawing = ex.submit(lambda: on_kaggle(s, get_plans(), folder))
            voices = voice.narrate(s["chapters"], folder)
            plans = get_plans()
            drawing.result()
    write_plan(folder, s, plans, voices)
    if os.getenv("STOP_AFTER") == "pictures":  # the cloud GPU stage ends here; the render runs on a CPU machine
        print(f"voice and pictures ready: {folder}", flush=True)
        return
    laps = {"voice": time.time() - t0}
    (folder / "chapters").mkdir(exist_ok=True)
    # in the cloud every chapter renders on its own machine at the same time: RENDER_PART=<n> renders only chapter n,
    # RENDER_PART=join takes the chapters already made and puts the film together
    part = os.getenv("RENDER_PART")

    def finish(i, shots, mix, audio, dur, words):
        """Sound and render of one chapter, run while the next chapter's pictures are drawn."""
        made = [os.path.getmtime(x[0]) for x in shots]  # a redrawn picture means a new chapter clip
        key = hashlib.md5(f"{shots}{made}{mix}{audio}{os.getenv('HEIGHT')}".encode()).hexdigest()[:8]
        out = folder / "chapters" / f"{i:02d}_{key}.mp4"
        # chapters render in order, so the last frame of the one before is there for the page flip
        hold, last = folder / "chapters" / f"{i - 1:02d}_last.png", folder / "chapters" / f"{i:02d}_last.png"
        if part is not None and i and not hold.exists():
            # rendered side by side, the chapter before is not done yet: its last picture stands in for its last frame
            ch, plan = s["chapters"][i - 1], plans[i - 1]
            before = [x for x in visuals.pending([f[0] for f in pictures_of(ch, plan)], folder / "img" / f"{i - 1:02d}")[0]
                      if x and x.exists()]
            if before:
                w, h = visuals.size()
                render.ffmpeg("-i", before[-1], "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
                              "-frames:v", 1, hold)
        edge = {"hold": hold if i else None, "last": last}
        if os.getenv("SOUND", "1") == "0":
            render.chapter(shots, audio, dur, words, out, **edge)
        elif not out.exists():
            mixed, clean = out.with_suffix(".wav"), out.with_suffix(".voice.wav")
            sound.mix(render.clean_voice(audio, clean), dur + render.PAD, *mix, mixed)
            render.chapter(shots, mixed, dur, words, out, mixed=True, **edge)
            mixed.unlink()
            clean.unlink()
        print(f"  render {i + 1}/{len(voices)} done", flush=True)
        return out, (shots, mix)

    credits, cover, jobs, done, kinds = [], None, [], [], {}
    if part == "join":  # each chapter machine left its clip and its plan; nothing is drawn or rendered again
        for i in range(len(voices)):
            got = json.loads((folder / "chapters" / f"{i:02d}_cloud.json").read_text(encoding="utf-8"))
            kinds.update(got.get("kinds", {}))
            shots = [(Path(x[0]), *x[1:]) for x in got["shots"]]
            cover = cover or (Path(got["cover"]) if got["cover"] else None)
            credits += [c for c in got["credits"] if c not in credits]
            done.append((folder / "chapters" / f"{i:02d}_cloud.mp4", (shots, got["mix"])))
    else:
        with ThreadPoolExecutor(1) as ex:
            for i, (ch, (audio, dur, words)) in enumerate(zip(s["chapters"], voices)):
                if part is not None and int(part) != i:
                    continue
                print(f"  visuals {i + 1}/{len(voices)}: {dur / 60:.1f} min", flush=True)
                shots, cr, mix = timeline(ch, dur, words, folder / "img" / f"{i:02d}", plans[i], first=i == 0)
                cover = cover or next((x[0] for x in shots if x[0].suffix == ".jpg"), None)
                credits += [c for c in cr if c not in credits]
                jobs.append(ex.submit(finish, i, shots, mix, audio, dur, words))
                if part is not None:  # what the join needs from this chapter, next to its clip (cards are made here)
                    specs = {str(x[0]): json.loads(x[0].read_text(encoding="utf-8")) for x in shots if x[0].suffix == ".json"}
                    (folder / "chapters" / f"{i:02d}_plan.json").write_text(json.dumps(
                        {"shots": shots, "mix": mix, "credits": cr, "cover": cover, "kinds": specs}, default=str),
                        encoding="utf-8")
            visuals.release()
            laps["visuals"] = time.time() - t0 - sum(laps.values())
            done = [j.result() for j in jobs]
    if part not in (None, "join"):
        print(f"chapter {int(part) + 1} rendered", flush=True)
        return
    clips = [c for c, _ in done]
    starts = list(accumulate((dur + render.PAD for _, dur, _ in voices[:-1]), initial=0.0))
    t = starts[-1] + voices[-1][1] + render.PAD

    render.join(clips, folder / "final.mp4")
    laps["render"] = time.time() - t0 - sum(laps.values())
    render.srt([v[2] for v in voices], starts, folder / "subtitles.srt")
    visuals.thumbnail(cover, s["thumb"], folder / "thumbnail.jpg")
    qa.report(folder, [x for _, x in done], t, laps, kinds)
    if os.getenv("LLM_API_KEY"):
        try:  # the video is finished; a busy Gemini only delays the description, which `make.py build` redoes
            writer.youtube_meta(s, starts, credits, folder / "youtube.txt")
        except (Exception, SystemExit) as err:
            print(f"  youtube.txt not written ({str(err)[:80]}), run the same build again later to write it")
    if os.getenv("META", "1") == "1":
        try:  # titles, description and tags from YouTube research, written by the strongest Kaggle model
            meta.pack(folder, s, starts, credits)
        except (Exception, SystemExit) as err:
            print(f"  youtube pack not made ({str(err)[:100]}), the dashboard can make it later")
    print(f"done: {folder / 'final.mp4'} ({t / 60:.1f} min video, built in {(time.time() - t0) / 60:.1f} min)")
    print("  time per minute of video: " + ", ".join(f"{k} {v / max(t / 60, 0.01) / 60:.1f} min" for k, v in laps.items()))


def pending(path, count):
    topics = [l.strip() for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    return [t for t in topics if not (folder_for(t) / "final.mp4").exists()][:count]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Long history documentary maker")
    ap.add_argument("mode", choices=["script", "build", "auto", "check"])
    ap.add_argument("target", nargs="?", help="topic (script, auto) or output folder (build)")
    ap.add_argument("--file", help="topics file, one topic per line")
    ap.add_argument("--count", type=int, default=2)
    ap.add_argument("--minutes", type=int, default=int(os.getenv("MINUTES", "60")))
    a = ap.parse_args()

    made = []
    if a.mode == "check":  # only the picture check, on the pictures already in the folder (cloud.py check)
        s = writer.read_script(Path(a.target) / "script.md")
        check_pictures(s, writer.edits(Path(a.target), s), Path(a.target))
        sys.exit()
    if a.mode == "build":
        build(Path(a.target))
        made.append(Path(a.target))
    else:
        for topic in pending(a.file, a.count) if a.file else [a.target]:
            folder = script(topic, a.minutes)
            if a.mode == "auto":
                build(folder)
                made.append(folder)
    if sys.stdin.isatty():
        for folder in made:
            clean.ask(folder)
