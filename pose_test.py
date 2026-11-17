"""Check character movement on this computer before using it in a full video.

python pose_test.py                      pencil sketch
STYLE=storybook python pose_test.py      storybook (on Windows: set STYLE=storybook, then python pose_test.py)

Makes 3 shots, each with 3 movement frames, joins them into output/pose_test/pose_test.mp4 (about 20 seconds,
no sound) and prints how long everything took, plus what that means for a full video.
"""
import os
import time
from pathlib import Path

from dotenv import load_dotenv

os.chdir(Path(__file__).parent)
load_dotenv() or load_dotenv(".env.example")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
os.environ["STYLE"] = os.getenv("STYLE") if os.getenv("STYLE") in ("sketch", "storybook") else "sketch"

import visuals  # noqa: E402

for key, value in visuals.STYLES[os.environ["STYLE"]][2].items():
    os.environ[key] = value

import render  # noqa: E402

SHOTS = [
    ("medium shot, John White, grey bearded English governor in a 1590 doublet, in a ruined colonial village, "
     "holding a sealed letter, worried face",
     ["he breaks the wax seal of the letter", "he unfolds the letter with both hands",
      "he reads the letter, eyes wide, mouth open"]),
    ("wide shot, small 16th century English sailing ship in a calm bay at dawn, colonists standing on the beach",
     ["the sails fill with wind and the ship turns", "the ship sails away from the beach",
      "the ship is small on the horizon, colonists waving"]),
    ("close up, English colonist man carving marks into a tall wooden post with a knife, forest behind him",
     ["he lifts the knife to the post", "he carves into the wood, chips flying",
      "he steps back and looks at the carved post"]),
]

folder = Path("output/pose_test") / os.environ["STYLE"]
prompts, poses = [p for p, _ in SHOTS], [m for _, m in SHOTS]

t = time.time()
visuals.local([])
load = time.time() - t
t = time.time()
shots = visuals.scenes(prompts, folder)
base = (time.time() - t) / len(SHOTS)
t = time.time()
visuals.scenes(prompts, folder, poses)
move = (time.time() - t) / sum(map(len, poses))
visuals.release()

t = time.time()
parts = [render.segment(s, round(6.5 * render.FPS), k, folder / f"part{k}.mp4") for k, s in enumerate(shots)]
(folder / "parts.txt").write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts))
out = folder / "pose_test.mp4"
render.ffmpeg("-f", "concat", "-safe", 0, "-i", folder / "parts.txt", "-c", "copy", out)
cut = time.time() - t

minutes = int(os.getenv("MINUTES", "60"))
beats = minutes * 130 // 40
print(f"""
Done: {out.resolve()}
  loading the picture model   {load:.0f}s (once per video)
  one picture                 {base:.0f}s
  one movement frame          {move:.0f}s
  rendering 20 seconds        {cut:.0f}s

For a {minutes} minute video (about {beats} pictures, movement on about one in three, 3 frames each):
  pictures         about {beats * base / 60:.0f} min
  movement frames  about {beats * move / 60:.0f} min extra
If "one picture" shows 0s, the pictures were made in an earlier run: delete output/pose_test to measure again.
If a number above is much bigger than the rest, the computer ran out of memory during that step. Close other apps
and run the test again.""")
