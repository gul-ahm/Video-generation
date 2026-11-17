"""Documentary maker: give a title or a headline, get a researched 20 to 25 minute film.

    python documentary/doc.py "The title or headline" [minutes] ["the event it is about"]

1. research.py   the open web is searched from about 40 angles in two rounds plus the Wikipedia references, up to
                 150 sources are read in full and every claim is checked against the source's own words and
                 counted across sites (research/, sources.md)
2. assets.py     a screenshot of every article at its headline, shown in the film as a source card
3. write.py      the script, written only from the checked claims, at the voice's real pace
4. make.py       History Studio builds the film: your voice, realistic pictures drawn on Kaggle, cards, maps,
                 HyperFrames and Remotion effects, music and sound, the YouTube pack

RESEARCH_ONLY=1 stops after step 3, to read sources.md and script.md before the film is made.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path[:0] = [str(ROOT), str(ROOT / "documentary")]
from dotenv import load_dotenv  # noqa: E402

load_dotenv() or load_dotenv(".env.example")
import assets  # noqa: E402
import research  # noqa: E402
import write  # noqa: E402


def main(title, minutes=22):
    if sys.platform == "darwin":  # a sleeping Mac drops the network and half the screenshots with it
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    folder = ROOT / "output" / slug
    (folder / "research").mkdir(parents=True, exist_ok=True)
    (folder / "topic.txt").write_text(title, encoding="utf-8")
    if not (folder / "script.md").exists():
        sources = research.gather(title, minutes, folder / "research")
        if len(sources) < 6:
            sys.exit(f"only {len(sources)} readable sources found for this title; try a more specific headline")
        claims = research.ledger(title, sources, folder / "research")
        research.check_title(title, claims, folder / "research")
        sources = assets.screenshots(sources, folder / "research" / "shots")
        write.script(title, minutes, claims, sources, folder)
    print(f"script ready: {folder / 'script.md'}", flush=True)
    if os.getenv("RESEARCH_ONLY") == "1":
        return
    env = {**os.environ, "STYLE": os.getenv("DOC_STYLE", "documentary"), "ASK_CLEAN": "0"}
    sys.exit(subprocess.run([sys.executable, "-u", "make.py", "build", str(folder.relative_to(ROOT))], env=env).returncode)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip())
    if len(sys.argv) > 3:  # a headline like "3,000 vs 100,000: The 7-Day Stand..." needs its event named for research
        os.environ["DOC_SUBJECT"] = sys.argv[3]
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 22)
