import hashlib
import os
import re
import subprocess
import tempfile
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

UA = {"User-Agent": f"HistoryStudio/1.0 (personal documentary tool; {os.getenv('CONTACT', 'no contact')})"}
ZOOM = 1.15
MODERN = re.compile(r"\(\d{8,}\)|\b(19[5-9]\d|20\d\d)\b")


def size():
    h = int(os.getenv("HEIGHT", "1080"))
    return h * 16 // 9 // 2 * 2, h


def font(px):
    for name in (os.getenv("FONT", "assets/font.ttf"), "impact.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            pass
    return ImageFont.load_default(size=px)


def commons(query, n=20):
    r = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=30, params={
        "action": "query", "generator": "search", "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6,
        "gsrlimit": n, "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 2400, "format": "json"}).json()
    found = []
    for p in sorted(r.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0)):
        info = p["imageinfo"][0]
        if info["width"] < 900 or MODERN.search(p["title"]) or p["title"].lower().endswith((".tif", ".tiff")):
            continue
        meta = info.get("extmetadata", {})
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "unknown")).strip()
        lic = meta.get("LicenseShortName", {}).get("value", "")
        found.append((info.get("thumburl") or info["url"], f"{p['title'][5:]}, {artist}, {lic}, Wikimedia Commons"))
    return found


def frame(img):
    w, h = size()
    W, H = int(w * ZOOM) // 2 * 2, int(h * ZOOM) // 2 * 2
    img = img.convert("RGB")
    if img.width / img.height < 1.3:
        bg = ImageOps.fit(img, (W, H)).filter(ImageFilter.GaussianBlur(40))
        bg = ImageEnhance.Brightness(bg).enhance(0.45)
        fg = ImageOps.contain(img, (W, int(H * 0.92)), Image.LANCZOS)
        bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
        return bg
    fit = ImageOps.fit(img, (W, H), Image.LANCZOS)
    # a picture smaller than the frame was stretched: sharpen so the lines stay crisp
    return fit.filter(ImageFilter.UnsharpMask(radius=2, percent=70, threshold=3)) if img.width < W else fit


def upscale(img):
    """Real-ESRGAN through ncnn (Vulkan, so it runs on the Apple GPU too): twice the size with clean edges, where a
    plain resize only blurs. UPSCALE=0 turns it off; without the program the picture passes through as it is."""
    exe = Path(os.getenv("ESRGAN", "tools/realesrgan/realesrgan-ncnn-vulkan"))
    if os.getenv("UPSCALE", "1") != "1" or not exe.exists():
        return img
    with tempfile.TemporaryDirectory() as d:
        a, b = Path(d, "a.png"), Path(d, "b.png")
        img.save(a)
        done = subprocess.run([str(exe), "-i", str(a), "-o", str(b), "-s", "2", "-m", str(exe.parent / "models"),
                               "-n", os.getenv("ESRGAN_MODEL", "realesr-animevideov3")], capture_output=True)
        if done.returncode or not b.exists():
            print("    upscaler failed, using the plain picture:", done.stderr.decode(errors="ignore")[-200:])
            os.environ["UPSCALE"] = "0"
            return img
        return Image.open(b).convert("RGB")


def download(url, dst):
    """The picture as it is, no cropping (documents and portraits keep their shape)."""
    r = requests.get(url, headers=UA, timeout=60)
    if r.status_code == 429:
        time.sleep(float(r.headers.get("Retry-After", 20)) + 2)
        r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    Image.open(BytesIO(r.content)).convert("RGB").save(dst, quality=92)
    return dst


def fetch(url, dst, tries=1, trim=0.0):
    for k in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=150)
            if r.status_code in (402, 429):
                time.sleep(float(r.headers.get("Retry-After", 45)) + 2)
                continue
            r.raise_for_status()
            img = Image.open(BytesIO(r.content))
            frame(img.crop((0, 0, img.width, round(img.height * (1 - trim))))).save(dst, quality=92)
            return dst
        except Exception:
            time.sleep(10 * (k + 1))
    return None


ILLUSTRATION = {"CLIPS": "0", "GRADE": "0"}
PERIOD = "modern clothes, jeans, sweater, t-shirt, tank top, sneakers, car, phone"
# look, avoid, settings. The two illustration looks won style_test.py (row 3 of each sheet).
STYLES = {
    "sketch": ("rough graphite pencil sketch, 2D animation character concept art, appealing stylized cartoon character, "
               "expressive face, simple shapes, loose gestural lines, soft grey shading, unfinished edges fading to "
               "white, plain white paper, empty background",
               "photo, photorealistic, realistic proportions, 3d render, detailed background, busy background, sepia, "
               "beige paper, brown paper, color, ink wash, comic book, manga, anime, text, watermark, signature, "
               f"deformed hands, extra fingers, {PERIOD}", {**ILLUSTRATION, "PAPER": "mono"}),
    # no LoRA trigger word ("KidsRedmAF, Kids Book"): with it, half the people in the Mary Celeste video came out as
    # children; the LoRA at 0.6 still gives the storybook look (TRIGGER puts the word back)
    "storybook": ("picture book illustration, charming simple characters, grown ups with adult "
                  "proportions, rosy cheeks, thin warm brown pencil outlines, soft flat colors, light paper texture, "
                  "wide storybook scene, small full body figures with open space around them",
                  # no "dark" here: night, storm and mystery light comes from MOOD_LIGHT
                  "photo, photorealistic, 3d render, chibi, toddler, gritty, gore, horror, heavy shading, harsh "
                  "contrast, thick black outlines, close up, portrait, face filling the frame, cropped body, anime, manga, "
                  f"text, watermark, signature, deformed hands, {PERIOD}",
                  # won look_test.py (row 4): SDXL base with the 8 step Lightning LoRA, then Real-ESRGAN
                  {**ILLUSTRATION, "PAPER": "pastel", "SD_MODEL": "stabilityai/stable-diffusion-xl-base-1.0",
                   "LORA": "ByteDance/SDXL-Lightning:1.0:sdxl_lightning_8step_lora.safetensors,"
                           "artificialguybr/StoryBookRedmond-V2:0.6",
                   "SD_SIZE": "1344x768", "SD_STEPS": "8", "SD_SCHEDULER": "trailing", "SD_CFG": "2", "UPSCALE": "1",
                   "WIDE": "1"}),
    "reference": ("detailed 2D digital illustration, clean line art, soft cel shading, warm cinematic light, rich background",
                  "photo, photorealistic, 3d render, text, letters, watermark, logo, signature, blurry, deformed hands, "
                  "extra fingers", {}),
    "cinematic": ("cinematic film still, realistic historical scene, golden hour light, vivid saturated colors, lush "
                  "greens and warm gold, epic composition, sharp focus, 35mm",
                  "cartoon, anime, illustration, painting, text, letters, watermark, logo, signature, blurry, deformed hands, "
                  f"extra fingers, plastic skin, sepia, brown tint, desaturated, {PERIOD}", {"CLIPS": "0", "POSES": "0"}),
    # for documentary/ (today's stories as well as old ones): a real photograph, and no period rule
    "documentary": ("documentary photograph, photojournalism, realistic, natural light, muted cinematic colours, "
                    "shallow depth of field, 35mm, sharp focus",
                    "cartoon, anime, illustration, painting, drawing, child, toy, text, letters, watermark, logo, "
                    "signature, blurry, deformed hands, extra fingers, plastic skin",
                    {"CLIPS": "0", "POSES": "0", "SD_MODEL": "stabilityai/stable-diffusion-xl-base-1.0",
                     "LORA": "ByteDance/SDXL-Lightning:1.0:sdxl_lightning_8step_lora.safetensors", "SD_SIZE": "1344x768",
                     "SD_STEPS": "8", "SD_SCHEDULER": "trailing", "SD_CFG": "2", "UPSCALE": "1", "WIDE": "1"}),
}
SHORT = {"sketch": "pencil sketch on white paper, animation concept art",
         "storybook": "storybook illustration"}
_pipe = _encoders = None


def long_prompt(text, avoid, dev):
    """SDXL text embeddings without the 77 token cut: CLIP reads the prompt in pieces of 75 tokens and the pieces are
    joined, the way long prompts work in the common SDXL apps. Without this the long storybook look pushed the scene
    out of the big encoder and the pictures ignored what happens in them."""
    import torch
    pieces = []
    for tok in (_pipe.tokenizer, _pipe.tokenizer_2):
        ids = [tok(x, add_special_tokens=False).input_ids for x in (text, avoid)]
        pieces.append([[i[k:k + 75] for k in range(0, max(len(i), 1), 75)] for i in ids])
    n = max(len(c) for enc in pieces for c in enc)
    out = []
    for side in (0, 1):  # prompt, then what to avoid
        hidden, pooled = [], None
        for enc, tok, chunks in zip((_pipe.text_encoder, _pipe.text_encoder_2), (_pipe.tokenizer, _pipe.tokenizer_2),
                                    (pieces[0][side], pieces[1][side])):
            parts = []
            for c in chunks + [[]] * (n - len(chunks)):
                ids = [tok.bos_token_id, *c, tok.eos_token_id]
                ids += [tok.pad_token_id] * (77 - len(ids))
                got = enc(torch.tensor([ids], device=dev), output_hidden_states=True)
                parts.append(got.hidden_states[-2])
                if enc is _pipe.text_encoder_2 and pooled is None:
                    pooled = got[0]
            hidden.append(torch.cat(parts, dim=1))
        out.append((torch.cat(hidden, dim=-1).to(torch.float16), pooled.to(torch.float16)))
    (pos, pooled), (neg, neg_pooled) = out
    return pos, neg, pooled, neg_pooled


# light and colour for the feeling of the moment (make.py adds it to each picture from the edit plan's mood)
MOOD_LIGHT = {
    "calm": "soft daylight, gentle natural colours",
    "mystery": "misty blue dusk, muted cold colours, long shadows, serious faces",
    "tension": "dark stormy overcast sky, cold grey blue colours, dramatic light, worried faces",
    "sad": "grey overcast light, muted faded colours, quiet sad faces",
    "wonder": "warm golden glow, rich colours",
    "triumph": "bright warm sunlight, vivid colours",
}


def people(prompt):
    """Said first, so the model reads it before anything else: who is in the picture. Negatives barely work at the
    low guidance the Lightning model uses, so adults and empty places have to be asked for, not just forbidden."""
    p = prompt.lower()
    if os.getenv("ADULTS", "1") != "1":
        return ""
    if re.search(r"\b(empty|deserted|abandoned|no one|nobody|without people|unmanned)\b", p):
        return "no people, an empty place, "
    if re.search(r"\b(child|children|kid|boy|girl|baby|son|daughter)s?\b", p):
        return ""
    if re.search(r"\b(men|man|sailors?|crew|captain|mate|officer|official|judge|keeper|seamen|doctor|scientist|writer|"
                 r"people|crowd|woman|women|owner|consul|surveyor|diver|historian|merchants?)\b", p):
        return "grown up adults with mature faces, "
    return ""


def absent(prompt):
    """What must not be in this picture. The storybook LoRA draws children everywhere, so children are kept out unless
    the scene asks for them, and an empty room or deserted place gets no people at all."""
    p = prompt.lower()
    out = [] if re.search(r"\b(child|children|kid|boy|girl|baby|son|daughter)s?\b", p) else ["children, kids, child"]
    if re.search(r"\b(empty|deserted|abandoned|no one|nobody|without people|unmanned)\b", p):
        out.append("people, person, man, woman, figure, crowd")
    return ", ".join(out)


def wide(prompt):
    """WIDE=1 (storybook): small figures with room around them, so a close up the model asked for becomes a full body shot."""
    if os.getenv("WIDE") != "1":
        return prompt
    return re.sub(r"\b(extreme )?close[ -]?up|medium( close)? shot|portrait", "full body shot", prompt, flags=re.I)


# things a history picture must not show: the model drew cannons and top hats into a battle of 636
LATER = "modern clothing, modern uniform, car, electric light, plastic, wristwatch, eyeglasses, skyscraper, asphalt road"
GUNS = "firearm, gun, rifle, musket, cannon, artillery, top hat, cowboy hat, bowler hat"


def style():
    name = os.getenv("STYLE", "reference")
    look, avoid, _ = STYLES.get(name, STYLES["reference"])
    look = os.getenv("ART_STYLE") or look
    trigger = os.getenv("TRIGGER", "")
    year = re.search(r"\b(\d{3,4})\b", os.getenv("DOC_SUBJECT", ""))
    if year:  # a film about a dated event: nothing from the centuries after it
        avoid = ", ".join(x for x in (avoid, LATER, GUNS if int(year.group(1)) < 1300 else "") if x)
    return ", ".join(x for x in (trigger, look) if x), ", ".join(x for x in (trigger, SHORT.get(name, look)) if x), avoid


def paper(img):
    """Lift the paper to white and take out the brown cast the model adds (PAPER=mono for pencil, pastel for colour)."""
    import numpy as np
    kind = os.getenv("PAPER")
    if not kind:
        return img
    a = np.asarray(img.convert("RGB")).astype(float)
    lum = a.mean(axis=2)
    if kind == "mono":
        lo, hi = np.percentile(lum, 0.5), np.percentile(lum, 75)
        return Image.fromarray(np.clip((a - lo) / max(hi - lo, 1) * 250, 0, 255).astype("uint8")).convert("L").convert("RGB")
    # colour: only a gentle level, so night and storm pictures keep their dark (the old 75% white point washed them out)
    lo, hi = np.percentile(lum, 0.3), np.percentile(lum, 99.5)
    out = Image.fromarray(np.clip((a - lo) / max(hi - lo, 1) * 255, 0, 255).astype("uint8"))
    return ImageEnhance.Color(out).enhance(0.9)


def device():
    import torch
    return "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"


def free():
    import torch
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    elif device() == "mps":
        torch.mps.empty_cache()


def engine():
    if os.getenv("IMAGE_ENGINE"):
        return os.getenv("IMAGE_ENGINE")
    try:
        import diffusers  # noqa: F401
        return "local" if device() != "cpu" else "pollinations"
    except ImportError:
        return "pollinations"


def unet_fp8(repo):
    """SDXL's UNet stored once in 8 bits (2.6 GB instead of 5.1 GB) and loaded straight onto the GPU, so a laptop
    with 6 GB of VRAM and 8 GB of RAM can run it. Each layer is widened to 16 bits only while it runs."""
    import torch
    from accelerate import init_empty_weights
    from diffusers import UNet2DConditionModel
    from huggingface_hub import hf_hub_download
    from safetensors import safe_open
    from safetensors.torch import load_file, save_file

    cache = Path.home() / ".cache" / "history_studio" / f"{repo.replace('/', '--')}-unet-fp8.safetensors"
    if not cache.exists():
        print("    preparing the 8 bit picture model (one time)")
        cache.parent.mkdir(parents=True, exist_ok=True)
        keep = ("conv_in", "conv_out", "proj_in", "proj_out")
        with safe_open(hf_hub_download(repo, "unet/diffusion_pytorch_model.fp16.safetensors"), "pt") as f:
            small = {k: t.to(torch.float8_e4m3fn) if t.ndim > 1 and not k.rsplit(".", 1)[0].endswith(keep) else t
                     for k in f.keys() for t in [f.get_tensor(k)]}
        save_file(small, str(cache))
        del small
    with init_empty_weights():
        unet = UNet2DConditionModel.from_config(UNet2DConditionModel.load_config(repo, subfolder="unet"))
    unet.load_state_dict(load_file(str(cache), device="cuda"), assign=True)

    def widen(mod, _):
        mod.w8 = mod.weight.data
        mod.weight.data = mod.w8.to(torch.float16)

    def narrow(mod, *_):
        mod.weight.data = mod.w8

    for mod in unet.modules():
        if getattr(mod, "weight", None) is not None and mod.weight.dtype == torch.float8_e4m3fn:
            mod.register_forward_pre_hook(widen)
            mod.register_forward_hook(narrow)
    return unet.eval()


def blank(path):
    """Missing, or a black picture left by a failed run."""
    try:
        return Image.open(path).convert("L").getextrema()[1] < 12
    except OSError:
        return True


def check(img):
    if img.convert("L").getextrema()[1] < 12:
        raise SystemExit("the picture model returned a black picture (its numbers broke), stopping so no hours are "
                         "wasted. Send this log.")


def compose(prompt):
    """(text, avoid) the picture model gets for one scene: the scene goes first, so it never loses to the look."""
    look, short, avoid = style()
    return f"{short}, {people(prompt)}{wide(prompt)}, {look}", f"{avoid}, {absent(prompt)}"


def finish(img, dst):
    """A drawn picture made ready for the film: paper level, 2x upscale, framed. Shared by the Mac and Kaggle paths."""
    check(img)
    frame(upscale(paper(img))).save(dst, quality=93)


def local(jobs, moves=()):
    """jobs: [(prompt, seed, dst)], moves: [(prompt, seed, src, dst)] for pose frames. On an NVIDIA card with little
    memory the UNet runs in 8 bits and the text encoders leave the GPU after one pass. On a Mac the whole model sits
    in unified memory on the Apple GPU.
    A pose frame starts from its finished shot (img2img with the same seed), so people, place and camera stay put
    and only the action moves on."""
    global _pipe, _encoders
    import torch
    from diffusers import (AutoencoderKL, DPMSolverMultistepScheduler, EulerDiscreteScheduler,
                           StableDiffusionXLImg2ImgPipeline, StableDiffusionXLPipeline)

    dev = device()
    mac = dev == "mps"
    width, height = map(int, (os.getenv("SD_SIZE") or ("1024x576" if mac else "1344x768")).split("x"))
    steps = int(os.getenv("SD_STEPS") or (4 if mac else 6))
    cfg = float(os.getenv("SD_CFG", "2"))
    if _pipe is None:
        repo = os.getenv("SD_MODEL", "lykon/dreamshaper-xl-lightning")
        if dev == "cuda":
            _pipe = StableDiffusionXLPipeline.from_pretrained(repo, unet=unet_fp8(repo), torch_dtype=torch.float16,
                                                              variant="fp16")
            _pipe.enable_vae_tiling()
            _pipe.vae.to(dev)
        else:
            vae = AutoencoderKL.from_pretrained("madebyollin/sdxl-vae-fp16-fix", torch_dtype=torch.float16)
            # no attention slicing on a Mac: the sliced path does its math in 16 bit, overflows and gives black pictures
            _pipe = StableDiffusionXLPipeline.from_pretrained(repo, vae=vae, torch_dtype=torch.float16,
                                                              variant="fp16").to(dev)
        # LORA="repo:weight[:file],..." (the 8 bit UNet of the NVIDIA path cannot take a LoRA)
        loras = [x.split(":") for x in os.getenv("LORA", "").split(",") if x] if dev != "cuda" else []
        for k, (lora, _, *file) in enumerate(loras):
            _pipe.load_lora_weights(lora, weight_name=file[0] if file else None, adapter_name=f"l{k}")
        if loras:
            _pipe.set_adapters([f"l{k}" for k in range(len(loras))], adapter_weights=[float(x[1]) for x in loras])
            _pipe.fuse_lora()
            _pipe.unload_lora_weights()
        if os.getenv("SD_SCHEDULER") == "trailing":  # what the SDXL Lightning LoRAs were trained with
            _pipe.scheduler = EulerDiscreteScheduler.from_config(_pipe.scheduler.config, timestep_spacing="trailing")
        else:
            _pipe.scheduler = DPMSolverMultistepScheduler.from_config(_pipe.scheduler.config)
        _encoders = (_pipe.text_encoder, _pipe.text_encoder_2)
    _pipe.text_encoder, _pipe.text_encoder_2 = (e.to(dev) for e in _encoders)
    with torch.no_grad():  # long_prompt keeps every word
        embeds = [long_prompt(*compose(p), dev) for p, *_ in [*jobs, *moves]]
    if dev == "cuda":
        for e in _encoders:
            e.to("cpu")
        _pipe.text_encoder = _pipe.text_encoder_2 = None
    free()
    for k, ((_, seed, dst), (pos, neg, pooled, neg_pooled)) in enumerate(zip(jobs, embeds), 1):
        t = time.time()
        img = _pipe(prompt_embeds=pos, negative_prompt_embeds=neg, pooled_prompt_embeds=pooled,
                    negative_pooled_prompt_embeds=neg_pooled, num_inference_steps=steps,
                    guidance_scale=cfg, width=width, height=height, generator=torch.manual_seed(seed)).images[0]
        finish(img, dst)
        free()
        print(f"    image {k}/{len(jobs)} in {time.time() - t:.0f}s")
    strength = float(os.getenv("POSE_STRENGTH", "0.5"))
    redraw = StableDiffusionXLImg2ImgPipeline(**_pipe.components)
    for k, ((_, seed, src, dst), (pos, neg, pooled, neg_pooled)) in enumerate(zip(moves, embeds[len(jobs):]), 1):
        t = time.time()
        img = redraw(image=Image.open(src).convert("RGB").resize((width, height), Image.LANCZOS), strength=strength,
                     prompt_embeds=pos, negative_prompt_embeds=neg, pooled_prompt_embeds=pooled,
                     negative_pooled_prompt_embeds=neg_pooled, num_inference_steps=round(max(2, steps - 1) / strength),
                     guidance_scale=cfg, generator=torch.manual_seed(seed)).images[0]
        finish(img, dst)
        free()
        print(f"    pose {k}/{len(moves)} in {time.time() - t:.0f}s")


def release():
    global _pipe, _encoders
    if _pipe is not None:
        import gc
        _pipe = _encoders = None
        gc.collect()
        free()


def pose_files(src):
    return sorted(p for p in src.parent.glob(f"{src.stem}.p*.jpg") if re.fullmatch(rf"{re.escape(src.stem)}\.p\d\.jpg", p.name))


def pending(prompts, folder, poses=None):
    """File of every picture, and which pictures and pose frames are still to draw: [(prompt, seed, dst)] and
    [(prompt, seed, src, dst)]. The name comes from the prompt, so a picture drawn on Kaggle is found here later."""
    folder.mkdir(parents=True, exist_ok=True)
    look, _, _ = style()
    kind = engine()
    paths = [folder / f"s{hashlib.md5(f'{kind}{p}{look}'.encode()).hexdigest()[:10]}.jpg" if p else None for p in prompts]
    todo = [(p, 101 + i, dst) for i, (p, dst) in enumerate(zip(prompts, paths)) if dst and blank(dst)]
    moves = [(f"{m}, {p}", 101 + i, dst, dst.with_suffix(f".p{k}.jpg"))
             for i, (p, dst, steps) in enumerate(zip(prompts, paths, poses or [[]] * len(paths))) if dst
             for k, m in enumerate(steps, 1) if dst in [t[2] for t in todo] or blank(dst.with_suffix(f".p{k}.jpg"))]
    return paths, todo, moves


def scenes(prompts, folder, poses=None):
    """One picture per narration beat, made on the GPU when there is one, otherwise by the free web service
    (one image every 45 seconds). A beat with poses also gets one extra frame per pose (GPU only).
    Finished pictures are kept for the next run."""
    look, _, _ = style()
    kind = engine()
    gap = float(os.getenv("AI_GAP", "46"))
    w, h = size()
    paths, todo, moves = pending(prompts, folder, poses)
    if os.getenv("PLAN_ONLY") == "1":  # the plan gate: where each picture will be is enough, nothing is drawn
        return paths
    if (todo or moves) and os.getenv("MAC_PICTURES", "1") == "0":
        raise SystemExit(f"{len(todo)} pictures and {len(moves)} pose frames did not come from Kaggle and drawing on "
                         "the Mac is off (MAC_PICTURES=0). Run the build again, finished pictures are kept.")
    if kind == "local" and (todo or moves):
        local(todo, moves)
    elif todo:
        last = 0.0
        for k, (p, seed, dst) in enumerate(todo, 1):
            time.sleep(max(0.0, last + gap - time.time()))
            last = time.time()
            url = (f"https://image.pollinations.ai/prompt/{quote(f'{look}. {p}')}"
                   f"?width={w}&height={round(h * 1.08)}&seed={seed}&nologo=true&model=flux")
            print(f"    image {k}/{len(todo)} {'ok' if fetch(url, dst, tries=5, trim=0.08) else 'failed'}")
    return [d if d and d.exists() else None for d in paths]


def stock(queries, need, folder):
    folder.mkdir(parents=True, exist_ok=True)
    done = sorted(folder.glob("r*.jpg"))
    credit_file = folder / "credits.txt"
    if not done and need:
        picks, seen = [], set()
        for q in queries:
            try:
                picks += [p for p in commons(q) if p[0] not in seen and not seen.add(p[0])]
            except requests.RequestException:
                pass
            if len(picks) >= need + 5:
                break
        with ThreadPoolExecutor(4) as ex:
            got = list(ex.map(lambda a: fetch(a[1][0], folder / f"r{a[0]:03d}.jpg", 2), enumerate(picks[:need + 5])))
        credit_file.write_text("\n".join(p[1] for p, ok in zip(picks, got) if ok), encoding="utf-8")
        done = sorted(folder.glob("r*.jpg"))
    credits = credit_file.read_text(encoding="utf-8").splitlines() if credit_file.exists() else []
    return done, credits


def card(src, number, text, dst):
    img = ImageEnhance.Brightness(Image.open(src)).enhance(0.35)
    d = ImageDraw.Draw(img)
    W, H = img.size
    d.text((W / 2, H / 2 - H * 0.09), f"CHAPTER {number}", font=font(H // 22), fill="#e8c76a", anchor="mm")
    d.multiline_text((W / 2, H / 2 + H * 0.03), textwrap.fill(text.upper(), 22), font=font(H // 11),
                     fill="white", anchor="mm", align="center", spacing=H // 60)
    img.save(dst, quality=92)
    return dst


def thumbnail(src, text, dst):
    img = ImageOps.fit(Image.open(src).convert("RGB"), (1280, 720), Image.LANCZOS)
    img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(1.3)).enhance(1.15)
    shade = Image.linear_gradient("L").rotate(90).resize((1280, 720))
    img = Image.composite(img, Image.new("RGB", img.size, "black"), shade.point(lambda v: 60 + v * 0.75))
    d = ImageDraw.Draw(img)
    d.multiline_text((60, 360), textwrap.fill(text.upper(), 14), font=font(104), fill="#ffd400", anchor="lm",
                     stroke_width=7, stroke_fill="black", spacing=10)
    img.save(dst, quality=95)
