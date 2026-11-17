"""Quality check of a finished video against the two references: the El Dorado explainer (edit: cuts, captions,
cards, sound) and the cartoon (format: something always moves). Writes qa.md next to the video and prints a summary.
Videos in assets/reference/ are measured the same way and shown side by side with ours (side_by_side.jpg).

python qa.py output/<video folder>       show the last check of a video
python qa.py compare output/<video folder>   redo the side by side with the videos in assets/reference/
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from render import FFMPEG

TARGETS = {  # name: (low, high, what the reference measured)
    "cuts per minute": (10, 16, "12.7"),
    "median shot (s)": (3.0, 5.5, "4.3"),
    "frames with movement (%)": (60, 100, "94 in the cartoon"),
    "time with caption or card (%)": (45, 100, "about 60"),
    "full screen cards per minute": (1.0, 4.0, "about 1.7"),
    "sound effects per minute": (8, 30, "about 15"),
    "music under the voice (%)": (90, 100, "97"),
    "loudness (LUFS)": (-15.5, -12.5, "YouTube plays at -14"),
    "timing error (s)": (0.0, 0.15, "card on the spoken word"),
    "longest still moment (s)": (0.0, 2.0, "huashu: a still picture longer than 2 s feels like a slideshow"),
}
REFS = Path("assets/reference")


def frames(video):
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(video), "-vf", "fps=6,scale=96:54,format=gray", "-f",
                          "rawvideo", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, 54, 96).astype(float)


def loudness(video):
    err = subprocess.run([FFMPEG, "-i", str(video), "-af", "ebur128", "-f", "null", "-"], capture_output=True,
                         text=True).stderr
    found = re.findall(r"I:\s+(-?[\d.]+) LUFS", err)
    return float(found[-1]) if found else 0.0


def timing(chapters):
    """How far each caption and card landed from its word (make.py keeps the word time with every element)."""
    off = []
    for shots, _ in chapters:
        t = 0.0
        for s in shots:
            extra = s[3] if len(s) > 3 else {}
            if "card_t" in extra:
                off.append(abs(t - extra["card_t"]))
            if extra.get("caption") and "_t" in extra["caption"]:
                off.append(abs(t + extra["caption"].get("at", 0) - extra["caption"]["_t"]))
            t += s[1]
    return float(np.mean(off)) if off else 0.0


def motion(f):
    """Share of moving frames, cuts per minute and the longest stretch where nothing moves, from 6 fps thumbnails."""
    d = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))
    still, run, longest = d < 0.12, 0, 0  # a frozen frame differs only by compression noise (under 0.1)
    for x in still:
        run = run + 1 if x else 0
        longest = max(longest, run)
    # a cut is one sharp jump; a card flying in changes many frames in a row, so it is not counted
    pad = np.pad(d, 1)
    cuts = ((d >= 15) & (pad[:-2] < d * 0.5) & (pad[2:] < d * 0.5)).sum()
    return {"moving": float((d[d < 22] >= 1.0).mean() * 100), "cuts": float(cuts / (len(f) / 6 / 60)),
            "still": longest / 6, "seconds": len(f) / 6}


def side_by_side(folder):
    """Eight frames of our video next to eight frames of every reference video, and the same numbers for each."""
    videos = [("this video", folder / "final.mp4")] + [(p.stem, p) for p in sorted(REFS.glob("*.mp4"))]
    if len(videos) == 1:
        return []
    rows, tw, th = [], 320, 180
    sheet = Image.new("RGB", (220 + tw * 8, th * len(videos)), "white")
    font = ImageFont.load_default(size=18)
    for r, (name, path) in enumerate(videos):
        cache = path.with_suffix(".qa.json")
        if path.parent == REFS and cache.exists():
            m = json.loads(cache.read_text())
        else:
            m = motion(frames(path))
            if path.parent == REFS:
                cache.write_text(json.dumps(m))
        rows.append(f"| {name} | {m['cuts']:.1f} | {m['moving']:.0f} | {m['still']:.1f} |")
        raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-vf",
                              f"fps={8 / max(m['seconds'], 1):.5f},scale={tw}:{th}", "-frames:v", "8",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
        for k in range(len(raw) // (tw * th * 3)):
            sheet.paste(Image.frombytes("RGB", (tw, th), raw[k * tw * th * 3:(k + 1) * tw * th * 3]), (220 + k * tw, r * th))
        ImageDraw.Draw(sheet).multiline_text((10, r * th + 20), f"{name[:20]}\n\ncuts/min {m['cuts']:.1f}\n"
                                             f"moving {m['moving']:.0f}%\nstill {m['still']:.1f} s", fill="black", font=font)
    sheet.save(folder / "side_by_side.jpg", quality=88)
    return ["## Side by side (measured the same way, see side_by_side.jpg)", "",
            "| video | cuts per minute | frames with movement (%) | longest still moment (s) |", "|---|---|---|---|", *rows, ""]


def report(folder, chapters, total, laps=None):
    """chapters: [(shots, (events, sections, scenes))] as make.py built them; laps: seconds per step."""
    video = folder / "final.mp4"
    m = motion(frames(video))
    shots = [s for ch, _ in chapters for s in ch]
    lens = [s[1] for s in shots]
    covered = sum(s[1] for s in shots if s[0].suffix == ".json" or (len(s) > 3 and s[3].get("caption")))
    cards = sum(1 for s in shots if s[0].suffix == ".json")
    events = sum(len(m[0]) for _, m in chapters)
    music = sum(end - start for _, m in chapters for start, end, *_ in m[1])
    minutes = total / 60
    got = {
        "cuts per minute": len(shots) / minutes,
        "median shot (s)": float(np.median(lens)),
        "frames with movement (%)": m["moving"],
        "time with caption or card (%)": covered / total * 100,
        "full screen cards per minute": cards / minutes,
        "sound effects per minute": events / minutes,
        "music under the voice (%)": min(100.0, music / total * 100),
        "loudness (LUFS)": loudness(video),
        "timing error (s)": timing(chapters),
        "longest still moment (s)": m["still"],
    }
    rows = ["| check | this video | target | reference |", "|---|---|---|---|"]
    passed = 0
    for name, (lo, hi, ref) in TARGETS.items():
        ok = lo <= got[name] <= hi
        passed += ok
        rows.append(f"| {name} | {got[name]:.2f} {'ok' if ok else 'FIX'} | {lo} to {hi} | {ref} |")
    steps = ["## Time per step", "", "| step | minutes | minutes per minute of video |", "|---|---|---|"]
    steps += [f"| {k} | {v / 60:.1f} | {v / 60 / max(minutes, 0.01):.1f} |" for k, v in (laps or {}).items()] + [""]
    text = "\n".join([f"# Quality check: {folder.name}", "", f"{passed} of {len(TARGETS)} checks pass", "", *rows, "",
                      *(steps if laps else []), *side_by_side(folder)])
    (folder / "qa.md").write_text(text, encoding="utf-8")
    print(f"  quality: {passed} of {len(TARGETS)} checks pass, see {folder / 'qa.md'}")
    return got


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip())
    if sys.argv[1] == "compare":
        print("\n".join(side_by_side(Path(sys.argv[2]))) or f"put reference videos (.mp4) in {REFS} first")
        sys.exit()
    folder = Path(sys.argv[1])
    print((folder / "qa.md").read_text(encoding="utf-8") if (folder / "qa.md").exists() else "build the video first")
