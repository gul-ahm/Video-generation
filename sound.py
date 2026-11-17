"""Sound design for one chapter: a music bed that follows the mood of the story, a sound on every on screen event,
scene ambience, all mixed under the narration.

Library first: assets/music/<mood>/*.mp3 and assets/sfx/<name>/*.wav (any audio ffmpeg reads). What is missing is
synthesised, so a video never goes out silent: effects and ambience sound fine synthesised, music is a soft pad and
pulse, so real tracks are better (YouTube Audio Library, sorted into the mood folders).

Levels follow hyperframes' MOTION_PHILOSOPHY: voice on top, music about 10 dB under it while someone speaks and up
again in the pauses, effects quiet and short.
"""
import hashlib
import os
import subprocess
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt

from render import FFMPEG

SR = 48000
MOODS = ("calm", "mystery", "tension", "sad", "wonder", "triumph")
AMBIENCE = ("crowd", "wind", "fire", "water", "church", "market", "battle", "night", "rain", "forest")
CHORDS = {  # semitones from A2 for each chord of a four chord loop, and beats per minute
    "calm": ([0, 7, 12, 16], [5, 12, 17, 21], [7, 14, 19, 23], [3, 10, 15, 19], 70),
    "mystery": ([0, 7, 12, 15], [1, 8, 13, 16], [-2, 5, 10, 13], [0, 7, 12, 15], 80),
    "tension": ([0, 7, 12, 15], [0, 6, 12, 15], [-1, 6, 11, 14], [0, 7, 12, 15], 100),
    "sad": ([0, 7, 12, 15], [-4, 3, 8, 12], [-7, 0, 5, 8], [-5, 2, 7, 10], 66),
    "wonder": ([0, 7, 12, 16], [4, 11, 16, 19], [5, 12, 17, 21], [2, 9, 14, 17], 76),
    "triumph": ([0, 7, 12, 16], [5, 12, 17, 21], [7, 14, 19, 23], [0, 7, 12, 16], 96),
}
GAIN = {"pageturn": 0.3, "whip": 0.2, "shutter": 0.2, "whoosh": 0.22, "pop": 0.16, "hit": 0.35, "paper": 0.2, "pin": 0.14, "swell": 0.25, "sparkle": 0.2, "tick": 0.12}


def db(x):
    return 10 ** (x / 20)


def band(x, lo=None, hi=None, order=2):
    if lo and hi:
        return sosfilt(butter(order, [lo, hi], "band", fs=SR, output="sos"), x)
    return sosfilt(butter(order, lo or hi, "high" if lo else "low", fs=SR, output="sos"), x)


def load(path, seconds=None):
    cmd = [FFMPEG, "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    a = np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, np.float32).copy()
    return a[:round(seconds * SR)] if seconds else a


def number(seed):
    return int(hashlib.md5(str(seed).encode()).hexdigest()[:8], 16)


def pick(folder, seed):
    files = sorted(p for p in Path(folder).glob("*") if p.suffix.lower() in (".wav", ".mp3", ".m4a", ".ogg", ".flac"))
    return files[number(seed) % len(files)] if files else None


def synth(name, seed=0):
    rng = np.random.default_rng(seed)
    t = lambda s: np.arange(round(s * SR)) / SR
    if name == "whoosh":
        x = t(0.55)
        env = np.minimum(x / 0.3, 1) * np.exp(-np.maximum(x - 0.3, 0) * 12)
        return band(rng.normal(size=x.size), 300, 3500) * env
    if name == "pop":
        x = t(0.09)
        return np.sin(2 * np.pi * np.cumsum(np.linspace(900, 280, x.size)) / SR) * np.exp(-x * 45)
    if name == "hit":
        x = t(1.4)
        boom = np.sin(2 * np.pi * 52 * x) * np.exp(-x * 3.2)
        return np.tanh(1.6 * (boom + band(rng.normal(size=x.size), hi=900) * np.exp(-x * 18) * 0.6))
    if name == "paper":
        x = t(0.5)
        bursts = sum(np.exp(-np.maximum(x - s, 0) * 40) * (x >= s) for s in (0.0, 0.12, 0.27))
        return band(rng.normal(size=x.size), 1500, 7000) * bursts
    if name == "pin":
        x = t(0.18)
        return (np.sin(2 * np.pi * 1760 * x) + 0.6 * np.sin(2 * np.pi * 2640 * x)) * np.exp(-x * 28)
    if name == "swell":
        x = t(1.3)
        return band(rng.normal(size=x.size), lo=2500) * (x / x[-1]) ** 2.5
    if name == "sparkle":
        x = t(1.0)
        return sum(np.sin(2 * np.pi * f * x) * np.exp(-np.maximum(x - d, 0) * 6) * (x >= d)
                   for f, d in zip(rng.uniform(2200, 4200, 7), np.linspace(0, 0.35, 7))) / 4
    if name == "tick":
        x = t(0.03)
        return rng.normal(size=x.size) * np.exp(-x * 300)
    x = t(12)  # ambience loops
    if name == "wind":
        return band(rng.normal(size=x.size), 150, 900) * (0.6 + 0.4 * np.sin(2 * np.pi * x / 5.3)) * 0.6
    if name == "rain":
        return band(rng.normal(size=x.size), lo=1800) * 0.25 + band(rng.normal(size=x.size) * (rng.random(x.size) > 0.9995), lo=2500)
    if name == "fire":
        crackle = rng.normal(size=x.size) * (rng.random(x.size) > 0.9993) * 3
        return band(rng.normal(size=x.size), hi=400) * 0.4 + band(crackle, lo=1200)
    if name == "water":
        return band(rng.normal(size=x.size), 200, 1200) * (0.7 + 0.3 * np.sin(2 * np.pi * x / 2.1)) * 0.5
    if name == "night":
        chirp = (np.sin(2 * np.pi * 4300 * x) * ((x * 14) % 1 < 0.35) * ((x / 0.9) % 1 < 0.4))
        return band(rng.normal(size=x.size), hi=300) * 0.1 + chirp * 0.15
    return np.zeros(x.size)


LENGTH = {"pageturn": 1.2, "whip": 0.8, "shutter": 1.0, "whoosh": 1.5, "pop": 0.6, "hit": 3.0, "paper": 1.5, "pin": 0.8, "swell": 3.0, "sparkle": 2.0, "tick": 2.0}


STANDIN = {"pageturn": "paper", "whip": "whoosh", "shutter": "hit"}


def effect(name, seed):
    """One effect from assets/sfx/<name>/ (or synthesised). Downloaded files often start with silence and run long,
    so the silence is cut (the sound must start on its cue) and the file stops at LENGTH with a short fade."""
    f = pick(Path("assets/sfx") / name, seed)
    if not f and name in STANDIN:  # the transition sounds of fx.py borrow a near one until their folder has files
        return effect(STANDIN[name], seed)
    if not f:
        a = synth(name, number(seed) % 1000)
        return a / (np.abs(a).max() or 1)
    a = load(f, 30)
    loud = np.flatnonzero(np.abs(a) > 0.03 * (np.abs(a).max() or 1))
    a = a[loud[0]:] if loud.size else a
    a = a[:round(LENGTH.get(name, 5.0) * SR)].copy()  # moments (a scream, a heartbeat) may run 5 s
    tail = min(a.size, round(0.25 * SR))
    a[a.size - tail:] *= np.linspace(1, 0, tail)
    return a / (np.abs(a).max() or 1)


def loop(a, seconds, overlap=1.0):
    """a repeated to fill seconds, each repeat crossfading into the next so there is no click at the seam."""
    n, o = round(seconds * SR), min(round(overlap * SR), a.size // 3)
    if a.size >= n or o == 0:
        return np.tile(a, n // max(a.size, 1) + 1)[:n]
    out, pos, fade = np.zeros(n + a.size), 0, np.linspace(0, 1, o)
    while pos < n:
        piece = a.copy()
        if pos:
            piece[:o] *= fade
        piece[-o:] *= fade[::-1]
        out[pos:pos + a.size] += piece
        pos += a.size - o
    return out[:n]


def ambience(name, seconds, seed):
    """The sound of a place for a whole scene. A file under 10 s (one church bell, one battle cry) plays once at the
    start of the scene; a longer one loops with crossfades."""
    f = pick(Path("assets/sfx") / name, seed)
    a = load(f) if f else synth(name)
    if not a.any():
        return np.zeros(round(seconds * SR))
    a = a / (np.abs(a).max() or 1)
    if f and a.size < 10 * SR:
        return np.pad(a, (0, max(0, round(seconds * SR) - a.size)))[:round(seconds * SR)]
    return loop(a, seconds)


def music(mood, seconds, seed):
    """A track from the mood folder, looped, or a synthesised pad with a soft pulse. Returns (audio, beat times)."""
    f = pick(Path("assets/music") / mood, seed)
    if f:
        a = loop(load(f), seconds, overlap=2.0)
        return a / (np.abs(a).max() or 1), beats(a)
    *chords, bpm = CHORDS.get(mood, CHORDS["calm"])
    beat, n = 60 / bpm, round(seconds * SR)
    x = np.arange(n) / SR
    out = np.zeros(n)
    bar = 4 * beat
    for k, chord in enumerate(chords * (int(seconds / bar / 4) + 1)):
        a, b = round(k * bar * SR), min(n, round((k + 1) * bar * SR + 0.5 * SR))
        if a >= n:
            break
        seg = x[: b - a]
        env = np.minimum(seg / 0.8, 1) * np.minimum(1, (seg[-1] - seg + 1e-3) / 0.5)
        out[a:b] += sum(np.sin(2 * np.pi * 110 * 2 ** (s / 12) * seg) * (0.5 if i else 1) for i, s in enumerate(chord)) * env
    out = band(out, hi=1800)
    pulse = np.zeros(n)
    for b0 in np.arange(0, seconds, beat):
        i = round(b0 * SR)
        k = x[: min(n - i, round(0.25 * SR))]
        pulse[i:i + k.size] += np.sin(2 * np.pi * 60 * k) * np.exp(-k * 14) * (0.8 if mood in ("tension", "triumph") else 0.35)
    out = out / (np.abs(out).max() or 1) * 0.8 + pulse
    return out / (np.abs(out).max() or 1), list(np.arange(0, seconds, beat))


def beats(a):
    """Beat times of a track from its onset strength (energy rises, autocorrelated to find the tempo)."""
    hop = 512
    env = np.sqrt(np.convolve(a ** 2, np.ones(hop) / hop, "same")[::hop])
    onset = np.maximum(np.diff(env, prepend=env[0]), 0)
    lags = np.arange(int(SR / hop * 60 / 160), int(SR / hop * 60 / 60))
    ac = [np.dot(onset[:-l], onset[l:]) for l in lags]
    period = lags[int(np.argmax(ac))] * hop / SR
    start = np.argmax(onset[: int(period * SR / hop) + 1]) * hop / SR
    return list(np.arange(start, a.size / SR, period))


def envelope(voice, attack=0.03, release=0.4):
    """How loud the voice is at every sample, smoothed like a compressor's side chain."""
    hop = 480
    rms = np.sqrt(np.convolve(voice ** 2, np.ones(hop) / hop, "same")[::hop])
    level = rms / (np.percentile(rms, 95) or 1)
    out, cur = np.zeros_like(level), 0.0
    up, down = np.exp(-hop / SR / attack), np.exp(-hop / SR / release)
    for i, v in enumerate(level):
        cur = v + (cur - v) * (up if v > cur else down)
        out[i] = cur
    return np.repeat(np.clip(out, 0, 1), hop)[:voice.size]


def plan(moods, dur, seed):
    """moods: [(start seconds, mood)] per sentence. Sections where the mood holds at least 15 s get their own music;
    returns [(start, end, mood, audio, beat times)] and every beat of the whole chapter."""
    sections = []
    for start, mood in moods or [(0.0, "calm")]:
        if sections and (mood == sections[-1][1] or start - sections[-1][0] < 15):
            continue
        sections.append([start, mood])
    sections[0][0] = 0.0
    out, grid = [], []
    for (start, mood), nxt in zip(sections, sections[1:] + [[dur, None]]):
        a, bts = music(mood, nxt[0] - start + 2, f"{seed}{mood}{start:.0f}")
        out.append((start, nxt[0], mood, a, bts))
        grid += [start + b for b in bts if start + b < nxt[0]]
    return out, grid


def mix(voice_wav, dur, events, sections, scenes, out):
    """voice_wav: the processed narration. events: [(time, effect name)]. sections: from plan().
    scenes: [(start, ambience name or None)]. Writes a stereo 48 kHz wav of exactly dur seconds."""
    n = round(dur * SR)
    voice = np.zeros(n)
    v = load(voice_wav)[:n]
    voice[:v.size] = v
    duck = envelope(voice)
    bed = np.zeros(n)
    fade = round(1.5 * SR)
    for k, (start, end, _, audio, _) in enumerate(sections):
        a, b = round(start * SR), min(n, round(end * SR) + fade)
        seg = audio[: b - a].copy()
        ramp = np.minimum(1, np.arange(seg.size) / fade)
        if k:
            seg *= ramp
        if end < dur:
            seg *= ramp[::-1]
        bed[a:a + seg.size] += seg
    # music sits about 10 dB under the voice and comes back up to its full level in the pauses; music, ambience and
    # effects are 10% (0.8 dB) above the first mix, asked for after the first videos
    bed *= db(float(os.getenv("MUSIC_DB", "-16.2"))) * (1 - 0.68 * duck)
    for t, name in events:  # a music drop: the bed falls silent for a breath before the reveal, then comes back
        if name == "drop":
            x = np.arange(n) / SR - t
            bed *= np.clip(np.where(x < 0, -x / 0.6 - 0.4, x / 1.2), 0.05, 1)
    amb = np.zeros(n)
    for (start, name), nxt in zip(scenes, scenes[1:] + [(dur, None)]):
        if name in AMBIENCE:
            a, b = round(start * SR), round(nxt[0] * SR)
            seg = ambience(name, (b - a) / SR, name)[: max(0, min(b, n) - a)]
            ramp = np.minimum(1, np.minimum(np.arange(seg.size), seg.size - np.arange(seg.size)) / (0.8 * SR))
            amb[a:a + seg.size] += seg * ramp
    amb *= db(float(os.getenv("AMBIENCE_DB", "-29.2")))
    fx = np.zeros(n)
    for k, (t, name) in enumerate(e for e in events if e[1] != "drop"):
        a = effect(name, f"{name}{k}") * GAIN.get(name, 0.3 if name.startswith("moments/") else 0.15)
        a *= db(float(os.getenv("SFX_DB", "0.8")))
        # a whoosh peaks on the cut, a swell (riser) ends on its moment
        lead = {"whoosh": 0.3, "swell": a.size / SR - 0.35}.get(name, 0)
        i = round(max(0, t - lead) * SR)
        if i < n:
            fx[i:i + a.size] += a[: n - i]
    total = voice + bed + amb + fx
    total = np.tanh(total * 1.1) / np.tanh(1.1)
    pcm = (np.repeat(total[:, None], 2, axis=1) * 32767).clip(-32768, 32767).astype("<i2")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return out
