"""Life inside a still picture, the way the reference cartoon moves: the character breathes and sways, the background
sits a little further back than the character (parallax), and small things drift through the air (birds,
butterflies, dust, leaves, embers, rain, snow), picked from what the scene shows. A chapter can open with a sparkle.

The character is cut out once per picture and the hole it leaves is repainted (cutout.py), so the two layers can move
apart without a ghost. Without a cut out the picture just gets the camera move.
"""
import math
import os
import re
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw

AIR = [  # first match wins
    ("smoke", r"\b(smoke|smoking|chimneys?|cannons?|ruins|ash|ashes|burnt|burned|volcano|steam|foundry|musket|gunpowder)\b"),
    ("embers", r"\b(fire|flames?|burning|torch|candle|forge|battle|siege|blaze)\b"),
    ("rain", r"\b(rain|storm|downpour|monsoon)\b"),
    ("snow", r"\b(snow|winter|blizzard|frozen|ice)\b"),
    ("leaves", r"\b(forest|woods|autumn|jungle|trees?)\b"),
    ("butterflies", r"\b(meadow|garden|field|flowers?|spring|summer|countryside)\b"),
    ("dust", r"\b(room|desk|hall|church|chapel|interior|inside|cave|library|cell|council|court|palace)\b"),
    ("birds", r"\b(sky|street|square|village|town|city|river|sea|harbou?r|mountains?|road|market|bridge|fields?)\b"),
]


def air(text):
    return next((kind for kind, pat in AIR if re.search(pat, (text or "").lower())), "dust")


@lru_cache(1)
def session():
    from rembg import new_session
    return new_session(os.getenv("MASK_MODEL", "birefnet-general"))


def prepare(path, prompt=None):
    """Cut the character out of a picture once (cutout.py): <name>.fg.png and <name>.bg.jpg, or <name>.nofg with the
    reason. Pictures from Kaggle arrive already cut; this is the Mac path for the rest."""
    import cutout
    fg_path = path.with_suffix(".fg.png")
    if os.getenv("MOTION", "1") != "1" or fg_path.exists() or path.with_suffix(".nofg").exists():
        return
    if prompt is not None and not cutout.subject(prompt):
        path.with_suffix(".nofg").write_text("nothing in the prompt to move")
        return
    try:
        import cv2  # noqa: F401
        from rembg import remove
    except ImportError:
        return
    img = Image.open(path).convert("RGB")
    alpha = np.asarray(remove(img, session=session(), only_mask=True), np.float32) / 255
    cutout.save(path, np.asarray(img), alpha)


def layers(path):
    fg_path, bg_path = path.with_suffix(".fg.png"), path.with_suffix(".bg.jpg")
    if os.getenv("MOTION", "1") != "1" or not fg_path.exists():
        return None
    fg = Image.open(fg_path).convert("RGBA")
    box = fg.getbbox()
    pad = round(fg.width * 0.03)
    box = (max(0, box[0] - pad), max(0, box[1] - pad), min(fg.width, box[2] + pad), min(fg.height, box[3] + pad))
    return Image.open(bg_path).convert("RGB"), fg.crop(box), box


def draw_layers(bg, fg, box, w, h, sc, ox, oy, t):
    """Camera: output pixel (x, y) shows picture pixel (sc*x + ox, sc*y + oy). The character sits closer than the
    background so it moves 1.2% more with the camera (more would show the painted over edge), breathes (0.6%) and
    sways (0.4 degrees) around its feet. Only rigid moves: the figure is never bent, so a face, an eye or a neck keeps
    its drawn shape (the old puppet warp bent the head and stretched the neck)."""
    frame = bg.transform((w, h), Image.AFFINE, (sc, 0, ox, 0, sc, oy), Image.BICUBIC)
    near = sc / 1.012
    ox, oy, sc = ox + (sc - near) * w / 2, oy + (sc - near) * h / 2, near
    s = 1 + 0.006 * math.sin(2 * math.pi * t / 3.2)
    a = math.radians(0.4 * math.sin(2 * math.pi * t / 4.1))
    ax, ay = (box[0] + box[2]) / 2, box[3]  # feet
    c, sn = math.cos(-a) / s, math.sin(-a) / s
    # picture point q = A + R(-a)/s * (camera(out) - A), written as one affine map from output pixels
    m = np.array([[c * sc, -sn * sc], [sn * sc, c * sc]])
    off = np.array([ax, ay]) + np.array([[c, -sn], [sn, c]]) @ (np.array([ox, oy]) - np.array([ax, ay]))
    inv = np.linalg.inv(m)
    corners = [inv @ (np.array(p) - off) for p in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3]))]
    x0, y0 = (max(0, math.floor(min(p[i] for p in corners))) for i in (0, 1))
    x1, y1 = min(w, math.ceil(max(p[0] for p in corners))), min(h, math.ceil(max(p[1] for p in corners)))
    if x1 > x0 and y1 > y0:
        o = m @ np.array([x0, y0]) + off - np.array(box[:2])
        part = fg.transform((x1 - x0, y1 - y0), Image.AFFINE, (m[0, 0], m[0, 1], o[0], m[1, 0], m[1, 1], o[1]),
                            Image.BICUBIC)
        frame.paste(part, (x0, y0), part)
    return frame


class Air:
    """Things drifting through the scene, drawn fresh each frame so they move smoothly."""

    def __init__(self, kind, w, h, seed):
        self.kind, self.w, self.h = kind, w, h
        rng = np.random.default_rng(seed)
        n = {"birds": 5, "butterflies": 6, "dust": 40, "leaves": 14, "embers": 30, "rain": 220, "snow": 90, "smoke": 14}[kind]
        self.p = rng.random((n, 6))

    def draw(self, img, t):
        d = ImageDraw.Draw(img, "RGBA")
        w, h = self.w, self.h
        for x, y, sp, ph, sz, tone in self.p:
            if self.kind == "birds":
                px = (x * w + t * w * (0.04 + 0.04 * sp)) % (w * 1.2) - w * 0.1
                py = y * h * 0.35 + math.sin(t * 1.3 + ph * 6) * h * 0.01
                flap = math.sin(t * 9 + ph * 6) * h * 0.008
                r = h * (0.012 + 0.008 * sz)
                d.line([(px - r, py - flap), (px, py), (px + r, py - flap)], fill=(40, 40, 45, 200), width=max(2, h // 400))
            elif self.kind == "butterflies":
                px = (x * w + math.sin(t * 0.6 + ph * 6) * w * 0.05 + t * w * 0.015) % w
                py = h * (0.35 + 0.45 * y) + math.sin(t * 1.7 + ph * 9) * h * 0.03
                r = h * (0.016 + 0.008 * sz) * (0.35 + 0.65 * abs(math.sin(t * 11 + ph * 6)))
                col = ((236, 160, 60), (230, 120, 140), (120, 160, 220))[int(tone * 3)]
                d.ellipse([px - r * 2, py - r, px, py + r], fill=(*col, 230))
                d.ellipse([px, py - r, px + r * 2, py + r], fill=(*col, 230))
            elif self.kind == "dust":
                px = (x * w + math.sin(t * 0.3 + ph * 6) * w * 0.02 + t * w * 0.004) % w
                py = (y * h - t * h * 0.006 * sp) % h
                r = h * 0.0015 * (1 + sz)
                d.ellipse([px - r, py - r, px + r, py + r], fill=(255, 245, 220, int(60 + 80 * tone)))
            elif self.kind == "leaves":
                px = (x * w + math.sin(t * 0.8 + ph * 6) * w * 0.04 + t * w * 0.02) % w
                py = (y * h + t * h * (0.05 + 0.05 * sp)) % h
                r = h * (0.006 + 0.004 * sz)
                tilt = math.sin(t * 2 + ph * 6) * r
                d.ellipse([px - r, py - r * 0.5 + tilt * 0.2, px + r, py + r * 0.5], fill=(180, 110, 40, 220))
            elif self.kind == "embers":
                px = (x * w + math.sin(t * 1.5 + ph * 6) * w * 0.01) % w
                py = h - (y * h + t * h * (0.06 + 0.08 * sp)) % h
                r = h * 0.0018 * (1 + sz)
                d.ellipse([px - r, py - r, px + r, py + r], fill=(255, 150 + int(80 * tone), 60, int(120 + 120 * tone)))
            elif self.kind == "rain":
                px = (x * w + t * w * 0.05) % w
                py = (y * h + t * h * (1.2 + 0.6 * sp)) % h
                d.line([(px, py), (px - h * 0.004, py + h * 0.03)], fill=(200, 210, 230, 90), width=1)
            elif self.kind == "smoke":  # soft puffs that rise, grow and fade, drawn as rings to look blurred
                age = (t / (5 + 3 * sp) + ph) % 1
                px = x * w + math.sin(t * 0.4 + ph * 6) * w * 0.02 + age * w * 0.05
                py = h * (0.98 - 0.75 * age) * (0.7 + 0.3 * y)
                r = h * (0.03 + 0.12 * age) * (0.7 + 0.6 * sz)
                fade = (1 - age) * min(1.0, age * 6)
                for k in range(7):
                    rr = r * (1 - k * 0.12)
                    d.ellipse([px - rr, py - rr * 0.8, px + rr, py + rr * 0.8], fill=(205, 205, 210, int(11 * fade)))
            elif self.kind == "snow":
                px = (x * w + math.sin(t * 0.7 + ph * 6) * w * 0.02) % w
                py = (y * h + t * h * (0.04 + 0.04 * sp)) % h
                r = h * 0.002 * (1 + sz)
                d.ellipse([px - r, py - r, px + r, py + r], fill=(255, 255, 255, 200))
        return img


def sparkle(img, t):
    """The cartoon's scene change: a glowing ring opens from the middle with a few twinkling stars (first 0.7 s)."""
    if t > 0.7:
        return img
    w, h = img.size
    d = ImageDraw.Draw(img, "RGBA")
    p = t / 0.7
    r = (0.05 + 0.75 * (1 - (1 - p) ** 2)) * w
    d.rectangle([0, 0, w, h], fill=(255, 250, 230, int(110 * (1 - p) ** 3)))  # a soft flash as the scene opens
    for k in range(8):
        rr = r + (k - 3) * h * 0.007
        d.ellipse([w / 2 - rr, h / 2 - rr * 0.8, w / 2 + rr, h / 2 + rr * 0.8],
                  outline=(255, 244, 200, int(230 * (1 - p) / (1 + abs(k - 3)))), width=max(3, h // 160))
    rng = np.random.default_rng(3)
    for sx, sy, ph in rng.random((14, 3)):
        a = max(0.0, math.sin(math.pi * (p * 2 + ph))) * (1 - p)
        x, y, s = sx * w, sy * h, h * 0.02 * a
        d.polygon([(x, y - s * 2), (x + s * 0.5, y), (x, y + s * 2), (x - s * 0.5, y)], fill=(255, 255, 240, int(230 * a)))
        d.polygon([(x - s * 2, y), (x, y + s * 0.5), (x + s * 2, y), (x, y - s * 0.5)], fill=(255, 255, 240, int(230 * a)))
    return img
