"""Pictures on Kaggle's free GPU (two T4s) instead of the Mac.

The Mac writes every prompt of the video into one small Kaggle script, pushes it as a private notebook and waits.
On Kaggle the script downloads the same picture model and LoRAs, draws the pictures and the pose frames on both
GPUs and ends. A pushed notebook is a batch run: the session closes by itself the moment the script ends, and
the push carries a time limit too, so no GPU hours keep running after the work. The Mac then fetches the pictures,
does the paper level, the upscale and the frame (visuals.finish) and puts them where the build looks for them.
Whatever Kaggle did not deliver is drawn on the Mac as before.

Needs ~/.kaggle/kaggle.json (kaggle.com > Settings > API > Create New Token) and a phone verified account (GPU and
internet). KAGGLE=0 keeps everything on the Mac.

python kaggle_gpu.py test     two pictures on Kaggle, to check the account
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

import cutout
import visuals

TOKEN = Path.home() / ".kaggle" / "kaggle.json"
SLUG = "history-studio-pictures"
WORK = Path("output/.kaggle")

SCRIPT = r'''
import json, os, subprocess, sys, threading, time
JOBS = json.loads(r"""__JOBS__""")
SET = json.loads(r"""__SET__""")
OUT = "/kaggle/working/out"


def long_prompt(pipe, text, avoid, dev):
    import torch
    pieces = []
    for tok in (pipe.tokenizer, pipe.tokenizer_2):
        ids = [tok(x, add_special_tokens=False).input_ids for x in (text, avoid)]
        pieces.append([[i[k:k + 75] for k in range(0, max(len(i), 1), 75)] for i in ids])
    n = max(len(c) for enc in pieces for c in enc)
    out = []
    for side in (0, 1):
        hidden, pooled = [], None
        for enc, tok, chunks in zip((pipe.text_encoder, pipe.text_encoder_2), (pipe.tokenizer, pipe.tokenizer_2),
                                    (pieces[0][side], pieces[1][side])):
            parts = []
            for c in chunks + [[]] * (n - len(chunks)):
                ids = [tok.bos_token_id, *c, tok.eos_token_id]
                ids += [tok.pad_token_id] * (77 - len(ids))
                got = enc(torch.tensor([ids], device=dev), output_hidden_states=True)
                parts.append(got.hidden_states[-2])
                if enc is pipe.text_encoder_2 and pooled is None:
                    pooled = got[0]
            hidden.append(torch.cat(parts, dim=1))
        out.append((torch.cat(hidden, dim=-1).to(torch.float16), pooled.to(torch.float16)))
    (pos, pooled), (neg, neg_pooled) = out
    return dict(prompt_embeds=pos, negative_prompt_embeds=neg, pooled_prompt_embeds=pooled,
                negative_pooled_prompt_embeds=neg_pooled)


def load(dev):
    import torch
    from diffusers import (AutoencoderKL, DPMSolverMultistepScheduler, EulerDiscreteScheduler,
                           StableDiffusionXLImg2ImgPipeline, StableDiffusionXLPipeline)
    vae = AutoencoderKL.from_pretrained("madebyollin/sdxl-vae-fp16-fix", torch_dtype=torch.float16)
    pipe = StableDiffusionXLPipeline.from_pretrained(SET["model"], vae=vae, torch_dtype=torch.float16,
                                                     variant="fp16").to(dev)
    loras = [x.split(":") for x in SET["lora"].split(",") if x]
    for k, (repo, _, *file) in enumerate(loras):
        pipe.load_lora_weights(repo, weight_name=file[0] if file else None, adapter_name=f"l{k}")
    if loras:
        pipe.set_adapters([f"l{k}" for k in range(len(loras))], adapter_weights=[float(x[1]) for x in loras])
        pipe.fuse_lora()
        pipe.unload_lora_weights()
    cls = EulerDiscreteScheduler if SET["scheduler"] == "trailing" else DPMSolverMultistepScheduler
    extra = {"timestep_spacing": "trailing"} if SET["scheduler"] == "trailing" else {}
    pipe.scheduler = cls.from_config(pipe.scheduler.config, **extra)
    return pipe, StableDiffusionXLImg2ImgPipeline(**pipe.components)


def cutter(dev):
    """BiRefNet: the figure's mask, sharper on outlines and thin things (a sword, a spear) than the older models."""
    import torch
    from transformers import AutoModelForImageSegmentation
    net = AutoModelForImageSegmentation.from_pretrained("ZhengPeng7/BiRefNet", trust_remote_code=True)
    return net.to(dev).half().eval()


def mask_of(net, img, dev):
    import numpy as np, torch
    from PIL import Image
    x = np.asarray(img.convert("RGB").resize((1024, 1024), Image.BILINEAR), np.float32) / 255
    x = (x - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]
    x = torch.tensor(x.transpose(2, 0, 1)[None], dtype=torch.float16, device=dev)
    with torch.no_grad():
        m = net(x)[-1].sigmoid()[0, 0].float().cpu().numpy()
    return Image.fromarray((m * 255).astype("uint8")).resize(img.size, Image.BILINEAR)


LAMA = "https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt"


def lama(dev):
    """LaMa fills a hole from the picture around it and never invents a new figure, which a diffusion model does
    at the low guidance this look runs at (it painted children in the empty doorway)."""
    import torch
    path = torch.hub.get_dir() + "/big-lama.pt"
    os.makedirs(torch.hub.get_dir(), exist_ok=True)  # a fresh Kaggle machine has no hub folder yet
    if not os.path.exists(path):
        torch.hub.download_url_to_file(LAMA, path, progress=False)
    return torch.jit.load(path, map_location=dev).eval()


def repaint(fill, net, img, mask, emb, gen, dev):
    """The background behind the figure: LaMa paints the hole from what is around it, then the picture model goes
    over it lightly (strength 0.3) so the fill takes on the same drawn look. The mask grows a margin and more below
    (the feet and their shadow), the same way the Mac blends it in."""
    import cv2, numpy as np, torch
    from PIL import Image
    keep = (np.asarray(mask) > 127).astype("uint8")
    w = keep.shape[1]
    grow = cv2.dilate(keep, np.ones((max(3, w // 50),) * 2, np.uint8))
    grow = np.maximum(grow, cv2.dilate(grow, np.ones((max(3, w // 25), 1), np.uint8), anchor=(0, 0)))
    H, W = (keep.shape[0] + 7) // 8 * 8, (w + 7) // 8 * 8
    x = torch.tensor(np.asarray(img.convert("RGB").resize((W, H)), np.float32).transpose(2, 0, 1)[None] / 255, device=dev)
    m = torch.tensor(cv2.resize(grow, (W, H), interpolation=cv2.INTER_NEAREST)[None, None].astype(np.float32), device=dev)
    with torch.no_grad():
        out = net(x, m)[0].permute(1, 2, 0).clamp(0, 1).cpu().numpy()
    filled = Image.fromarray((out * 255).astype("uint8")).resize(img.size)
    hole = Image.fromarray(grow * 255)
    return fill(image=filled, mask_image=hole, strength=0.3, num_inference_steps=round(SET["steps"] / 0.3),
                guidance_scale=SET["cfg"], width=img.width, height=img.height, generator=gen, **emb).images[0]


LOADING = threading.Lock()  # diffusers loads through empty "meta" weights, which is not safe in two threads at once


def work(gpu, jobs):
    try:
        draw(gpu, jobs)
    except Exception:
        import traceback
        traceback.print_exc()


def draw(gpu, jobs):
    import torch
    from PIL import Image
    dev = f"cuda:{gpu}"
    with LOADING:
        pipe, redraw = load(dev)
        try:  # the cut out is extra: if its models fail to load, the pictures are still drawn
            net = cutter(dev) if any(j.get("cut") for j in jobs) else None
            painter = lama(dev) if net is not None else None
        except Exception as err:
            print("cut out off:", str(err)[:200], flush=True)
            net = painter = None
    from diffusers import StableDiffusionXLInpaintPipeline
    fill, plate_emb = StableDiffusionXLInpaintPipeline(**pipe.components), None
    w, h = SET["width"], SET["height"]
    for j in jobs:
        t = time.time()
        with torch.no_grad():
            emb = long_prompt(pipe, j["text"], j["avoid"], dev)
        gen = torch.Generator(dev).manual_seed(j["seed"])
        if j.get("src"):
            src = Image.open(f"{OUT}/{j['src']}.png").convert("RGB").resize((w, h), Image.LANCZOS)
            s = SET["strength"]
            img = redraw(image=src, strength=s, num_inference_steps=round(max(2, SET["steps"] - 1) / s),
                         guidance_scale=SET["cfg"], generator=gen, **emb).images[0]
        else:
            img = pipe(num_inference_steps=SET["steps"], guidance_scale=SET["cfg"], width=w, height=h,
                       generator=gen, **emb).images[0]
        img.save(f"{OUT}/{j['id']}.png")
        if j.get("cut") and net is not None:
            mask = mask_of(net, img, dev)
            share = (__import__("numpy").asarray(mask) > 127).mean()
            if 0.02 < share < 0.55:  # the Mac checks the shape again; this only saves repainting hopeless ones
                mask.save(f"{OUT}/{j['id']}.mask.png")
                if plate_emb is None:
                    with torch.no_grad():
                        plate_emb = long_prompt(pipe, SET["plate"], SET["plate_avoid"], dev)
                repaint(fill, painter, img, mask, plate_emb, torch.Generator(dev).manual_seed(j["seed"]), dev).save(
                    f"{OUT}/{j['id']}.plate.png")
        print(f"gpu{gpu} {j['id']} {time.time() - t:.1f}s", flush=True)


os.makedirs(OUT, exist_ok=True)
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "diffusers", "peft", "timm", "kornia", "einops"],
               check=True)
# the old torchao in Kaggle's image makes the new peft refuse to load a LoRA; nothing here needs torchao
subprocess.run([sys.executable, "-m", "pip", "uninstall", "-q", "-y", "torchao"])
import torch
from diffusers import StableDiffusionXLPipeline
StableDiffusionXLPipeline.download(SET["model"], variant="fp16", torch_dtype=torch.float16)  # once for both GPUs
gpus = max(1, torch.cuda.device_count())
# a picture and its pose frames stay on one GPU: the pose frames start from that picture
groups = [[j] + [m for m in JOBS if m.get("src") == j["id"]] for j in JOBS if not j.get("src")]
share = [sum(groups[g::gpus], []) for g in range(gpus)]
threads = [threading.Thread(target=work, args=(g, share[g])) for g in range(gpus)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print("done", len(os.listdir(OUT)), "of", len(JOBS))
'''


def kaggle():
    exe = Path(sys.executable).with_name("kaggle")
    return str(exe) if exe.exists() else shutil.which("kaggle")


def ready():
    return os.getenv("KAGGLE", "1") == "1" and TOKEN.exists() and kaggle() is not None


def user():
    return json.loads(TOKEN.read_text())["username"]


def cli(*args, timeout=300):
    return subprocess.run([kaggle(), *args], capture_output=True, text=True, timeout=timeout)


def status(ref):
    out = cli("kernels", "status", ref, timeout=60)
    text = (out.stdout + out.stderr).lower()
    for word in ("complete", "error", "cancel", "running", "queued"):
        if word in text:
            return word
    return text.strip()[-80:] or "unknown"


def pictures(todo, moves):
    """Draw [(prompt, seed, dst)] and pose frames [(prompt, seed, src, dst)] on Kaggle. Returns how many arrived."""
    if not todo or not ready():
        return 0
    ids = {dst: f"p{k:04d}" for k, (_, _, dst) in enumerate(todo)}
    cut = lambda p: os.getenv("MOTION", "1") == "1" and cutout.subject(p)
    jobs = [{"id": ids[dst], "seed": seed, "cut": cut(p), **dict(zip(("text", "avoid"), visuals.compose(p)))}
            for p, seed, dst in todo]
    jobs += [{"id": f"{ids[src]}m{k}", "src": ids[src], "seed": seed, "cut": cut(p),
              **dict(zip(("text", "avoid"), visuals.compose(p)))} for k, (p, seed, src, dst) in enumerate(moves) if src in ids]
    where = {j["id"]: dst for j, (_, _, dst) in zip(jobs, todo)}
    where.update({f"{ids[src]}m{k}": dst for k, (_, _, src, dst) in enumerate(moves) if src in ids})
    w, h = map(int, (os.getenv("SD_SIZE") or "1344x768").split("x"))
    settings = {"model": os.getenv("SD_MODEL", "lykon/dreamshaper-xl-lightning"), "lora": os.getenv("LORA", ""),
                "scheduler": os.getenv("SD_SCHEDULER", ""), "steps": int(os.getenv("SD_STEPS") or 6),
                "cfg": float(os.getenv("SD_CFG", "2")), "width": w, "height": h,
                "strength": float(os.getenv("POSE_STRENGTH", "0.5")),
                "plate": f"empty background, scenery only, nobody there, {visuals.style()[0]}",
                "plate_avoid": f"{visuals.style()[2]}, person, people, man, woman, child, figure, face, crowd, animal, horse"}

    shutil.rmtree(WORK, ignore_errors=True)
    ref = f"{user()}/{SLUG}"
    made, waiting = earlier(ref, jobs, where, settings)
    if not jobs:
        shutil.rmtree(WORK, ignore_errors=True)
        return made
    folder = WORK / "push"
    folder.mkdir(parents=True, exist_ok=True)
    code = SCRIPT.replace("__JOBS__", json.dumps(jobs)).replace("__SET__", json.dumps(settings))
    (folder / "pictures.py").write_text(code, encoding="utf-8")
    (folder / "kernel-metadata.json").write_text(json.dumps({
        "id": ref, "title": SLUG, "code_file": "pictures.py", "language": "python", "kernel_type": "script",
        "is_private": True, "enable_gpu": True, "enable_internet": True, "dataset_sources": [],
        "competition_sources": [], "kernel_sources": [], "machine_shape": "NvidiaTeslaT4"}))
    limit = min(3 * 3600, 900 + 30 * len(jobs))  # the session closes at this limit even if something hangs
    if waiting:
        print(f"  kaggle: the same {len(jobs)} pictures are already {waiting} on Kaggle, waiting for that run", flush=True)
    else:
        print(f"  kaggle: {len(jobs)} pictures go to the Kaggle GPU (time limit {limit // 60} min)", flush=True)
        push = cli("kernels", "push", "-p", str(folder), "-t", str(limit), "--accelerator", "NvidiaTeslaT4")
        if push.returncode:
            print(f"  kaggle: push failed, pictures stay on the Mac ({(push.stderr or push.stdout).strip()[-160:]})")
            return 0

    # a busy day can keep a run in Kaggle's GPU queue for hours; only the running time counts against the limit
    queue = float(os.getenv("KAGGLE_QUEUE_HOURS", "6")) * 3600
    start, ran, state, shown = time.time(), None, "queued", 0
    while True:
        time.sleep(30)
        try:
            state = status(ref)
        except subprocess.TimeoutExpired:
            continue
        if state in ("complete", "error", "cancel"):
            break
        ran = ran or (time.time() if state == "running" else None)
        if (ran and time.time() - ran > limit + 600) or (not ran and time.time() - start > queue):
            break
        if time.time() - shown > 300:
            print(f"  kaggle: {state}, {(time.time() - start) / 60:.0f} min", flush=True)
            shown = time.time()
    print(f"  kaggle: {state} after {(time.time() - start) / 60:.0f} min", flush=True)
    if waiting:
        made += earlier(ref, jobs, where, settings)[0]
        shutil.rmtree(WORK, ignore_errors=True)
        return made

    got = WORK / "got"
    got.mkdir()
    cli("kernels", "output", ref, "-p", str(got), "--force", timeout=1800)
    files = {p.stem: p for p in got.rglob("*.png")}
    drawn = 0
    for k, (jid, dst) in enumerate(where.items(), 1):
        if jid in files:
            arrive(got, files[jid], jid, dst)
            drawn += 1
            print(f"    image {k}/{len(where)} from kaggle", flush=True)
    log = next(got.rglob("*.log"), None)
    if log:  # kept for a look when something went wrong on Kaggle
        shutil.copy(log, "output/kaggle_last.log")
    if drawn < len(where):
        print(f"  kaggle: {drawn} of {len(where)} arrived, see output/kaggle_last.log")
    shutil.rmtree(WORK, ignore_errors=True)
    return made + drawn


def arrive(got, png, jid, dst):
    visuals.finish(Image.open(png), dst)
    mask, plate = next(got.rglob(f"{jid}.mask.png"), None), next(got.rglob(f"{jid}.plate.png"), None)
    if mask and plate:
        cutout.attach(dst, Image.open(mask), Image.open(plate))
    elif os.getenv("MOTION", "1") == "1":  # Kaggle found nothing to cut, so the Mac does not try either
        dst.with_suffix(".nofg").write_text("no figure on Kaggle")


def earlier(ref, jobs, where, settings):
    """The last Kaggle run of these same pictures (same words, same seed, same model): when it has finished, its
    pictures are downloaded instead of drawn again, and the matched jobs leave the lists; when it is still queued or
    running, nothing is pushed and the build waits for it. Returns (pictures taken, "queued"/"running" or None)."""
    try:
        state = status(ref)
        if state not in ("complete", "queued", "running"):
            return 0, None
        src, got = WORK / "prev", WORK / "prev_out"
        cli("kernels", "pull", ref, "-p", str(src))
        code = next(f for f in src.glob("*.py")).read_text(encoding="utf-8")  # the pulled file takes the kernel's name
        if json.loads(re.search(r'SET = json\.loads\(r"""(.*?)"""\)', code, re.S).group(1)) != settings:
            print("  kaggle: the last run used other settings, drawing again", flush=True)
            return 0, None
        old = re.search(r'JOBS = json\.loads\(r"""(.*?)"""\)', code, re.S)
        old = {(j.get("text"), j["seed"]): j["id"] for j in json.loads(old.group(1)) if "src" not in j}
    except Exception as err:
        print(f"  kaggle: last run not reused ({type(err).__name__})", flush=True)
        return 0, None
    used = {jid: old[(j.get("text"), j["seed"])] for j in jobs if "src" not in j
            and (jid := j["id"]) in where and (j.get("text"), j["seed"]) in old}
    if state != "complete":
        return 0, (state if len(used) == len(jobs) else None)
    if not used:
        return 0, None
    cli("kernels", "output", ref, "-p", str(got), "--force", timeout=1800)
    files = {p.stem: p for p in got.rglob("*.png")}
    made = 0
    for jid, oid in used.items():
        if oid in files and not any(j.get("src") == jid for j in jobs):  # a pose frame needs its picture in the run
            arrive(got, files[oid], oid, where.pop(jid))
            jobs[:] = [j for j in jobs if j["id"] != jid]
            made += 1
    print(f"  kaggle: {made} pictures taken from the last finished Kaggle run, {len(jobs)} left to draw", flush=True)
    log = next(got.rglob("*.log"), None)
    if log:
        shutil.copy(log, "output/kaggle_last.log")
    return made, None


def test():
    folder = Path("output/kaggle_test")
    folder.mkdir(parents=True, exist_ok=True)
    prompts = ["Wide shot of a lonely white lighthouse on a steep green island, grey stormy sea, 1900",
               "Wide shot of three lighthouse keepers in navy wool uniforms at the lighthouse door"]
    paths, todo, moves = visuals.pending(prompts, folder, [["the keeper lifts a lantern"], []])  # a pose frame too
    t = time.time()
    print("arrived:", pictures(todo, moves), f"in {(time.time() - t) / 60:.1f} min")


if __name__ == "__main__":
    from dotenv import load_dotenv
    os.chdir(Path(__file__).parent)
    load_dotenv() or load_dotenv(".env.example")
    for key, value in visuals.STYLES.get(os.getenv("STYLE", ""), ("", "", {}))[2].items():
        os.environ[key] = value
    test() if sys.argv[1:] == ["test"] else sys.exit(__doc__.strip())
