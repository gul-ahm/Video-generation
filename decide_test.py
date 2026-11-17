"""See what the decision model picks for a script before it drives the edit.

python decide_test.py                                         the dancing plague test script
python decide_test.py output/<video folder>                   any other script

Writes output/decide_test.md: every sentence with its pick, plus how many cards and turning points per minute.
"""
import os
import sys
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

os.chdir(Path(__file__).parent)
load_dotenv() or load_dotenv(".env.example")

import decide  # noqa: E402
from writer import read_script  # noqa: E402

folder = Path(sys.argv[1] if len(sys.argv) > 1 else "output/the-dancing-plague-of-1518")
script = read_script(folder / "script.md")
lines = [s for ch in script["chapters"] for s in decide.sentences(ch["text"])]

t = time.time()
picks = decide.decide(lines, folder / "decisions.json")
if picks is None:
    sys.exit("colibri is not running and could not be started. Follow README, \"Edit decisions\", then try again.")
spent = time.time() - t

minutes = sum(len(s.split()) for s in lines) / 150
cards = [p for p in picks if p["show"] != "scene" and p["sure"] >= 0.3]
turns = [p for p in picks if p["turn"] >= 0.7]
rows = [f"| {s[:90]} | {p['show']} | {p['sure']} | {p['turn']} | {p['mood']} |" for s, p in zip(lines, picks)]
report = "\n".join([f"# Edit decisions: {script['title']}", "",
                    f"{len(lines)} sentences, about {minutes:.1f} min of speech, asked in {spent:.0f}s", "",
                    f"proof cards: {len(cards)} ({len(cards) / minutes:.1f} per minute) {dict(Counter(p['show'] for p in cards))}",
                    f"turning points: {len(turns)} ({len(turns) / minutes:.1f} per minute)", "",
                    "| sentence | show | sure | turning point | tension 0-3 |", "|---|---|---|---|---|", *rows])
Path("output/decide_test.md").write_text(report, encoding="utf-8")
print("\n".join(report.splitlines()[:7]))
print(f"\nfull table: {Path('output/decide_test.md').resolve()}")
