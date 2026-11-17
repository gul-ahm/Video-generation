"""Try picture set ups side by side before making a video, the way voice_test does for voices.

python style_test.py              both styles
python style_test.py sketch       one style (sketch or storybook)

Every candidate draws the same 4 test scenes. Result: output/style_test/<style>.jpg, one row per candidate, plus a
score for how close each row is to the reference look (paper colour, colour amount, ink, empty space, detail).
Pictures are kept, so a second run only draws what is new.
"""
import gc
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFilter, ImageFont

os.chdir(Path(__file__).parent)
load_dotenv() or load_dotenv(".env.example")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import visuals  # noqa: E402

DS = "lykon/dreamshaper-xl-lightning"
BASE = "stabilityai/stable-diffusion-xl-base-1.0"
LIGHTNING = ("ByteDance/SDXL-Lightning", "sdxl_lightning_4step_lora.safetensors", 1.0)
STORY = ("artificialguybr/StoryBookRedmond-V2", None, 0.9)

SCENES = [
    "medium shot, a young woman in a plain 1518 linen dress and white headscarf dancing wildly on a cobblestone street, "
    "arms thrown up, eyes shut",
    "close up, an old bearded city councilman in a dark 16th century robe and cap, holding a quill, worried frown",
    "wide shot, a small medieval town square with timber framed houses, a few townsfolk in 16th century clothes watching",
    "medium shot, a physician in a long dark coat kneeling beside an exhausted woman sitting on stone steps, "
    "checking her swollen feet",
]

# measured on the two reference pictures
REF = {"sketch": dict(paper=236, colour=1, ink=5.7, empty=80, detail=8.5),
       "storybook": dict(paper=243, colour=14, ink=2.6, empty=96, detail=4.1)}

LOOK = {
    "sketch": ("rough graphite pencil sketch, 2D animation character concept art, appealing stylized cartoon character, "
               "expressive face, simple shapes, loose gestural lines, soft grey shading, unfinished edges fading to "
               "white, plain white paper, minimal background",
               "pencil sketch on white paper, animation concept art",
               "photo, photorealistic, realistic proportions, 3d render, detailed background, busy background, sepia, "
               "beige paper, brown paper, color, ink wash, comic book, manga, anime, modern clothes, car, t-shirt, "
               "tank top, text, watermark, signature, deformed hands, extra fingers"),
    "storybook": ("children's picture book illustration, cute simple rounded characters, dot eyes, rosy cheeks, thin warm "
                  "brown pencil outlines, soft flat pastel colors, light paper texture, pale cream background, whimsical, "
                  "minimal background",
                  "cute pastel children's book illustration",
                  "photo, photorealistic, realistic proportions, 3d render, dark, gritty, horror, heavy shading, harsh "
                  "contrast, thick black outlines, detailed background, anime, manga, modern clothes, car, text, "
                  "watermark, signature, deformed hands"),
}

# name, model, loras, prompt, clean paper, guidance
CANDIDATES = {
    "sketch": [
        ("now", DS, [], "old", False, 2),
        ("new prompt", DS, [], "new", False, 2),
        ("new prompt + white paper", DS, [], "new", True, 2),
        ("SDXL base + new prompt + white paper", BASE, [LIGHTNING], "new", True, 1.5),
    ],
    "storybook": [
        ("now", DS, [], "old", False, 2),
        ("new prompt + clean paper", DS, [], "new", True, 2),
        ("storybook LoRA + clean paper", DS, [STORY], "lora", True, 2),
        ("SDXL base + storybook LoRA + clean paper", BASE, [LIGHTNING, STORY], "lora", True, 1.5),
    ],
}


def pipeline(repo, loras):
    import torch
    from diffusers import (AutoencoderKL, DPMSolverMultistepScheduler, EulerDiscreteScheduler,
                           StableDiffusionXLPipeline)
    vae = AutoencoderKL.from_pretrained("madebyollin/sdxl-vae-fp16-fix", torch_dtype=torch.float16)
    pipe = StableDiffusionXLPipeline.from_pretrained(repo, vae=vae, torch_dtype=torch.float16, variant="fp16")
    if loras:
        for k, (lora, weight, _) in enumerate(loras):
            pipe.load_lora_weights(lora, weight_name=weight, adapter_name=f"l{k}")
        pipe.set_adapters([f"l{k}" for k in range(len(loras))], adapter_weights=[w for *_, w in loras])
        pipe.fuse_lora()
        pipe.unload_lora_weights()
    if repo == BASE:
        pipe.scheduler = EulerDiscreteScheduler.from_config(pipe.scheduler.config, timestep_spacing="trailing")
    else:
        pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config)
    dev = visuals.device()
    pipe.enable_model_cpu_offload() if dev == "cuda" else pipe.to(dev)
    return pipe


def prompts(style, mode, scene):
    if mode == "old":
        look, avoid, _ = visuals.STYLES[style]
        return dict(prompt=f"{look}, {scene}", negative_prompt=avoid)
    look, short, avoid = LOOK[style]
    if mode == "lora":
        look, short = f"KidsRedmAF, Kids Book, {look}", f"KidsRedmAF, {short}"
    # CLIP reads 77 tokens: the small encoder gets the scene first, the big one (which sets the overall look) the style
    return dict(prompt=f"{scene}, {short}", prompt_2=f"{look}, {scene}", negative_prompt=avoid, negative_prompt_2=avoid)


def clean(img, mono):
    """Lift the paper to white and drop the sepia cast, keep the pencil lines."""
    a = np.asarray(img.convert("RGB")).astype(float)
    lum = a.mean(axis=2)
    lo, hi = np.percentile(lum, 0.5), np.percentile(lum, 75)
    a = np.clip((a - lo) / max(hi - lo, 1) * 250, 0, 255)
    out = Image.fromarray(a.astype("uint8"))
    return out.convert("L").convert("RGB") if mono else out


def metrics(img):
    img = img.convert("RGB").resize((768, round(768 * img.height / img.width)))
    g = np.asarray(img.convert("L")).astype(float)
    h, w = g.shape
    border = np.concatenate([g[:h // 10].ravel(), g[-h // 10:].ravel(), g[:, :w // 10].ravel(), g[:, -w // 10:].ravel()])
    edges = np.asarray(img.convert("L").filter(ImageFilter.FIND_EDGES))
    return dict(paper=round(border.mean()), colour=round(np.asarray(img.convert("HSV"))[..., 1].mean()),
                ink=round((g < 140).mean() * 100, 1), empty=round((g > 225).mean() * 100),
                detail=round((edges > 40).mean() * 100, 1))


def score(m, ref):
    """100 means the same numbers as the reference."""
    gaps = [abs(m[k] - ref[k]) / (2.55 if k in ("paper", "colour") else 1) for k in ref]
    return round(100 - sum(gaps) / len(gaps))


def run(style):
    folder = Path("output/style_test") / style
    folder.mkdir(parents=True, exist_ok=True)
    times_file = folder / "times.json"
    times = json.loads(times_file.read_text()) if times_file.exists() else {}
    w, h = map(int, (os.getenv("SD_SIZE") or "1024x576").split("x"))
    rows = []
    for name, repo, loras, mode, cleaned, guidance in CANDIDATES[style]:
        key = f"{repo.split('/')[1]}-{'-'.join(lora[0].split('/')[1] for lora in loras) or 'none'}-{mode}"
        raw = [folder / f"{key}_{k}.jpg" for k in range(len(SCENES))]
        if not all(p.exists() for p in raw):
            print(f"{style}: {name}")
            try:
                pipe = pipeline(repo, loras)
            except Exception as e:
                print(f"  skipped, could not load: {type(e).__name__}: {str(e)[:160]}")
                continue
            import torch
            spent = []
            for k, (scene, dst) in enumerate(zip(SCENES, raw)):
                t = time.time()
                img = pipe(**prompts(style, mode, scene), num_inference_steps=4, guidance_scale=guidance,
                           width=w, height=h, generator=torch.manual_seed(7 + k)).images[0]
                img.save(dst, quality=93)
                spent.append(time.time() - t)
                print(f"  scene {k + 1}/{len(SCENES)} in {spent[-1]:.0f}s")
            times[key] = round(sum(spent[1:]) / max(len(spent) - 1, 1))
            times_file.write_text(json.dumps(times))
            del pipe
            gc.collect()
            visuals.free()
        imgs = [clean(Image.open(p), style == "sketch") if cleaned else Image.open(p).convert("RGB") for p in raw]
        for k, img in enumerate(imgs):
            img.save(folder / f"{name.replace(' ', '_').replace('+', 'and')}_{k}.jpg", quality=93)
        found = [metrics(i) for i in imgs]
        avg = {k: round(sum(m[k] for m in found) / len(found), 1) for k in found[0]}
        rows.append((name, imgs, avg, score(avg, REF[style]), times.get(key, "?")))
    sheet(style, rows)


def sheet(style, rows):
    tw, th, side = 480, 270, 470
    out = Image.new("RGB", (side + tw * len(SCENES), th * len(rows)), "white")
    draw = ImageDraw.Draw(out)
    font = ImageFont.load_default(size=22)
    print(f"\n{style}  (reference: {REF[style]})")
    print(f"{'candidate':42} score  sec/pic  paper colour  ink  empty  detail")
    for r, (name, imgs, m, s, sec) in enumerate(rows):
        for k, img in enumerate(imgs):
            out.paste(img.resize((tw, th)), (side + k * tw, r * th))
        draw.multiline_text((12, r * th + 20), f"{r + 1}. {name}\n\nscore {s} / 100\n{sec}s per picture",
                            fill="black", font=font, spacing=6)
        print(f"{r + 1}. {name:39} {s:5}  {sec:>7}  {m['paper']:5} {m['colour']:6} {m['ink']:4} {m['empty']:6} {m['detail']:6}")
    path = Path("output/style_test") / f"{style}.jpg"
    out.save(path, quality=90)
    print(f"sheet: {path.resolve()}")


if __name__ == "__main__":
    for style in sys.argv[1:] or ["sketch", "storybook"]:
        run(style)
