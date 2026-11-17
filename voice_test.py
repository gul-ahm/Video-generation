import json
import os
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

os.chdir(Path(__file__).parent)
load_dotenv() or load_dotenv(".env.example")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import voice

TEXT = """August, 1590. A small boat pushes through the mist toward a quiet island.
John White has waited three long years for this moment. His daughter is here. So is his granddaughter.
His men sound a trumpet. They sing old English songs into the trees. No one answers.
The houses are gone... and on one wooden post, someone has carved a single word. Croatoan.
So where did one hundred and fifteen people go?"""

VOICES = [
    ("edge", "en-US-AndrewMultilingualNeural", True),
    ("edge", "en-US-AndrewMultilingualNeural", False),
    ("edge", "en-US-BrianMultilingualNeural", True),
    ("edge", "en-US-ChristopherNeural", True),
    ("edge", "en-US-GuyNeural", True),
    ("edge", "en-GB-RyanNeural", True),
    ("edge", "en-GB-ThomasNeural", True),
    ("kokoro", "am_michael", True),
    ("kokoro", "am_fenrir", True),
    ("kokoro", "bm_george", True),
    ("kokoro", "bm_fable", True),
    ("chatterbox", "default", True),
    ("turbo", "default", True),
    *[(e, str(p), True) for p in Path("assets").glob("my_voice.*") if not p.name.endswith(".ref.wav")
      for e in ("chatterbox", "turbo")],
]


def measure(path):
    import parselmouth

    snd = parselmouth.Sound(str(path))
    f0 = snd.to_pitch(time_step=0.01, pitch_floor=60, pitch_ceiling=350).selected_array["frequency"]
    semis = 12 * np.log2(f0[f0 > 0] / np.median(f0[f0 > 0]))
    db = snd.to_intensity(minimum_pitch=60).values[0]
    talk = db > db.max() - 30
    quiet = np.convolve(f0 == 0, np.ones(30), "same") >= 30
    cuts = np.flatnonzero(np.diff(quiet.astype(int)) == -1)
    levels = [semis_of.mean() for p in np.split(f0, cuts) if (semis_of := 12 * np.log2(p[p > 0] / np.median(f0[f0 > 0]))).size > 20]
    return {
        "pitch_sd": round(float(semis.std()), 2),
        "phrase_sd": round(float(np.std(levels)), 2),
        "pitch_range": round(float(np.percentile(semis, 95) - np.percentile(semis, 5)), 1),
        "loud_sd": round(float(db[talk].std()), 1),
        "wpm": round(len(TEXT.split()) / snd.duration * 60),
        "pause_pct": round(100 * (1 - float(talk.mean()))),
    }


if __name__ == "__main__":
    out = Path("output/voice_test")
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    stats = out / "speed.json"
    speed = json.loads(stats.read_text()) if stats.exists() else {}
    for engine, name, vary in VOICES:
        label = f"{engine}_{Path(name).stem}" if engine in ("chatterbox", "turbo") else f"{name}{'' if vary else '_flat'}"
        wav = out / f"{label}.wav"
        try:
            t0 = time.time()
            if not wav.exists():
                voice.speak(TEXT, wav, engine=engine, voice=name, vary=vary)
                speed[label] = round(voice.length(wav) / (time.time() - t0), 2)
            rows.append({"voice": label, "engine": engine, **measure(wav), "realtime": speed.get(label, "")})
            print(f"  {label}: {rows[-1]}")
        except Exception as e:
            print(f"  {label}: skipped ({type(e).__name__}: {str(e)[:90]})")

    stats.write_text(json.dumps(speed))
    rows.sort(key=lambda r: -r["phrase_sd"])
    verdict = lambda r: ("lively" if r["pitch_sd"] >= 4 and r["phrase_sd"] >= 1.6 else
                         "flat" if r["pitch_sd"] < 3 or r["phrase_sd"] < 1.0 else "okay")
    lines = ["# Voice test", "",
             "pitch_sd: how much the pitch moves inside the speech, in semitones.",
             "phrase_sd: how much each phrase starts higher or lower than the last. Low means every sentence has the same tune, the main sign of a flat voice.",
             "wpm: words per minute with pauses. 130 to 160 suits documentary narration.",
             "realtime: seconds of speech made per second of work. Above 1 is faster than real time.", "",
             "| Voice | Engine | Pitch SD | Phrase SD | Range | Loudness SD | WPM | Pause % | Realtime | Verdict |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    lines += [f"| {r['voice']} | {r['engine']} | {r['pitch_sd']} | {r['phrase_sd']} | {r['pitch_range']} | {r['loud_sd']} | "
              f"{r['wpm']} | {r['pause_pct']} | {r['realtime']} | {verdict(r)} |" for r in rows]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
