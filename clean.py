"""Delete the working files of a finished video and keep only what is needed to upload it.

python clean.py output/the-lost-colony-of-roanoke      asks first, deletes only after you type yes
"""
import os
import shutil
import sys
from pathlib import Path

KEEP = {"final.mp4", "thumbnail.jpg", "youtube.txt", "subtitles.srt"}


def size(p):
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size


def junk(folder):
    folder = Path(folder)
    return [p for p in folder.iterdir() if p.name not in KEEP] if (folder / "final.mp4").exists() else []


def junk_gb(folder):
    return round(sum(map(size, junk(folder))) / 1024 ** 3, 2)


def clean(folder):
    gone = junk(folder)
    freed = sum(map(size, gone))
    for p in gone:
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    return round(freed / 1024 ** 3, 2)


def ask(folder):
    folder = Path(folder)
    if not junk(folder):
        return
    print(f"\nVideo ready: {(folder / 'final.mp4').resolve()}")
    print(f"Check the video. If it is correct, the working files can be deleted ({junk_gb(folder)} GB of pictures,")
    print("voice, chapter clips and script). Kept: final.mp4, thumbnail.jpg, youtube.txt, subtitles.srt")
    if os.getenv("ASK_CLEAN", "1") == "0" or not sys.stdin.isatty():  # the dashboard and the job runner do not wait
        print("Nothing deleted. Later you can run: python clean.py " + str(folder))
        return
    if input("Clean the storage? Type yes if sure: ").strip().lower() == "yes":
        print(f"Cleaned, {clean(folder)} GB free again.")
    else:
        print("Nothing deleted. Later you can run: python clean.py " + str(folder))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip())
    for f in sys.argv[1:]:
        ask(f)
