"""Find the fastest way to run the full Chatterbox voice on this Mac without losing quality.

python voice_speed.py

Reads the voice_test text with assets/my_voice.wav through each backend, saves the audio in output/voice_speed
for listening, and prints speed, how lively it sounds, and how long the voice of a full video would take.
"""
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

os.chdir(Path(__file__).parent)
load_dotenv() or load_dotenv(".env.example")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import voice
import voice_test

VOICE = next((str(p) for p in Path("assets").glob("my_voice.*") if not p.name.endswith(".ref.wav")), "default")
out = Path("output/voice_speed")


def one(backend):
    """One backend per process, so memory numbers and the GPU start clean each time."""
    os.environ["CHATTER_BACKEND"] = backend
    wav = out / f"{backend}.wav"
    voice.speak("August, 1590.", out / "warmup.wav", engine="chatterbox", voice=VOICE)
    t = time.time()
    voice.speak(voice_test.TEXT, wav, engine="chatterbox", voice=VOICE)
    rt = round(voice.length(wav) / (time.time() - t), 2)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 ** 3 if sys.platform == "darwin" else 1024 ** 2)
    (out / f"{backend}.json").write_text(json.dumps([backend, rt, voice_test.measure(wav), f"{peak:.1f} GB"]))


if __name__ == "__main__":
    out.mkdir(parents=True, exist_ok=True)
    if sys.argv[1:2] == ["--one"]:
        one(sys.argv[2])
        sys.exit()
    rows = []
    old = Path("output/voice_test/chatterbox_my_voice.wav")
    if old.exists():
        speed = json.loads(Path("output/voice_test/speed.json").read_text()).get("chatterbox_my_voice", "")
        rows.append(["torch (old, CPU)", speed, voice_test.measure(old), ""])
    for backend in sys.argv[1:] or ["mlx", "pure"]:
        print(f"testing {backend} ...")
        code = subprocess.run([sys.executable, __file__, "--one", backend]).returncode
        res = out / f"{backend}.json"
        if code == 0 and res.exists():
            rows.append(json.loads(res.read_text()))
        else:
            print(f"  {backend} failed, see the error above")

    minutes = int(os.getenv("MINUTES", "60"))
    print(f"\n{'backend':18} {'realtime':>8} {'pitch_sd':>8} {'phrase_sd':>9} {'wpm':>5} {'memory':>8}   voice for {minutes} min video")
    for name, rt, m, mem in rows:
        hours = f"{minutes / rt / 60:.1f} h" if rt else ""
        print(f"{name:18} {rt:>8} {m['pitch_sd']:>8} {m['phrase_sd']:>9} {m['wpm']:>5} {mem:>8}   {hours}")
    print(f"\nListen to the files in {out.resolve()}. The presenter scored pitch_sd 4.2, phrase_sd 2.2, about 165 wpm.")
    print("Pick the fastest one that still sounds like the old file, then set CHATTER_BACKEND to it in .env.example.")
