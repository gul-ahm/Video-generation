import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import zlib
from concurrent.futures import ThreadPoolExecutor
from itertools import accumulate

from PIL import Image

import cards
import fx
import hf
import motion
from visuals import pose_files, size

FPS = 30
PAD = 0.8
FFMPEG = shutil.which("ffmpeg")
if not FFMPEG:
    import imageio_ffmpeg

    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

HW = "h264_videotoolbox" in subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
FAST = ["-c:v", "h264_videotoolbox", "-b:v", "40M"] if HW else ["-c:v", "libx264", "-preset", "ultrafast", "-crf", 16]
FINAL = ["-c:v", "h264_videotoolbox", "-b:v", "8M"] if HW else ["-c:v", "libx264", "-preset", "veryfast", "-crf", 20]

VOICE_CHAIN = ("highpass=f=70,equalizer=f=200:t=q:w=1:g=1.5,equalizer=f=3500:t=q:w=1.2:g=2,"
               "acompressor=threshold=-20dB:ratio=3:attack=8:release=150:makeup=2,loudnorm=I=-16:TP=-1.5:LRA=9")


def ffmpeg(*args, cwd=None):
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", *map(str, args)], check=True, cwd=cwd)


GRADE = ("eq=contrast=1.06:saturation=1.15:gamma=0.98,colorbalance=rs=-0.04:bs=0.04:rh=0.04:bh=-0.04,"
         "vignette=angle=PI/5,noise=alls=6:allf=t+u")


# the director's camera moves (writer.MOVES): (zoom, x, y) at the start and the end of the shot
MOVES = {"push": ((1.0, .5, .5), (1.2, .5, .45)), "pull": ((1.25, .5, .45), (1.0, .5, .5)),
         "pan-left": ((1.2, 1, .5), (1.2, 0, .5)), "pan-right": ((1.2, 0, .5), (1.2, 1, .5)),
         "rise": ((1.22, .5, 1), (1.22, .5, 0)), "dive": ((1.22, .5, 0), (1.22, .5, 1)),
         "drift": ((1.06, .48, .5), (1.1, .52, .48)), "shake": ((1.12, .5, .5), (1.2, .5, .5))}


def segment(src, frames, k, out, offset=0.0, extra=None):
    """One shot: a full screen card (.json), real footage, or a picture with a camera move and maybe a caption."""
    w, h = size()
    quality = FAST if HW else FAST + ["-threads", 2]
    if src.suffix == ".json":
        encode(cards.card_frames(json.loads(src.read_text(encoding="utf-8")), frames, w, h), out, quality)
        return out
    if src.suffix.lower() not in (".jpg", ".png"):
        slow = 1 / float(os.getenv("CLIP_SPEED", "0.8"))
        ffmpeg("-ss", f"{offset:.2f}", "-stream_loop", -1, "-i", src, "-an", "-vf",
               f"setpts={slow:.3f}*PTS,fps={FPS},scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
               "setsar=1,format=yuv420p", "-frames:v", frames, *quality, out)
        return out
    # (zoom, x, y) at the start and the end of the shot; zoom 1 shows the whole picture, x and y place the view
    moves = [((1.0, .5, .5), (1.18, .5, .42)), ((1.2, 0, .5), (1.2, 1, .5)), ((1.22, .5, .35), (1.0, .5, .5)),
             ((1.25, 1, .6), (1.25, 0, .4)), ((1.05, .5, .5), (1.3, .5, .3))]
    move = MOVES.get((extra or {}).get("move")) or moves[k % len(moves)]
    encode(glide([src, *pose_files(src)], frames, *move, extra), out, quality)
    return out


def encode(frames, out, quality):
    w, h = size()
    enc = subprocess.Popen([FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                            "-r", str(FPS), "-i", "-", *map(str, quality), "-pix_fmt", "yuv420p", str(out)],
                           stdin=subprocess.PIPE)
    for img in frames:
        enc.stdin.write(img.tobytes())
    enc.stdin.close()
    if enc.wait():
        raise RuntimeError(f"ffmpeg failed on {out}")


def glide(pictures, frames, start, end, extra=None):
    """Camera move with easing and sub pixel steps (ffmpeg's crop and scale move in whole pixels, so slow moves
    stutter). Movement frames take turns every POSE_SECONDS. extra: {"caption": spec, "air": kind, "intro": "sparkle"}
    adds a caption, drifting particles and a sparkle opening; a picture with a cut out character also breathes and
    gets parallax (motion.py)."""
    w, h = size()
    extra = extra or {}
    imgs = [Image.open(p).convert("RGB") for p in pictures]
    parts = [motion.layers(p) for p in pictures]
    hold = round(float(os.getenv("POSE_SECONDS", "0.6")) * FPS)
    iw, ih = imgs[0].size
    base = min(iw / w, ih / h)
    cap = extra.get("caption")
    tier2 = cap and cap.get("kind") == "hfo"  # a HyperFrames lower third or label, frames drawn by hf.py
    layer = cards.overlay(cap, w, h) if cap and not tier2 else None
    shown = round(cap.get("at", 0) * FPS) if cap else 0  # the caption appears on its word
    # a caption held over several shots goes on from where the shot before left it ("from") and leaves at "dur"
    skip = round(cap.get("from", 0) * FPS) if cap else 0
    span = round(cap["dur"] * FPS) if cap and cap.get("dur") else None
    air = motion.Air(extra["air"], w, h, zlib.crc32(str(pictures[0]).encode()) % 1000) if extra.get("air") else None
    for n in range(frames):
        t = n / max(frames - 1, 1)
        t = t * t * (3 - 2 * t)
        z, x, y = (a + (b - a) * t for a, b in zip(start, end))
        sc = base / z
        ox, oy = (iw - w * sc) * x, (ih - h * sc) * y
        if extra.get("move") == "shake":  # an impact: a hard jolt that settles within half a second
            kick = 0.012 * iw * math.exp(-5 * (n % (FPS * 2)) / FPS)
            ox, oy = ox + kick * math.sin(n * 2.1), oy + kick * math.cos(n * 1.7)
        k = min(n // hold, len(imgs) - 1)
        if parts[k]:
            img = motion.draw_layers(*parts[k], w, h, sc, ox, oy, n / FPS)
        else:
            img = imgs[k].transform((w, h), Image.AFFINE, (sc, 0, ox, 0, sc, oy), Image.BICUBIC)
        if air:
            air.draw(img, n / FPS)
        if extra.get("intro") == "sparkle":
            motion.sparkle(img, n / FPS)
        if tier2 and n >= shown:
            top = hf.frame(cap["frames"], n - shown + skip, (w, h), cap.get("step", 2))
            img.paste(top, (0, 0), top)
            yield img
        else:
            yield cards.draw_overlay(img, *layer, n - shown + skip, span or frames - shown) if layer and n >= shown else img


def captions(words, max_words):
    cues, cur = [], []
    for a, b, w in words:
        if cur and not re.search(r"\w", w):
            cur[-1] = (cur[-1][0], b, cur[-1][2] + w)
            continue
        if cur and (len(cur) >= max_words or a - cur[-1][1] > 0.35 or cur[-1][2].endswith((".", "?", "!"))):
            cues.append((cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)))
            cur = []
        cur.append((a, b, w))
    if cur:
        cues.append((cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)))
    return cues


def ass(words, out):
    w, h = size()
    t = lambda s: f"{int(s // 3600)}:{int(s // 60 % 60):02d}:{s % 60:05.2f}"
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "",
             "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV",
             f"Style: D,Arial,{h // 13},&H00FFFFFF,&H00000000,&H80000000,1,1,{h // 270},{h // 540},2,{w // 10},{w // 10},{h // 12}",
             "", "[Events]", "Format: Layer, Start, End, Style, Text"]
    lines += [f"Dialogue: 0,{t(a)},{t(b + 0.05)},D,{text}" for a, b, text in captions(words, 7)]
    out.write_text("\n".join(lines), encoding="utf-8")


def merge(segs, starts, fade, tmp, level=0):
    """Crossfade segments into one video, at most 8 inputs per ffmpeg call so a low RAM laptop keeps up."""
    if len(segs) == 1:
        return segs[0]
    merged, heads = [], []
    for g in range(0, len(segs), 8):
        part, at = segs[g:g + 8], starts[g:g + 8]
        graph, last = [], "0:v"
        for i in range(1, len(part)):
            graph.append(f"[{last}][{i}:v]xfade=transition=fade:duration={fade / FPS:.3f}:offset={(at[i] - at[0]) / FPS:.3f}[x{i}]")
            last = f"x{i}"
        out = tmp / f"m{level}_{g // 8:03d}.mp4"
        if len(part) == 1:
            out = part[0]
        else:
            ffmpeg(*[a for p in part for a in ("-i", p.name)], "-filter_complex", ";".join(graph), "-map", f"[{last}]",
                   *FAST, out.name, cwd=tmp)
        merged.append(out)
        heads.append(at[0])
    return merge(merged, heads, fade, tmp, level + 1)


def clean_voice(audio, out):
    """The narration through the voice chain (EQ, compressor, loudness), ready to mix."""
    ffmpeg("-i", audio, "-af", VOICE_CHAIN, "-ar", 48000, "-ac", 1, out)
    return out


def chapter(shots, audio, dur, words, out, mixed=False, hold=None, last=None):
    """Shots are (file, seconds, offset, extra). They join with hard cuts, Remotion transitions go in where the
    timeline asked for them (fx_open, fx_turn), HyperFrames titles and light leaks are laid over (fx_title,
    fx_leaks), then the whole chapter gets one colour grade so pictures and real footage feel like the same film.
    hold: the last frame of the previous chapter (the page flips away from it); last: where to keep this one's."""
    if out.exists():
        if last and not Path(last).exists():
            ffmpeg("-sseof", "-1.0", "-i", out, "-frames:v", 1, "-update", 1, last)
        return out
    tmp = out.with_suffix("")
    tmp.mkdir(exist_ok=True)
    # hard cuts like the reference (CROSSFADE=0): a crossfade between two framings of one picture shows a double image
    fade = round(float(os.getenv("CROSSFADE", "0")) * FPS)
    edges = [round(t * FPS) for t in [*accumulate((s[1] for s in shots[:-1]), initial=0), dur + PAD]]
    jobs = [(src, b - a + (fade if i < len(shots) - 1 else 0), i, tmp / f"{i:03d}.mp4", offset, *extra)
            for i, ((src, _, offset, *extra), a, b) in enumerate(zip(shots, edges, edges[1:]))]
    with ThreadPoolExecutor(min(4, max(1, (os.cpu_count() or 2) // 2))) as ex:
        segs = list(ex.map(lambda j: segment(*j), jobs))
    lead = shots[0][3] if len(shots[0]) > 3 else {}
    overlays = []
    if fade:
        video = merge(segs, edges[:-1], fade, tmp)
    else:  # segments are frame exact and encoded the same way, so they join without a new encode
        plan = [{"at": 0, "kinds": lead["fx_open"], "hold": str(hold)}] if lead.get("fx_open") and hold and Path(hold).exists() else []
        plan += [{"at": i, "kinds": s[3]["fx_turn"]} for i, s in enumerate(shots) if i and len(s) > 3 and s[3].get("fx_turn")]
        segs, used = fx.joins(segs, plan, tmp, FAST if HW else FAST + ["-threads", 2])
        missed = [edges[p["at"]] / FPS - 0.6 for p in plan if p["at"] and p["at"] not in used]
        if plan:
            print(f"  fx joins: {', '.join(used.values()) or 'none'}" + (f", {len(missed)} turned into light leaks" if missed else ""), flush=True)
        if lead.get("fx_title"):
            overlays.append((0.25, lead["fx_title"], 1.0))
        overlays += [(t, mov, 0.55) for t, mov in fx.leaks(sorted([*lead.get("fx_leaks", []), *missed]), tmp / "fx")]
        (tmp / "cuts.txt").write_text("".join(f"file '{os.path.relpath(p, tmp)}'\n" for p in segs))
        video = tmp / "cuts.mp4"
        ffmpeg("-f", "concat", "-safe", 0, "-i", "cuts.txt", "-c", "copy", video.name, cwd=tmp)
    if last:  # the next chapter's page flip starts from this frame, before the grade (it gets graded there)
        ffmpeg("-sseof", "-0.1", "-i", video.name, "-frames:v", 1, "-update", 1, Path(last).resolve(), cwd=tmp)

    look = [GRADE] if os.getenv("GRADE", "1") == "1" else []
    if os.getenv("BURN_SUBS", "0") == "1":
        ass(words, tmp / "subs.ass")
        look.append("subtitles=subs.ass")
    chain = "loudnorm=I=-14:TP=-1.5:LRA=11" if mixed else VOICE_CHAIN
    inputs, graph, top = fx.overlay_graph("0:v", overlays, 2)
    graph.append(f"[{top}]{','.join(look) or 'null'}[vout]")
    ffmpeg("-i", video.name, "-i", audio.resolve(), *inputs, "-filter_complex", ";".join(graph), "-map", "[vout]",
           "-map", "1:a", *FINAL, "-af", f"{chain},apad=whole_dur={edges[-1] / FPS:.3f}", "-t", f"{edges[-1] / FPS:.3f}",
           "-c:a", "aac", "-b:a", "192k", "-ar", 48000, "-ac", 2, out.resolve(), cwd=tmp)
    shutil.rmtree(tmp)
    return out


def join(clips, out):
    lst = out.parent / "chapters" / "list.txt"
    lst.write_text("".join(f"file '{c.name}'\n" for c in clips))
    music = os.getenv("MUSIC", "assets/music.mp3")
    if os.getenv("SOUND", "1") == "1" or not os.path.exists(music):  # sound.py already mixed music into chapters
        ffmpeg("-f", "concat", "-safe", 0, "-i", lst, "-c", "copy", "-movflags", "+faststart", out)
        return
    vol = os.getenv("MUSIC_VOLUME", "0.18")
    ffmpeg("-f", "concat", "-safe", 0, "-i", lst, "-stream_loop", -1, "-i", music, "-filter_complex",
           f"[0:a]asplit[v][key];[1:a]volume={vol}[m];[m][key]sidechaincompress=threshold=0.03:ratio=6:attack=30:release=600[duck];"
           "[v][duck]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5[a]",
           "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out)


def srt(chapter_words, starts, out):
    def ts(s):
        ms = round(s * 1000)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    lines, n = [], 0
    for words, start in zip(chapter_words, starts):
        for a, b, text in captions(words, 12):
            n += 1
            lines += [str(n), f"{ts(start + a)} --> {ts(start + b + 0.05)}", text, ""]
    out.write_text("\n".join(lines), encoding="utf-8")
