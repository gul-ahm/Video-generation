"""HyperFrames cards (assets/hyperframes, from the hyperframes-video-pipeline kit) rendered into our video.

Every card is a small web page with one paused GSAP timeline. A headless Chromium opens it, puts our text into its
named slots (data-slot), seeks the timeline frame by frame and saves each frame as a PNG: tier1 cards fill the
screen, tier2 cards (lower thirds, labels) come out transparent and sit over the moving picture. Text in a card
that is not a slot is example copy from the kit, so it is blanked; nothing on screen comes from outside the script.

python hf.py sheet      contact sheet of every card (assets/hyperframes/sheet_<style>.jpg) to pick from
"""
import hashlib
import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path("assets/hyperframes")
FPS = 15  # the Vox style moves on twos; every frame is shown twice in the 30 fps video
GSAP = re.compile(r'<script src="[^"]*gsap[^"]*"></script>')

FILL = """
(slots) => {
  const root = document.querySelector('[data-composition-id]');
  // a slot the script gave nothing for loses the kit's example text ("SOURCE: ATTENTION STUDY 2025", "DR. REYES")
  for (const el of root.querySelectorAll('[data-slot]'))
    if (!(el.dataset.slot in slots)) el.innerHTML = '';
  for (const [name, value] of Object.entries(slots)) {
    for (const el of root.querySelectorAll(`[data-slot="${name}"]`)) {
      if (typeof value === 'string' && /\\.(jpe?g|png)$/i.test(value)) {
        el.style.backgroundImage = `url("${value}")`;
        el.style.backgroundSize = 'cover';
        el.style.backgroundPosition = 'center';
        el.innerHTML = '';
      } else {
        el.innerHTML = value;
      }
    }
  }
  // example copy that is not a slot goes, so a card never shows words the script did not say
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const blank = [];
  while (walk.nextNode()) {
    const n = walk.currentNode;
    if (/[A-Za-z0-9]/.test(n.textContent) && !n.parentElement.closest('[data-slot]')) blank.push(n);
  }
  blank.forEach(n => { n.textContent = ''; });
  for (const el of root.querySelectorAll('[data-slot]'))
    if (!(el.textContent || '').trim() && !el.style.backgroundImage) el.style.visibility = 'hidden';
}
"""


SEEK = "([id, t]) => { const all = window.__timelines || {}; (all[id] || Object.values(all)[0]).seek(t, false); }"


@lru_cache(1)
def catalog():
    """Every card of every style: id -> {file, tier, purpose, slots, ...}."""
    cards = {}
    for style in sorted(ROOT.glob("*/style.json")):
        for c in json.loads(style.read_text(encoding="utf-8"))["cards"]:
            cards[c["id"]] = {**c, "file": str(style.parent / c["file"])}
    return cards


LOWER = sorted(c for c in catalog() if ".t2.lower-third." in c and c.startswith("vox"))  # name, role
LABELS = sorted(c for c in catalog() if ".t2.label." in c and c.startswith("vox"))  # term, detail


# modern looking cards (a TV, a price tag, a receipt, a news ticker, a schedule) do not fit a history film
SKIP = {"t1.section.crt", "t1.section.ticker", "t1.stat.price-tag", "t1.stat.receipt", "t1.stat.ticker-tape",
        "t1.overview.board", "t1.overview.toc", "t1.overview.agenda-bars"}


def menu():
    """The full screen cards Gemini can choose from: Vox explainer tier1, with their text slots."""
    rows = []
    for cid, c in catalog().items():
        if cid.startswith("vox") and c["tier"] == "tier1" and cid.split(".", 1)[1] not in SKIP:
            text = [f"{s['name']}<={s.get('maxChars', 40)}" for s in c["slots"] if s.get("type") != "image"]
            rows.append(f"{cid.split('.', 1)[1]}: {', '.join(text)}")
    return "\n".join(rows)


def resolve(card):
    """Full card id from what the model wrote ("t1.stat.calendar" or "vox-explainer.t1.stat.calendar")."""
    return card if card in catalog() else f"vox-explainer.{card}" if f"vox-explainer.{card}" in catalog() else None


def filled(card, slots):
    """Share of the card's text slots the model filled; a half empty card (a comparison with one side blank) is
    worse than a caption."""
    text = [s["name"] for s in catalog()[card]["slots"] if s.get("type") != "image" and s["name"] != "source"]
    return sum(bool(str(slots.get(n, "")).strip()) for n in text) / max(len(text), 1)


def image_slots(card):
    return [s["name"] for s in catalog()[card]["slots"] if s.get("type") == "image"]


_browser = None


def fonts(route):
    """Google Fonts are fetched once and kept in assets/hyperframes/fonts, so later videos render offline.
    HF_FONTS=0 skips them (the cards then fall back to system fonts)."""
    if os.getenv("HF_FONTS", "1") != "1":
        return route.abort()
    f = ROOT / "fonts" / hashlib.md5(route.request.url.encode()).hexdigest()
    if not f.exists():
        try:
            got = route.fetch(timeout=15000)
        except Exception:
            return route.abort()
        f.parent.mkdir(exist_ok=True)
        f.write_bytes(got.body())
        f.with_suffix(".type").write_text(got.headers.get("content-type", "text/css"))
    route.fulfill(body=f.read_bytes(), content_type=f.with_suffix(".type").read_text())


def page():
    global _browser
    if _browser is None:
        from playwright.sync_api import sync_playwright
        _browser = sync_playwright().start().chromium.launch()
    p = _browser.new_page(viewport={"width": 1920, "height": 1080})
    gsap = (ROOT / "gsap.min.js").read_text(encoding="utf-8")
    p.route(re.compile(r".*gsap.*\.js$"), lambda r: r.fulfill(body=gsap, content_type="text/javascript"))
    p.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"), fonts)
    return p


def render(card, slots, seconds, out, size=(1920, 1080)):
    """Frames of one card into folder out (f0000.png ...), at FPS. Returns the frame count; kept for later runs."""
    spec = catalog()[card]
    n = max(1, round(seconds * FPS))
    if len(list(out.glob("f*.png"))) >= n:
        return n
    out.mkdir(parents=True, exist_ok=True)
    p = page()
    try:
        p.goto(Path(spec["file"]).resolve().as_uri(), wait_until="load")
        p.evaluate(FILL, {k: (Path(v).resolve().as_uri() if re.search(r"\.(jpe?g|png)$", str(v), re.I) else v)
                          for k, v in slots.items()})
        p.evaluate("document.fonts.ready.then(() => true)")
        cid = p.evaluate("document.querySelector('[data-composition-id]').dataset.compositionId")
        alpha = spec["tier"] != "tier1"
        for k in range(n):
            p.evaluate(SEEK, [cid, k / FPS])
            shot = out / f"f{k:04d}.png"
            p.screenshot(path=str(shot), omit_background=alpha)
            if size != (1920, 1080):
                Image.open(shot).resize(size, Image.LANCZOS).save(shot)
    finally:
        p.close()
    return n


def frames(folder, k):
    """Frame k of a 30 fps video from the 15 fps card frames (held on the last one)."""
    files = sorted(Path(folder).glob("f*.png"))
    return Image.open(files[min(k // 2, len(files) - 1)])


@lru_cache(64)
def frame(folder, k, size, step=2):
    """Frame k as RGBA at the video size, kept in memory: the last frame holds for most of a shot. step 2 for the
    15 fps kit cards, 1 for the 30 fps HyperFrames CLI frames of fx.py."""
    files = sorted(Path(folder).glob("f*.png"))
    img = Image.open(files[min(k // step, len(files) - 1)]).convert("RGBA")
    return img if img.size == size else img.resize(size, Image.LANCZOS)


def sheet():
    """One poster per card (at 2.5 s, with the kit's example text kept) to choose which cards fit our videos."""
    for style in sorted(ROOT.glob("*/style.json")):
        ids = [c["id"] for c in json.loads(style.read_text())["cards"]]
        w, h, cols = 384, 216, 6
        out = Image.new("RGB", (w * cols, (h + 24) * ((len(ids) + cols - 1) // cols)), "white")
        p = page()
        for i, cid in enumerate(ids):
            spec = catalog()[cid]
            p.goto(Path(spec["file"]).resolve().as_uri(), wait_until="load")
            comp = p.evaluate("document.querySelector('[data-composition-id]').dataset.compositionId")
            p.evaluate(SEEK, [comp, 2.5])
            p.screenshot(path="/tmp/hf_poster.png")
            x, y = (i % cols) * w, (i // cols) * (h + 24)
            out.paste(Image.open("/tmp/hf_poster.png").convert("RGB").resize((w, h)), (x, y))
            ImageDraw.Draw(out).text((x + 4, y + h + 4), cid.split(".", 1)[1], fill="black")
        p.close()
        out.save(ROOT / f"sheet_{style.parent.name}.jpg", quality=80)
        print(ROOT / f"sheet_{style.parent.name}.jpg")


if __name__ == "__main__":
    if sys.argv[1:] == ["sheet"]:
        sheet()
    else:
        sys.exit(__doc__.strip())
