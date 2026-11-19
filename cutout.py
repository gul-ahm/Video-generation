"""Cutting the character (or the object that should move) out of a picture, and the background it leaves behind.

What was wrong before: one salient object model (isnet) cut whatever stood out, a bright road as readily as a rider;
the hole was smeared shut with OpenCV inpainting; the edge was a plain threshold with a halo of the old background.
Now:
  1. only pictures whose prompt has someone or something to move get cut (an empty landscape stays one layer)
  2. BiRefNet cuts it (on Kaggle next to the picture, else rembg's birefnet-general on the Mac)
  3. a cut out that does not look like a figure is refused: too small or too big, a thin sprawling shape (a road,
     a river), or one that runs along the frame's edges
  4. the edge: small islands dropped, holes filled, a guided filter snaps the soft edge to the picture's own lines,
     and the edge colours are re-estimated so no old background shines through (pymatting)
  5. the hole is painted by the picture model itself on Kaggle (SDXL inpainting in the same look), else OpenCV
"""
import re

import numpy as np
from PIL import Image, ImageOps

MOVES = (r"\b(men|man|woman|women|people|crowd|army|armies|soldiers?|warriors?|riders?|horsem[ae]n|cavalry|archers?|"
         r"knights?|kings?|queens?|emperors?|sultans?|caliphs?|commanders?|generals?|captains?|sailors?|crew|keepers?|"
         r"officers?|officials?|priests?|monks?|merchants?|farmers?|figures?|travell?ers?|guards?|messengers?|"
         r"boy|girl|child|children|horses?|camels?|elephants?|dogs?|wolf|wolves|lions?|birds?|ships?|boats?|"
         r"carts?|wagons?|chariots?|lanterns?|he|she|his|her)\b")
EMPTY = r"\b(empty|deserted|abandoned|no one|nobody|without people|unmanned|landscape|aerial|map)\b"


def subject(prompt):
    """Is there someone or something in this picture worth moving on its own layer?"""
    p = (prompt or "").lower()
    return bool(re.search(MOVES, p)) and not re.search(EMPTY, p)


def clean(alpha):
    """Drop specks under 0.15% of the frame and fill holes inside the figure. alpha: float 0..1."""
    import cv2
    solid = (alpha > 0.5).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(solid)
    keep = np.zeros_like(solid)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= 0.0015 * solid.size:
            keep[lab == i] = 1
    flood = np.pad(keep, 1)  # a zero border, so the fill starts outside the figure wherever it stands
    cv2.floodFill(flood, None, (0, 0), 1)
    keep |= 1 - flood[1:-1, 1:-1]  # what the outside fill could not reach is a hole inside the figure
    near = cv2.dilate(keep, np.ones((5, 5), np.uint8))
    return np.where(keep, np.maximum(alpha, 0.5), alpha * near).astype(np.float32), keep


def good(keep):
    """A cut out that looks like figures, not like a road or the sky. keep: the cleaned 0/1 mask."""
    import cv2
    share = keep.mean()
    if not 0.02 < share < 0.55:
        return False, f"covers {share:.0%} of the picture"
    h, w = keep.shape
    border = np.concatenate([keep[0], keep[:, 0], keep[:, -1]])  # top and sides; feet may touch the bottom
    if border.mean() > 0.15:
        return False, "runs along the edges of the frame"
    n, lab, stats, _ = cv2.connectedComponentsWithStats(keep)
    big = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    contours, _ = cv2.findContours((lab == big).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    hull = cv2.contourArea(cv2.convexHull(max(contours, key=cv2.contourArea)))
    area = stats[big, cv2.CC_STAT_AREA]
    solidity, fill = area / max(hull, 1), area / max(stats[big, cv2.CC_STAT_WIDTH] * stats[big, cv2.CC_STAT_HEIGHT], 1)
    if solidity < 0.45 and fill < 0.3:  # a figure fills its outline; a road or a river snakes through an empty box
        return False, f"a thin sprawling shape (solidity {solidity:.2f}, fills {fill:.0%} of its box)"
    if stats[big, cv2.CC_STAT_HEIGHT] < 0.08 * h:
        return False, "the figure is too small to move"
    if n - 1 > 8:
        return False, f"{n - 1} separate pieces"
    return True, ""


def guided(guide, src, r=6, eps=1e-3):
    """He's guided filter: src (the soft alpha) follows the edges of guide (the picture, grey 0..1)."""
    import cv2
    box = lambda x: cv2.boxFilter(x, -1, (2 * r + 1, 2 * r + 1))
    mi, mp = box(guide), box(src)
    a = (box(guide * src) - mi * mp) / (box(guide * guide) - mi * mi + eps)
    b = mp - a * mi
    return np.clip(box(a) * guide + box(b), 0, 1)


def colours(img, alpha):
    """Edge colours without the old background in them, worked out only around the figure."""
    try:
        from pymatting import estimate_foreground_ml
    except ImportError:
        return img
    ys, xs = np.nonzero(alpha > 0.02)
    if not len(ys):
        return img
    y0, y1, x0, x1 = max(0, ys.min() - 8), ys.max() + 9, max(0, xs.min() - 8), xs.max() + 9
    out = img.astype(np.float64) / 255
    out[y0:y1, x0:x1] = estimate_foreground_ml(out[y0:y1, x0:x1], alpha[y0:y1, x0:x1].astype(np.float64))
    return (np.clip(out, 0, 1) * 255).astype(np.uint8)


def hole(keep):
    """The part of the background to repaint: the figure, a margin for its halo, and a bit more below for its feet."""
    import cv2
    w = keep.shape[1]
    grow = cv2.dilate(keep, np.ones((max(3, w // 60),) * 2, np.uint8))
    down = cv2.dilate(grow, np.ones((max(3, w // 30), 1), np.uint8), anchor=(0, 0))
    return np.maximum(grow, down)


def save(path, img, alpha, plate=None):
    """Refine and write <name>.fg.png and <name>.bg.jpg next to the picture, or <name>.nofg with the reason.
    img: final picture (RGB array), alpha: float 0..1 at the same size, plate: repainted background or None."""
    import cv2
    alpha, keep = clean(alpha)
    ok, why = good(keep)
    if not ok:
        path.with_suffix(".nofg").write_text(why)
        return False
    grey = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
    alpha = guided(grey, alpha) * cv2.dilate(keep, np.ones((9, 9), np.uint8))
    fg = np.dstack([colours(img, alpha), (alpha * 255).astype(np.uint8)])
    gap = hole(keep)
    if plate is None:  # no repaint from Kaggle: OpenCV at half size, then blended in softly
        small = cv2.resize(img, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        g = cv2.resize(gap * 255, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_NEAREST)
        plate = cv2.resize(cv2.inpaint(small, g, 7, cv2.INPAINT_NS), (img.shape[1], img.shape[0]),
                           interpolation=cv2.INTER_CUBIC)
    soft = cv2.GaussianBlur(gap.astype(np.float32), (0, 0), max(2, img.shape[1] / 400))[..., None]
    bg = (plate * soft + img * (1 - soft)).astype(np.uint8)
    Image.fromarray(fg, "RGBA").save(path.with_suffix(".fg.png"))
    Image.fromarray(bg).save(path.with_suffix(".bg.jpg"), quality=93)
    return True


def attach(dst, mask, plate):
    """Kaggle's mask and repainted background for a finished picture: brought to its framing, then saved."""
    import visuals
    img = Image.open(dst).convert("RGB")
    fit = lambda x: ImageOps.fit(x, img.size, Image.LANCZOS)  # same crop as visuals.frame, at any scale
    alpha = np.asarray(fit(mask.convert("L")), np.float32) / 255
    bg = np.asarray(fit(visuals.paper(plate.convert("RGB"))))
    return save(dst, np.asarray(img), alpha, bg)
