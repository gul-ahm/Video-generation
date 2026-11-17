"""Picture test for the storybook look: the same scenes through the real picture code with each set up.

python look_test.py

Draws the same 4 scenes with each set up (a set up drawn before is kept) through the real picture code, so the winner can go straight into videos.
The scenes are the Mary Celeste pictures that came out wrong (children for crews). Results in output/people_test:
sheet.jpg (all rows), zoom.jpg (the middle of scene 1 at full size, to judge sharpness) and every picture.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

os.chdir(Path(__file__).parent)
load_dotenv() or load_dotenv(".env.example")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

# the Mary Celeste pictures that went wrong: crews drawn as children, an empty cabin full of people
SCENES = [
    "Wide shot of seven sailors in 1870s wool jumpers and caps standing in a line on the deck of a sailing ship",
    "Wide shot of three sailors in wool coats rowing a small boat across rough grey waves toward an empty sailing ship, "
    "dark stormy overcast sky, cold grey blue colours, dramatic light, worried faces",
    "Wide shot of an empty ship's cabin with a small table, a few scattered things, water on the floor, grey light from "
    "a skylight, misty blue dusk, muted cold colours, long shadows",
    "Wide shot of a grand Victorian courtroom with a judge in a wig and red robe, papers on the bench",
    "Wide shot of a scientist in a Victorian laboratory examining a sword under a microscope, glass bottles on shelves",
    "Wide shot of a small lifeboat crowded with people pulling away from a sailing ship, a green island in the distance, "
    "grey overcast light, muted faded colours, quiet sad faces",
]
STORY = "artificialguybr/StoryBookRedmond-V2"
LIGHTNING8 = "ByteDance/SDXL-Lightning:1.0:sdxl_lightning_8step_lora.safetensors"
SETUPS = {
    "A as in the video (kids trigger, LoRA 0.9)": {"TRIGGER": "KidsRedmAF, Kids Book", "ADULTS": "0",
                                                   "LORA": f"{LIGHTNING8},{STORY}:0.9"},
    "B no trigger, LoRA 0.6, adults asked for": {},  # the new storybook settings in visuals.py
    "C as B with guidance 2": {"SD_CFG": "2"},
}
OUT = Path("output/people_test")


def one(name):
    os.environ["STYLE"] = "storybook"
    import visuals
    os.environ.update({**visuals.STYLES["storybook"][2], **SETUPS[name]})
    folder = OUT / name.split()[0]
    t = time.time()
    pics = visuals.scenes(SCENES, folder)
    (folder / "run.json").write_text(json.dumps([round((time.time() - t) / len(SCENES)), [str(x) for x in pics]]))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--one"]:
        one(" ".join(sys.argv[2:]))
        sys.exit()
    from PIL import Image, ImageDraw, ImageFont
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in SETUPS:
        print(f"set up {name}")
        if (OUT / name.split()[0] / "run.json").exists():
            print("  already drawn")
        elif subprocess.run([sys.executable, __file__, "--one", *name.split()]).returncode:
            print("  failed, see above")
            continue
        sec, pics = json.loads((OUT / name.split()[0] / "run.json").read_text())
        rows.append((name, [Path(x) for x in pics], sec))
    tw, th, side = 480, 270, 360
    font = ImageFont.load_default(size=22)
    sheet = Image.new("RGB", (side + tw * len(SCENES), th * len(rows)), "white")
    zoom = Image.new("RGB", (640 * len(rows), 400), "white")
    for r, (name, pics, sec) in enumerate(rows):
        ImageDraw.Draw(sheet).multiline_text((12, r * th + 20), f"{name}\n\n{sec}s per picture", fill="black", font=font)
        for k, p in enumerate(pics):
            sheet.paste(Image.open(p).resize((tw, th)), (side + k * tw, r * th))
        big = Image.open(pics[0])
        cx, cy = big.width // 2, big.height // 2
        zoom.paste(big.crop((cx - 320, cy - 180, cx + 320, cy + 180)), (r * 640, 0))
        ImageDraw.Draw(zoom).text((r * 640 + 10, 365), name, fill="black", font=font)
    sheet.save(OUT / "sheet.jpg", quality=90)
    zoom.save(OUT / "zoom.jpg", quality=92)
    for name, _, sec in rows:
        print(f"  {name:34} {sec}s per picture")
    print(f"open {(OUT / 'sheet.jpg').resolve()} and {(OUT / 'zoom.jpg').resolve()}")
