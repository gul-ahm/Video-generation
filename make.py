import argparse
import hashlib
import json
import os
import re
import sys
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
            hf.render(e["card"], slots, HF_SECONDS, out)
        except Exception as err:  # no browser or a broken card: the words still show as a caption
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
        hf.render(card, slots, HF_SECONDS, out)
    except Exception as err:  # no browser: the plain caption still works
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
    got = fx.frames(kind, values, out)
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


def on_kaggle(s, plans, folder):
    """Every picture of the video drawn in one Kaggle run while the Mac speaks; the chapters later find them done.
    A clip line may become real footage, so it stays with the Mac."""
    if not kaggle_gpu.ready() or os.getenv("AI_IMAGES", "1") != "1":
        return 0
    todo, moves = [], []
    for i, (ch, plan) in enumerate(zip(s["chapters"], plans)):
        # without real footage a clip line is drawn like timeline() does; in the cloud the render machine has no GPU,
        # so the picture is drawn here anyway and stays unused when footage is found
        clips = os.getenv("CLIPS", "1") == "1" and os.getenv("CLOUD") != "1"
        prompts = [(None if clips else f"{p[5:].strip()}, showing: {shot[2][:150]}") if p and p.lower().startswith("clip:")
                   else p for p, shot in zip(lit(ch, plan), ch["shots"])]
        prompts = [None if p and p.lower().startswith("source:") else p for p in prompts]
        poses = [x[3] if os.getenv("POSES", "1") == "1" else [] for x in ch["shots"]]
        _, t, m = visuals.pending(prompts, folder / "img" / f"{i:02d}", poses)
        todo, moves = todo + t, moves + m
    try:
        return kaggle_gpu.pictures(todo, moves)
    except (Exception, SystemExit) as err:  # Kaggle down or the account not set up: the Mac draws them as before
        print(f"  kaggle: skipped ({str(err)[:120]}), pictures stay on the Mac")
        return 0


def source_card(line, folder):
    """A picture line "source: <screenshot> | <outlet> | <date>" (documentary/) as a full screen card, or None when
    the screenshot is missing (the beat then keeps the picture before it)."""
    shot, outlet, date = ([x.strip() for x in line[7:].split("|")] + ["", ""])[:3]
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
        if p and p.lower().startswith("clip:"):
            query = p[5:].strip()
            got = footage.clips(query, min(3, parts[i]), folder.parent / "clips") if use_clips else []
            if got:
                real[i] = got
                credits += [c for _, c in got]
            prompts[i] = f"{query}, showing: {ch['shots'][i][2][:150]}"
    poses = [s[3] if os.getenv("POSES", "1") == "1" else [] for s in ch["shots"]]
    proof = {i: source_card(p, folder) for i, p in enumerate(prompts) if p and p.lower().startswith("source:")}
    base = visuals.scenes([None if i in real or i in proof else p for i, p in enumerate(prompts)], folder, poses)
    for i, card in proof.items():  # the article itself fills the screen for this beat, in one unbroken shot
        base[i], parts[i] = card, 1 if card else parts[i]
    pool, stock_credits = ([], []) if any(base) or real else visuals.stock(ch["visuals"], sum(parts), folder)
    for img, p in zip(base, prompts):
        for pic in [img, *visuals.pose_files(img)] if img and img.suffix != ".json" else []:
            motion.prepare(pic, p)

    shots, k, last = [], 0, next((b for b in base if b), None)
    for i, (img, length, n) in enumerate(zip(base, lens, parts)):
        look = looks[i]
        air = look.get("air") or motion.air(f"{prompts[i] or ''} {ch['shots'][i][2]}")
        air = None if air == "none" else air
        for j in range(n):
            if i in real:
                clip, _ = real[i][j % len(real[i])]
                shots.append([clip, length / n, (j // len(real[i])) * length / n])
                continue
            if not img and pool:
                last, k = pool[k % len(pool)], k + 1
            last = img or last
            if last is None:
                raise RuntimeError(f"no visuals for chapter '{ch['title']}', check internet or the visuals line")
            # the first shot of a beat plays its poses (render does that), the next ones hold the final pose
            moved = visuals.pose_files(last) if j or not img else []
            move = look.get("move")  # the director's move opens the beat, the next framings vary from it
            move = writer.MOVES[(writer.MOVES.index(move) + 2 * j) % 7] if move and j else move
            shots.append([moved[-1] if moved else last, length / n, 0.0, {"air": air, **({"move": move} if move else {})}])
    if not first and len(shots[0]) > 3:
        shots[0][3]["intro"] = "sparkle"

    pics = list(dict.fromkeys(b for b in base if b))
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

    def finish(i, shots, mix, audio, dur, words):
        """Sound and render of one chapter, run while the next chapter's pictures are drawn."""
        made = [os.path.getmtime(x[0]) for x in shots]  # a redrawn picture means a new chapter clip
        key = hashlib.md5(f"{shots}{made}{mix}{audio}{os.getenv('HEIGHT')}".encode()).hexdigest()[:8]
        out = folder / "chapters" / f"{i:02d}_{key}.mp4"
        # chapters render in order, so the last frame of the one before is there for the page flip
        hold, last = folder / "chapters" / f"{i - 1:02d}_last.png", folder / "chapters" / f"{i:02d}_last.png"
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

    credits, cover, jobs = [], None, []
    with ThreadPoolExecutor(1) as ex:
        for i, (ch, (audio, dur, words)) in enumerate(zip(s["chapters"], voices)):
            print(f"  visuals {i + 1}/{len(voices)}: {dur / 60:.1f} min", flush=True)
            shots, cr, mix = timeline(ch, dur, words, folder / "img" / f"{i:02d}", plans[i], first=i == 0)
            cover = cover or next((x[0] for x in shots if x[0].suffix == ".jpg"), None)
            credits += [c for c in cr if c not in credits]
            jobs.append(ex.submit(finish, i, shots, mix, audio, dur, words))
        visuals.release()
        laps["visuals"] = time.time() - t0 - sum(laps.values())
        done = [j.result() for j in jobs]
    clips = [c for c, _ in done]
    starts = list(accumulate((dur + render.PAD for _, dur, _ in voices[:-1]), initial=0.0))
    t = starts[-1] + voices[-1][1] + render.PAD

    render.join(clips, folder / "final.mp4")
    laps["render"] = time.time() - t0 - sum(laps.values())
    render.srt([v[2] for v in voices], starts, folder / "subtitles.srt")
    visuals.thumbnail(cover, s["thumb"], folder / "thumbnail.jpg")
    qa.report(folder, [x for _, x in done], t, laps)
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
    ap.add_argument("mode", choices=["script", "build", "auto"])
    ap.add_argument("target", nargs="?", help="topic (script, auto) or output folder (build)")
    ap.add_argument("--file", help="topics file, one topic per line")
    ap.add_argument("--count", type=int, default=2)
    ap.add_argument("--minutes", type=int, default=int(os.getenv("MINUTES", "60")))
    a = ap.parse_args()

    made = []
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
