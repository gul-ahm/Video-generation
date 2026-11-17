import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

from render import FFMPEG

SR = 24000
_kokoro = _chatter = None
ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def two(n):
    return ONES[n] if n < 20 else TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])


def years(text):
    """Say 1590 as 'fifteen ninety', the way a narrator would."""
    def say(m):
        hi, lo = divmod(int(m.group()), 100)
        return two(hi) + (" hundred" if lo == 0 else f" oh {ONES[lo]}" if lo < 10 else " " + two(lo))
    return re.sub(r"\b1[1-9]\d\d\b", say, text)


def prosody(par, i):
    """Rate and pitch per paragraph, so the read never sits on one level."""
    base = int(os.getenv("VOICE_RATE", "-3%").rstrip("%"))
    sents = [s for s in re.split(r"(?<=[.!?])\s+", par) if s.strip()]
    avg = len(par.split()) / max(1, len(sents))
    rate, pitch = base, 0
    if avg < 8:
        rate, pitch = base - 7, -3
    elif avg > 17:
        rate = base + 5
    if par.endswith(("?", "...", "…")):
        rate, pitch = rate - 3, pitch + 2
    return rate + i * 7 % 5 - 2, pitch


async def edge_one(text, rate, pitch, voice):
    import edge_tts

    com = edge_tts.Communicate(text, voice, rate=f"{rate:+d}%", pitch=f"{pitch:+d}Hz", boundary="WordBoundary")
    mp3, words = bytearray(), []
    async for c in com.stream():
        if c["type"] == "audio":
            mp3 += c["data"]
        elif c["type"] == "WordBoundary":
            words.append((c["offset"] / 1e7, (c["offset"] + c["duration"]) / 1e7, c["text"]))
    pcm = await asyncio.to_thread(subprocess.run, [FFMPEG, "-loglevel", "error", "-i", "pipe:0", "-f", "s16le",
                                                  "-ar", str(SR), "-ac", "1", "pipe:1"],
                                  input=bytes(mp3), capture_output=True, check=True)
    return pcm.stdout, words


async def edge_all(pars, settings, voice):
    sem = asyncio.Semaphore(4)

    async def one(p, s):
        async with sem:
            for k in range(5):
                try:
                    return await edge_one(p, *s, voice)
                except Exception:
                    if k == 4:
                        raise
                    await asyncio.sleep(3 * (k + 1))

    return await asyncio.gather(*(one(p, s) for p, s in zip(pars, settings)))


def kokoro_one(text, rate, _, voice):
    global _kokoro
    import numpy as np
    from kokoro import KPipeline

    _kokoro = _kokoro or KPipeline(lang_code="b" if voice.startswith("b") else "a", repo_id="hexgrad/Kokoro-82M")
    chunks, words, t = [], [], 0.0
    for r in _kokoro(text, voice=voice, speed=1 + rate / 100, split_pattern=r"(?<=[.!?])\s+"):
        audio = np.asarray(r.audio, dtype=np.float32)
        words += [(t + k.start_ts, t + k.end_ts, k.text) for k in (r.tokens or []) if k.start_ts is not None]
        chunks.append((audio * 32767).clip(-32768, 32767).astype(np.int16).tobytes())
        t += len(audio) / SR
    return b"".join(chunks), words


def reference(path):
    """Any recording (m4a, mp3, wav) becomes a clean 15 second mono clip for voice cloning."""
    out = os.path.splitext(path)[0] + ".ref.wav"
    if not os.path.exists(out):
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", path, "-t", "15", "-ac", "1", "-ar", "24000",
                        "-af", "silenceremove=1:0:-45dB,highpass=f=70,loudnorm=I=-18", out], check=True)
    return out


def chatterbox_model(turbo):
    """On a Mac both models run through MLX on the Apple GPU (CHATTER_BACKEND=mlx, the default). The full model can
    also run fully in MLX (pure) or in plain PyTorch on the CPU (torch, the slow old way)."""
    from visuals import device
    backend = os.getenv("CHATTER_BACKEND", "mlx") if device() == "mps" else "torch"
    dev = "cpu" if device() == "mps" else device()
    if turbo and backend != "torch" and importlib.util.find_spec("chatterbox.tts_turbo_mlx"):
        from chatterbox.tts_turbo_mlx import ChatterboxTurboMLX
        return ChatterboxTurboMLX.from_pretrained()
    if turbo:
        from chatterbox.tts_turbo import ChatterboxTurboTTS
        return ChatterboxTurboTTS.from_pretrained(device=dev)
    if backend != "torch" and importlib.util.find_spec("chatterbox.tts_mlx"):
        from chatterbox.tts_mlx import ChatterboxTTSMLX, ChatterboxTTSPureMLX
        return ChatterboxTTSPureMLX.from_pretrained() if backend == "pure" else ChatterboxTTSMLX.from_pretrained()
    from chatterbox.tts import ChatterboxTTS
    return ChatterboxTTS.from_pretrained(device=dev)


def chatterbox_one(text, _rate, _pitch, voice, turbo=False):
    """Sentence by sentence, so every line can be checked and redone if the model rambles or cuts off."""
    global _chatter
    import numpy as np
    import torch
    import torchaudio.functional as AF

    kind = ("turbo" if turbo else "full") + os.getenv("CHATTER_BACKEND", "mlx")
    if _chatter is None or _chatter[0] != kind:
        _chatter = [kind, chatterbox_model(turbo), None]
    _, model, loaded = _chatter
    sample = reference(voice) if os.path.exists(voice) else None
    if sample and sample != loaded:
        model.prepare_conditionals(sample)
        _chatter[2] = sample
    if getattr(model, "conds", True) is None:
        raise RuntimeError("this voice model needs a voice clip, set VOICE=assets/my_voice.m4a in .env")
    mlx = not turbo and type(model).__module__.endswith("_mlx")
    lively = float(os.getenv("EXAGGERATION", "0.6")) + (0.1 if text.endswith(("?", "...", "…")) else 0)
    chunks, words, t = [], [], 0.0
    for sent in [x for x in re.split(r"(?<=[.!?])\s+", text) if x.strip()]:
        n = len(sent.split())
        for attempt in range(3):
            if turbo:
                wav = model.generate(years(sent))
            else:
                opts = {"show_progress": False} if mlx else {}
                with contextlib.redirect_stdout(io.StringIO()) if mlx else contextlib.nullcontext():
                    wav = model.generate(years(sent), exaggeration=lively,
                                         cfg_weight=float(os.getenv("CFG_WEIGHT", "0.45")), **opts)
            wav = torch.as_tensor(wav).reshape(1, -1)
            wav = AF.resample(wav, model.sr, SR)[0].numpy() if model.sr != SR else wav[0].numpy()
            loud = np.flatnonzero(np.abs(wav) > 0.02 * np.abs(wav).max())
            wav = wav[max(0, loud[0] - 960): loud[-1] + 2400] if loud.size else wav
            if 0.18 * n < len(wav) / SR < 0.75 * n + 1.5:
                break
        dur = len(wav) / SR
        weights = np.cumsum([0] + [len(w) + 1 for w in sent.split()])
        words += [(t + dur * a / weights[-1], t + dur * b / weights[-1], w)
                  for w, a, b in zip(sent.split(), weights, weights[1:])]
        gap = float(os.getenv("SENTENCE_GAP", "0.15"))
        chunks.append((wav * 32767).clip(-32768, 32767).astype(np.int16).tobytes() + b"\0\0" * int(gap * SR))
        t += dur + gap
    speed = float(os.getenv("SPEED", "1.0"))
    pcm = subprocess.run([FFMPEG, "-loglevel", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "pipe:0",
                          "-af", f"atempo={speed}", "-f", "s16le", "pipe:1"],
                         input=b"".join(chunks), capture_output=True, check=True).stdout
    return pcm, [(a / speed, b / speed, w) for a, b, w in words]


def speak(text, out, engine=None, voice=None, vary=True):
    engine = engine or os.getenv("TTS", "edge")
    voice = voice or os.getenv("VOICE") or ("default" if engine in ("chatterbox", "turbo") else "en-US-AndrewMultilingualNeural")
    if engine == "turbo" and voice == "default" and os.path.exists("assets/narrator.wav"):
        voice = "assets/narrator.wav"
    if engine in ("chatterbox", "turbo") and not importlib.util.find_spec("chatterbox"):
        print("  chatterbox is not installed, run: python start.py --chatterbox. Using the Edge voice for now.")
        engine, voice = "edge", "en-US-AndrewMultilingualNeural"
    pars = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    settings = [prosody(p, i) if vary else (int(os.getenv("VOICE_RATE", "-3%").rstrip("%")), 0) for i, p in enumerate(pars)]
    if engine == "edge":
        results = asyncio.run(edge_all(pars, settings, voice))
    else:
        one = kokoro_one if engine == "kokoro" else chatterbox_one
        extra = {"turbo": True} if engine == "turbo" else {}
        results = [one(p, *s, voice, **extra) for p, s in zip(pars, settings)]

    buf, words, t = bytearray(), [], 0.0
    for p, (pcm, w) in zip(pars, results):
        words += [(t + a, t + b, x) for a, b, x in w]
        gap = float(os.getenv("PARAGRAPH_GAP", "0.45")) * (1.6 if p.endswith(("?", "...", "…")) else 1)
        buf += pcm + b"\0\0" * int(gap * SR)
        t += len(pcm) / 2 / SR + gap
    with wave.open(str(out), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(bytes(buf))
    return words


def length(path):
    with wave.open(str(path)) as f:
        return f.getnframes() / f.getframerate()


def narrate(chapters, folder):
    """Chatterbox runs in its own process. When it ends, every byte of GPU memory it used is free again for the
    picture model; inside one process MLX keeps about 15 GB cached and the pictures run out of memory."""
    d = folder / "audio"
    d.mkdir(exist_ok=True)
    setup = "".join(os.getenv(k, "") for k in ("TTS", "VOICE", "VOICE_RATE", "EXAGGERATION", "CFG_WEIGHT", "SPEED",
                                          "SENTENCE_GAP", "PARAGRAPH_GAP"))
    files = []
    for i, ch in enumerate(chapters):
        key = hashlib.md5((setup + ch["text"]).encode()).hexdigest()[:8]
        files.append([str(d / f"{i:02d}_{key}{x}") for x in (".wav", ".json", ".words.json")])
    todo = [(i, ch["text"], *f) for i, (ch, f) in enumerate(zip(chapters, files)) if not os.path.exists(f[2])]
    if todo and os.getenv("TTS") in ("chatterbox", "turbo"):
        # one process per chapter: the MPS memory chatterbox holds grows with every chapter (a long video ran out
        # of it on chapter 8), and only ending the process gives it all back
        for job in todo:
            (d / "todo.json").write_text(json.dumps([len(chapters), [job]]), encoding="utf-8")
            subprocess.run([sys.executable, "-u", __file__, str(d / "todo.json")], check=True)
    else:
        make(len(chapters), todo)
    return [(Path(a), length(a), json.loads(Path(c).read_text(encoding="utf-8"))) for a, _, c in files]


def make(total, todo):
    """Voice for each chapter (<n>.wav and the guessed word times <n>.json), then the exact word times
    (<n>.words.json). Chatterbox guesses times inside a sentence, so align.py fixes them; Edge and Kokoro report
    real word times already."""
    import align
    for i, text, audio, cues, final in todo:
        if not os.path.exists(cues):
            t = time.time()
            print(f"  voice {i + 1}/{total}", flush=True)
            Path(cues).write_text(json.dumps(speak(text, audio)), encoding="utf-8")
            print(f"  voice {i + 1}/{total} took {(time.time() - t) / 60:.1f} min for {length(audio) / 60:.1f} min of "
                  "speech", flush=True)
        words = json.loads(Path(cues).read_text(encoding="utf-8"))
        if os.getenv("TTS") in ("chatterbox", "turbo") and os.getenv("ALIGN", "1") == "1":
            words = align.refine(audio, words)
        Path(final).write_text(json.dumps(words), encoding="utf-8")

if __name__ == "__main__":
    make(*json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
