"""YouTube pack for a finished video: title options, description, hashtags and tags, from real search data.

1. Research: yt-dlp searches YouTube for the topic (no login, no API key) and keeps the long videos with their views.
   Each title is measured for the patterns history channels use (a number, a question, a colon, "The man who",
   ALL CAPS, a strong word, its length), and every pattern gets its median views against titles without it.
2. Writing: the strongest model on Kaggle (Gemini 4 Argon, high reasoning) gets the script, the chapters and that
   evidence, as a Kaggle benchmark task pushed and run with the kaggle CLI. If Kaggle cannot run it, the Gemini key
   of the project writes it instead.
3. youtube_pack.json and youtube_pack.md in the video folder; the dashboard shows them with copy buttons.
   Chapter times and image credits are added by the code, so they are exact.

python meta.py output/<video folder>     makes (or remakes) the pack of one video
"""
import base64
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
from itertools import accumulate
from pathlib import Path

import writer

MODEL = os.getenv("META_MODEL", "gemini-4-argon")
TASK = "history-studio-youtube-pack"
STRONG = r"\b(secret|mystery|mysterious|vanished|haunted|truth|real|untold|forgotten|dark|deadly|impossible|shocking|lost|why|how)\b"
PATTERNS = {
    "a number": lambda t: bool(re.search(r"\d", t)),
    "a question": lambda t: "?" in t or bool(re.match(r"(how|why|what|who|did|was)\b", t, re.I)),
    "a colon or dash": lambda t: bool(re.search(r"[:|]| - ", t)),
    "'The ... who / that'": lambda t: bool(re.search(r"\b(who|that)\b", t, re.I)) and t.lower().startswith("the "),
    "an ALL CAPS word": lambda t: bool(re.search(r"\b[A-Z]{4,}\b", t)),
    "a strong word": lambda t: bool(re.search(STRONG, t, re.I)),
    "50 characters or less": lambda t: len(t) <= 50,
}

PROMPT = """You write the YouTube title, description and tags for a history documentary channel. The video is
finished; everything you write must be true to its script below. No claims the video does not make, no fake
quotes, no "you won't believe" bait. Clickable comes from curiosity and a clear promise, backed by the data.

Video: {title}
Length: {minutes} minutes
Chapters: {chapters}
What the narration says (first lines of each chapter):
{summary}

Research: {count} long YouTube videos found when searching for "{topic}" and its main subject, sorted by views:
{top}

Title patterns in those results (median views with the pattern / without it, how many titles have it):
{patterns}

Think about what the top titles have in common, what the searcher wants, and which patterns really lift views here
(a pattern on very few titles is weak evidence). Then return only this JSON:
{{"pattern_notes": ["3 to 5 short findings from the research, each with its numbers"],
  "titles": [{{"title": "under 70 characters, the key name early", "pattern": "which pattern it uses",
              "why": "one line, tied to the research"}}],
  "best": 0,
  "description": "first two lines are the hook (they show in search), then 2 short paragraphs about the story;
                  no chapter list and no hashtags (they are added after it)",
  "hashtags": ["#three", "#for", "#the_top"],
  "tags": ["10 to 15 search phrases people type"],
  "thumbnail_text": "3 to 5 words"}}
Give exactly 5 titles, each a different approach."""

TASK_CODE = '''import base64
import kaggle_benchmarks as kbench

PROMPT = base64.b64decode("{b64}").decode()


@kbench.task(name="{task}", description="YouTube title, description and tags for one History Studio video")
def pack(llm) -> dict:
    try:
        text = llm.prompt(PROMPT, reasoning="high")
    except TypeError:
        text = llm.prompt(PROMPT)
    print("PACK" + "_START" + text + "PACK" + "_END")  # split, so the source itself never holds the markers
    return {{"pack": text}}


pack.run(kbench.llm)
'''


def research(topic, folder):
    """Long YouTube videos for the topic with views, and the median views per title pattern."""
    exe = Path(sys.executable).with_name("yt-dlp")
    # a dramatic title finds little; the person or event in it (the part in brackets, else the part before ":") is
    # what people search for
    inner = re.search(r"\(([^)]+)\)", topic)
    subject = inner.group(1) if inner else re.split(r"[:|]", topic)[0]
    seen = {}
    for query in dict.fromkeys([subject.strip(), f"{subject.strip()} history documentary", topic]):
        out = subprocess.run([str(exe) if exe.exists() else "yt-dlp", f"ytsearch40:{query}", "--flat-playlist", "-j",
                              "--no-warnings"], capture_output=True, text=True, timeout=180).stdout
        for line in out.splitlines():
            v = json.loads(line)
            if v.get("view_count") and (v.get("duration") or 0) > 180:
                seen[v["id"]] = {"title": v["title"], "views": int(v["view_count"]), "channel": v.get("channel", ""),
                                 "minutes": round(v["duration"] / 60)}
    videos = sorted(seen.values(), key=lambda v: -v["views"])
    stats = []
    for name, test in PATTERNS.items():
        yes = [v["views"] for v in videos if test(v["title"])]
        no = [v["views"] for v in videos if not test(v["title"])]
        if yes and no:
            a, b = statistics.median(yes), statistics.median(no)
            stats.append({"pattern": name, "with": round(a), "without": round(b), "lift": round(a / b, 2), "titles": len(yes)})
    found = {"topic": topic, "videos": videos, "patterns": sorted(stats, key=lambda s: -s["lift"])}
    (folder / "research.json").write_text(json.dumps(found, indent=1), encoding="utf-8")
    return found


def kaggle(prompt, work):
    """The prompt as a Kaggle benchmark task, run on MODEL. Returns the model's text, or None."""
    exe = Path(sys.executable).with_name("kaggle")
    k = lambda *a, t=1800: subprocess.run([str(exe) if exe.exists() else "kaggle", "benchmarks", "tasks", *a],
                                          capture_output=True, text=True, timeout=t)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    (work / "task.py").write_text(TASK_CODE.format(b64=base64.b64encode(prompt.encode()).decode(), task=TASK))
    writer.say(f"kaggle: {MODEL} is writing the YouTube pack (benchmark task)")
    for step in (("push", TASK, "-f", str(work / "task.py"), "--wait", "900"),
                 ("run", TASK, "-m", MODEL, "--wait", "1800"),
                 ("download", TASK, "-m", MODEL, "-o", str(work / "out"), "-f")):
        r = k(*step)
        if r.returncode:
            writer.say(f"kaggle {step[0]} failed: {(r.stderr or r.stdout).strip()[-200:]}")
            return None
    return answer(work / "out")


def answer(out):
    """The pack in the downloaded run files, as JSON text, or None."""
    for f in sorted(out.rglob("*")):
        if f.is_file() and f.stat().st_size < 20_000_000:
            text = f.read_text(encoding="utf-8", errors="ignore")
            try:
                found = strings(json.loads(text))
            except ValueError:
                found = [text]
            for s in found:  # the answer is the piece that parses as the pack; the prompt also mentions "titles"
                for piece in re.findall(r"PACK_START(.*?)PACK_END", s, re.S) or [s]:
                    try:
                        got = writer.objects(piece)
                    except ValueError:
                        continue
                    if isinstance(got, dict) and isinstance(got.get("titles"), list) and got["titles"] \
                            and isinstance(got["titles"][0], dict) and "description" in got:
                        return json.dumps(got)
    writer.say("kaggle: the run finished but its answer was not found in the downloaded files")
    return None


def strings(node):
    """Every string inside a downloaded run file."""
    if isinstance(node, str):
        return [node]
    items = node.values() if isinstance(node, dict) else node if isinstance(node, list) else []
    return [s for v in items for s in strings(v)]


def starts_of(folder, chapters):
    """Chapter start times: from make.py when it calls, else from the voice files on disk."""
    import soundfile
    import render
    waves = {}
    for w in sorted((folder / "audio").glob("[0-9][0-9]_*.wav"), key=lambda p: p.stat().st_mtime):
        waves[int(w.name[:2])] = w
    if len(waves) < len(chapters):
        return []
    durs = [soundfile.info(str(waves[i])).duration for i in range(len(chapters))]
    return list(accumulate((d + render.PAD for d in durs[:-1]), initial=0.0))


def pack(folder, script=None, starts=None, credits=()):
    folder = Path(folder)
    script = script or writer.read_script(folder / "script.md")
    starts = starts if starts is not None else starts_of(folder, script["chapters"])
    topic = (folder / "topic.txt").read_text().strip() if (folder / "topic.txt").exists() else script["title"]
    found = research(topic, folder)
    words = sum(len(shot[2].split()) for c in script["chapters"] for shot in c["shots"])
    first = lambda c: " ".join(writer.sentences(" ".join(s[2] for s in c["shots"]))[:2])
    prompt = PROMPT.format(
        title=script["title"], topic=topic, minutes=round(words / 188) or "?",
        chapters=", ".join(c["title"] for c in script["chapters"]),
        summary="\n".join(f"- {c['title']}: {first(c)}" for c in script["chapters"]),
        count=len(found["videos"]),
        top="\n".join(f"{v['views']:>11,} views | {v['title']} | {v['channel']}" for v in found["videos"][:25]) or "none found",
        patterns="\n".join(f"{p['pattern']}: {p['with']:,} / {p['without']:,} (x{p['lift']}), {p['titles']} titles"
                           for p in found["patterns"]) or "not enough titles")
    text = kaggle(prompt, folder / ".kaggle_pack") if os.getenv("META_KAGGLE", "1") == "1" else None
    used = MODEL if text else "Gemini key (Kaggle not used)"
    meta = writer.objects(text) if text else writer.llm(prompt, as_json=True, temp=0.7)
    meta = meta["items"][0] if "items" in meta else meta
    chapters = "\n".join(f"{writer.stamp(t)} {c['title']}" for t, c in zip(starts, script["chapters"]))
    tail = [chapters and f"Chapters\n{chapters}", credits and "Image credits\n" + "\n".join(credits),
            " ".join(meta.get("hashtags", []))]
    meta.update(description="\n\n".join([meta.get("description", "").strip(), *[x for x in tail if x]]),
                model=used, research={"videos": len(found["videos"]), "top": found["videos"][:10],
                                      "patterns": found["patterns"]})
    (folder / "youtube_pack.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    best = meta.get("best", 0) if isinstance(meta.get("best"), int) else 0
    lines = ["# YouTube pack", f"Written by {used}, from {len(found['videos'])} YouTube videos on the topic.", "", "## Titles"]
    lines += [f"{'**' if i == best else ''}{i + 1}. {t.get('title')}{'** (best)' if i == best else ''}  \n   {t.get('pattern')}: {t.get('why')}"
              for i, t in enumerate(meta.get("titles", []))]
    lines += ["", "## Description", meta["description"], "", "## Tags", ", ".join(meta.get("tags", [])), "",
              "## Thumbnail text", meta.get("thumbnail_text", ""), "", "## What the research showed",
              *[f"- {n}" for n in meta.get("pattern_notes", [])]]
    (folder / "youtube_pack.md").write_text("\n".join(lines), encoding="utf-8")
    shutil.rmtree(folder / ".kaggle_pack", ignore_errors=True)
    print(f"  youtube pack: {len(meta.get('titles', []))} titles by {used}, see {folder / 'youtube_pack.md'}", flush=True)
    return meta


if __name__ == "__main__":
    from dotenv import load_dotenv
    os.chdir(Path(__file__).parent)
    load_dotenv() or load_dotenv(".env.example")
    if len(sys.argv) != 2:
        sys.exit(__doc__.strip())
    pack(sys.argv[1])
