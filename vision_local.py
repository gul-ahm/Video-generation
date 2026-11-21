"""Pictures looked at by an open vision model on this machine's GPUs, so the picture check needs no API calls.

    python vision_local.py todo.json cache.json

todo.json: [{"path", "md5", "question"}]. The answer for each picture goes into cache.json (name -> md5, answer) the
moment it is made, so a run that stops keeps every picture it had seen and the next run starts after them. Run in its
own process: its GPU memory is free again for the redraws when it ends. VISION_LOCAL_MODEL picks the model;
float32 split over both T4s, because Qwen2.5-VL in float16 can overflow and answer with rubbish.
"""
import json
import os
import sys
import time
from pathlib import Path


def main(todo, cache):
    import torch
    from PIL import Image
    from transformers import AutoModelForImageTextToText, AutoProcessor
    name = os.getenv("VISION_LOCAL_MODEL", "Qwen/Qwen2.5-VL-3B-Instruct")
    dtype = getattr(torch, os.getenv("VISION_LOCAL_DTYPE", "float32"))
    proc = AutoProcessor.from_pretrained(name)
    model = AutoModelForImageTextToText.from_pretrained(name, torch_dtype=dtype, device_map="auto").eval()
    seen = json.loads(cache.read_text()) if cache.exists() else {}
    items = json.loads(todo.read_text())
    t0, bad = time.time(), 0
    for k, item in enumerate(items, 1):
        msgs = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": item["question"]}]}]
        text = proc.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
        img = Image.open(item["path"]).convert("RGB")
        img.thumbnail((768, 768))  # about 600 picture tokens: enough to see a cannon, quick on a T4
        x = proc(text=[text], images=[img], return_tensors="pt").to(model.device)
        with torch.no_grad():
            ids = model.generate(**x, max_new_tokens=160, do_sample=False)
        answer = proc.batch_decode(ids[:, x["input_ids"].shape[1]:], skip_special_tokens=True)[0].strip()
        if "{" not in answer:  # no JSON at all: the model is not fit for this, Gemini takes the rest
            bad += 1
            if bad >= 3 and bad == k:
                raise SystemExit(f"{name} gave no JSON for the first pictures: {answer[:120]}")
            continue
        seen[Path(item["path"]).name] = {"md5": item["md5"], "answer": answer, "by": name.split("/")[-1]}
        cache.write_text(json.dumps(seen, ensure_ascii=False))
        if k % 10 == 0 or k == len(items):
            print(f"  vision: {k} of {len(items)} looked at on the GPU, {(time.time() - t0) / k:.1f}s each", flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
