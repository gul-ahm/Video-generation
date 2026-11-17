"""On screen graphics, drawn frame by frame with Pillow: fact captions over pictures, a title over a picture, and full
screen cards (number, list, bar chart, map, document on a desk, timeline, compare, quote, stamp, calendar) and a torn
paper headline over a picture.

Timing follows hyperframes' MOTION_PHILOSOPHY (every element eases in within 0.3 to 0.6 s, the next one follows
0.3 s later, nothing moves after it lands except a slow push) and huashu-art-motion's camera rules (slow push on
cards, no movement on readable text). Colours copy modern history explainers: cream infographic cards, navy maps
with a green country and gold routes, white condensed captions with a gold line under them.
"""
import json
import math
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

import visuals

FPS = 30
CREAM, INK, GOLD, RED = (236, 233, 225), (34, 34, 38), (232, 182, 44), (196, 40, 40)
NAVY, LAND, EDGE, GREEN, WHITE = (23, 33, 52), (40, 54, 78), (58, 74, 102), (62, 140, 80), (255, 255, 255)


def ease(t):
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 3


@lru_cache(None)
def font(px, title=True):
    try:
        return ImageFont.truetype("assets/fonts/BebasNeue-Regular.ttf" if title else "assets/fonts/Oswald.ttf", px)
    except OSError:
        return visuals.font(px)


def shadowed(size, items, blur=6):
    """items: [(xy, text, font, fill)]. White text with a soft dark shadow, as one RGBA layer."""
    layer = Image.new("RGBA", size)
    shade = Image.new("RGBA", size)
    for xy, text, f, fill in items:
        ImageDraw.Draw(shade).text((xy[0] + 3, xy[1] + 3), text, font=f, fill=(0, 0, 0, 200))
        ImageDraw.Draw(layer).text(xy, text, font=f, fill=fill)
    return Image.alpha_composite(shade.filter(ImageFilter.GaussianBlur(blur)), layer)


def overlay(spec, w, h):
    """Caption (bottom left) or title (centre) as an RGBA layer plus where it sits."""
    title, sub = str(spec.get("title", "")).upper(), str(spec.get("sub", "")).upper()
    if spec.get("kind") == "headline":
        return torn_strip(str(spec.get("title") or spec.get("text", "")).upper(), w, h)
    if spec.get("kind") == "title":
        big, small = font(h // 5), font(h // 26, False)
        tw, sw = big.getlength(title), small.getlength(sub)
        layer = shadowed((w, h), [(((w - tw) / 2, h * 0.36), title, big, GOLD), (((w - sw) / 2, h * 0.58), sub, small, WHITE)], 10)
    else:
        big, small = font(h // 10), font(h // 22, False)
        x, y = w * 0.045, h * 0.76 - (h * 0.04 if sub else 0)
        layer = shadowed((w, h), [((x, y), title, big, WHITE), ((x, y + h * 0.1), sub, small, GOLD)][:2 if sub else 1])
    box = layer.getbbox() or (0, 0, 1, 1)
    return layer.crop(box), box[:2]


def torn_strip(text, w, h):
    """huashu's paper cut out: a strip of cream paper with torn top and bottom edges, black condensed text, turned a
    little, low in the middle of the picture."""
    f = font(h // 9)
    tw = f.getlength(text)
    sw, sh = round(tw + w * 0.08), round(h * 0.16)
    rng = np.random.default_rng(len(text))
    top = h * 0.012 * rng.random(sw // 6 + 2)
    edge = [(x * 6, top[x]) for x in range(sw // 6 + 2)] + [(x * 6, sh - top[-1 - x]) for x in range(sw // 6 + 1, -1, -1)]
    strip = Image.new("RGBA", (sw, sh))
    ImageDraw.Draw(strip).polygon(edge, fill=(242, 236, 220, 255))
    ImageDraw.Draw(strip).text(((sw - tw) / 2, sh * 0.12), text, font=f, fill=INK)
    strip = strip.rotate(-2, Image.BICUBIC, expand=True)
    layer = Image.new("RGBA", (w, h))
    shade = Image.new("RGBA", strip.size, (0, 0, 0, 0))
    shade.paste((0, 0, 0, 150), (0, 0), strip.getchannel("A"))
    x, y = round((w - strip.width) / 2), round(h * 0.68)
    layer.alpha_composite(shade.filter(ImageFilter.GaussianBlur(8)), (x + 6, y + 8))
    layer.alpha_composite(strip, (x, y))
    box = layer.getbbox() or (0, 0, 1, 1)
    return layer.crop(box), box[:2]


def ring(d, box, p, width):
    """huashu's red marker circle, drawn by hand: it starts at the top right, goes round once and overshoots a bit."""
    if p <= 0:
        return
    p = min(p, 1.0)
    x0, y0, x1, y1 = box
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    pts = []
    for u in np.linspace(0, 1.12 * p, 80):
        a = -0.6 + u * 2 * math.pi
        wob = 1 + 0.05 * math.sin(u * 9) + 0.06 * u
        pts.append((cx + rx * wob * math.cos(a), cy + ry * wob * math.sin(a)))
    d.line(pts, fill=RED, width=width, joint="curve")


def draw_overlay(frame, layer, at, n, frames):
    """Rise and fade in over 0.35 s, fade out over the last 0.3 s."""
    a = min(ease(n / (0.35 * FPS)), 1 - ease((n - frames + 0.3 * FPS) / (0.3 * FPS)))
    if a <= 0:
        return frame
    alpha = layer.getchannel("A").point(lambda v: round(v * a))
    frame.paste(layer, (at[0], at[1] + round((1 - a) * frame.height * 0.025)), alpha)
    return frame


def stretch(a, size):
    a = (a - a.min()) / (np.ptp(a) or 1) * 255
    return np.asarray(Image.fromarray(a.astype("uint8")).resize(size, Image.BICUBIC)) / 255


@lru_cache(4)
def wood(w, h):
    """A dark wooden desk: long grain streaks, a few planks, fine noise and a vignette."""
    rng = np.random.default_rng(7)
    grain = stretch(rng.normal(size=(h // 3, 10)), (w, h))
    fine = stretch(rng.normal(size=(h, w // 2)), (w, h))
    planks = (np.arange(h)[:, None] % (h // 4) < 3) * 0.25
    yy, xx = np.mgrid[0:h, 0:w]
    vignette = 1 - 0.45 * (((xx - w / 2) / w) ** 2 + ((yy - h / 2) / h) ** 2) * 2.2
    shade = (0.62 + 0.3 * grain + 0.1 * fine - planks) * vignette
    return Image.fromarray(np.clip(np.array([112, 72, 42]) * shade[..., None], 0, 255).astype("uint8"))


def photo(img, width, border=0.04, angle=0.0):
    """A picture as a printed photo or document: white border, drop shadow, slightly turned."""
    img = img.convert("RGB")
    img = ImageOps.contain(img, (width, round(width * 1.4)))
    b = max(4, round(width * border))
    card = ImageOps.expand(img, b, fill=(246, 243, 236)).convert("RGBA").rotate(angle, Image.BICUBIC, expand=True)
    pad = round(width * 0.06)
    shade = Image.new("RGBA", (card.width + pad * 2, card.height + pad * 2))
    shade.paste((0, 0, 0, 150), (pad + pad // 3, pad + pad // 2), card.getchannel("A"))
    shade = shade.filter(ImageFilter.GaussianBlur(pad // 2))
    shade.alpha_composite(card, (pad, pad))
    return shade


@lru_cache(1)
def countries():
    return json.load(open("assets/maps/countries.json"))


def map_base(spec, w, h):
    places = spec.get("places") or [{"lat": 48.6, "lon": 7.75}]
    lats, lons = [p["lat"] for p in places], [p["lon"] for p in places]
    clat, clon = (max(lats) + min(lats)) / 2, (max(lons) + min(lons)) / 2
    span = max(14.0, (max(lons) - min(lons)) * 1.8, (max(lats) - min(lats)) * 1.8 * w / h)
    k = w / span
    kx = k * math.cos(math.radians(clat))

    def xy(lon, lat):
        return (w / 2 + (lon - clon) * kx, h / 2 - (lat - clat) * k)

    img = Image.new("RGB", (w, h), NAVY)
    d = ImageDraw.Draw(img)
    for c in countries():
        fill = GREEN if c["iso"] == spec.get("country") else LAND
        for ring in c["rings"]:
            pts = [xy(*p) for p in ring]
            if any(-w < x < 2 * w and -h < y < 2 * h for x, y in pts):
                d.polygon(pts, fill=fill, outline=EDGE)
    return img, [(xy(p["lon"], p["lat"]), str(p.get("name", "")).upper()) for p in places]


def card_frames(spec, frames, w, h):
    """Yields the frames of one full screen card. Every card keeps a slow push (5%) after its last element lands, so
    the screen is never fully still (huashu: a still frame over 2 s reads as a slideshow)."""
    if spec.get("kind") == "map":
        yield from drawn(spec, frames, w, h)
        return
    if spec.get("kind") == "hf":  # a HyperFrames card drawn by hf.py, then the same slow push as our cards
        import hf
        src = (hf.frame(spec["frames"], n, (w, h)).convert("RGB") for n in range(frames))
    else:
        src = drawn(spec, frames, w, h)
    for n, img in enumerate(src):
        z = 1 + 0.05 * n / max(frames - 1, 1)  # steady, not eased, so it never comes to a stop
        cw, ch = w / z, h / z
        yield img.transform((w, h), Image.EXTENT, ((w - cw) / 2, (h - ch) / 2, (w + cw) / 2, (h + ch) / 2), Image.BILINEAR)


def drawn(spec, frames, w, h):
    kind = spec.get("kind")
    sec = lambda n: n / FPS
    if kind == "map":
        base, pins = map_base(spec, w, h)
        f = font(h // 16)
        for n in range(frames):
            img = base.copy()
            d = ImageDraw.Draw(img)
            if spec.get("route") and len(pins) > 1:
                (x0, y0), (x1, y1) = pins[0][0], pins[-1][0]
                cx, cy = (x0 + x1) / 2, min(y0, y1) - abs(x1 - x0) * 0.25
                t = ease((sec(n) - 0.6) / 1.2)
                pts = [((1 - u) ** 2 * x0 + 2 * (1 - u) * u * cx + u * u * x1,
                        (1 - u) ** 2 * y0 + 2 * (1 - u) * u * cy + u * u * y1) for u in np.linspace(0, t, 60)]
                if t > 0:
                    d.line(pts, fill=GOLD, width=max(3, h // 180))
            for i, ((x, y), name) in enumerate(pins):
                a = ease((sec(n) - 0.3 - 0.35 * i) / 0.4)
                if a > 0:
                    r = h * 0.012 * (0.6 + 0.4 * a)
                    d.ellipse([x - r, y - r, x + r, y + r], fill=GOLD, outline=WHITE, width=2)
                    # labels take turns above and below their pin so close places do not overlap
                    img.paste(*draw_label(name, f, (round(x + r * 2), round(y - h * (0.075 if i % 2 == 0 else -0.005))), a))
            z = 1 + 0.06 * n / max(frames - 1, 1)
            yield img.resize((round(w * z), round(h * z)), Image.BILINEAR).crop(
                (round(w * (z - 1) / 2), round(h * (z - 1) / 2), round(w * (z - 1) / 2) + w, round(h * (z - 1) / 2) + h))
        return
    if kind == "document":
        desk = wood(w, h)
        paper = photo(Image.open(spec["image"]), round(w * 0.36), angle=spec.get("angle", -2.5))
        layer, at = overlay({"title": spec.get("title"), "sub": spec.get("sub")}, w, h)
        for n in range(frames):
            img = desk.copy().convert("RGBA")
            a = ease(sec(n) / 0.5)
            s = (1.08 - 0.08 * a) * (1 + 0.04 * sec(n) / sec(frames))
            p = paper.resize((round(paper.width * s), round(paper.height * s)), Image.BILINEAR)
            p.putalpha(p.getchannel("A").point(lambda v: round(v * a)))
            img.alpha_composite(p, (round(w * 0.62 - p.width / 2), round(h / 2 - p.height / 2)))
            yield draw_overlay(img.convert("RGB"), layer, at, max(0, n - round(0.3 * FPS)), frames)
        return
    if kind == "source":  # a screenshot of the article a claim comes from: big enough to read, outlet and date below
        back = Image.new("RGB", (w, h), NAVY)
        shot = Image.open(spec["image"]).convert("RGB")
        paper = photo(shot.crop((0, 0, shot.width, min(shot.height, round(shot.width * 0.56)))), round(w * 0.74),
                      border=0.012, angle=spec.get("angle", -1.0))
        layer, at = overlay({"title": spec.get("title"), "sub": spec.get("sub")}, w, h)
        for n in range(frames):
            img = back.copy().convert("RGBA")
            a = ease(sec(n) / 0.4)
            s = (1.05 - 0.05 * a) * (1 + 0.05 * sec(n) / sec(frames))
            p = paper.resize((round(paper.width * s), round(paper.height * s)), Image.BILINEAR)
            p.putalpha(p.getchannel("A").point(lambda v: round(v * a)))
            img.alpha_composite(p, (round(w / 2 - p.width / 2), round(h * 0.45 - p.height / 2)))
            yield draw_overlay(img.convert("RGB"), layer, at, max(0, n - round(0.3 * FPS)), frames)
        return
    if kind == "quote":
        yield from quote_frames(spec, frames, w, h)
        return
    if kind == "stamp":
        yield from stamp_frames(spec, frames, w, h)
        return
    if kind == "calendar":
        yield from calendar_frames(spec, frames, w, h)
        return

    big, mid, small = font(h // 3), font(h // 9), font(h // 18, False)
    items = [str(i).upper() for i in spec.get("items", [])][:4]
    cw = w * 0.84 / max(len(items), 1)
    polaroids = [photo(Image.open(p), round(cw * 0.78), angle=(-2, 1.5, -1, 2)[i])
                 for i, p in enumerate((spec.get("images") or [])[:len(items)])]
    for n in range(frames):
        img = Image.new("RGB", (w, h), CREAM)
        d = ImageDraw.Draw(img)
        t = sec(n)
        if kind == "number":
            value = str(spec.get("value", ""))
            digits = value.replace(",", "")
            shown = value
            if digits.isdigit() and int(digits) > 9:
                shown = f"{round(int(digits) * ease(t / 0.9)):,}" if "," in value else str(round(int(digits) * ease(t / 0.9)))
            colour = RED if digits in ("0",) else INK
            tw = big.getlength(shown)
            d.text(((w - tw) / 2, h * 0.2), shown, font=big, fill=colour)
            if spec.get("circle"):
                full = big.getlength(value)
                ring(d, ((w - full) / 2 - w * 0.04, h * 0.2, (w + full) / 2 + w * 0.04, h * 0.58), (t - 1.0) / 0.6,
                     max(4, h // 120))
            label = str(spec.get("label", "")).upper()
            a = ease((t - 0.4) / 0.4)
            d.text(((w - small.getlength(label)) / 2, h * 0.62), label, font=small,
                   fill=tuple(round(c + (g - c) * a) for c, g in zip(CREAM, (90, 90, 95))))
        elif kind == "list":
            title = str(spec.get("title", "")).upper()
            if not polaroids:
                d.text(((w - mid.getlength(title)) / 2, h * 0.14), title, font=mid, fill=INK)
            f = font(h // 16)
            for i, item in enumerate(items):
                a = ease((t - 0.3 - 0.35 * i) / 0.4)
                if a <= 0:
                    continue
                x = w * 0.08 + cw * i + cw / 2
                y = (h * 0.2 if polaroids else h * 0.42) + (1 - a) * h * 0.05
                if i < len(polaroids):
                    p = polaroids[i].copy()
                    p.putalpha(p.getchannel("A").point(lambda v: round(v * a)))
                    img.paste(p, (round(x - p.width / 2), round(y)), p)
                    y += p.height - h * 0.02
                d.text((x - f.getlength(item) / 2, y), item, font=f, fill=INK)
        elif kind == "bars":
            rows = [r for r in spec.get("rows", []) if len(r) >= 2][:3]
            title = str(spec.get("title", "")).upper()
            d.text((w * 0.12, h * 0.2), title, font=font(h // 11), fill=INK)
            top = max((float(r[1]) for r in rows), default=1) or 1
            for i, r in enumerate(rows):
                y = h * 0.42 + i * h * 0.15
                d.text((w * 0.12, y), str(r[0]).upper(), font=small, fill=INK)
                a = ease((t - 0.4 - 0.3 * i) / 0.7)
                bw = max(w * 0.004, w * 0.45 * float(r[1]) / top * a)
                d.rectangle([w * 0.32, y, w * 0.32 + bw, y + h * 0.065], fill=(GOLD, (140, 150, 160), GREEN)[i])
                if a > 0.95:
                    d.text((w * 0.32 + bw + w * 0.015, y), str(r[2] if len(r) > 2 else r[1]).upper(), font=small, fill=INK)
        elif kind == "timeline":
            events = [e for e in spec.get("events", []) if isinstance(e, dict)][:5]
            title = str(spec.get("title", "")).upper()
            d.text((w * 0.08, h * 0.14), title, font=font(h // 12), fill=INK)
            y = h * 0.55
            d.line([(w * 0.08, y), (w * 0.08 + w * 0.84 * ease(t / 0.8), y)], fill=INK, width=max(3, h // 200))
            for i, e in enumerate(events):
                a = ease((t - 0.5 - 0.35 * i) / 0.35)
                if a <= 0:
                    continue
                x = w * 0.08 + w * 0.84 * (i + 0.5) / len(events)
                r = h * 0.014 * a
                d.ellipse([x - r, y - r, x + r, y + r], fill=GOLD if i < len(events) - 1 else RED)
                year, label = str(e.get("year", "")).upper(), str(e.get("label", "")).upper()
                fy, fl = font(h // 11), font(h // 30, False)
                d.text((x - fy.getlength(year) / 2, y - h * 0.16 - (1 - a) * h * 0.03), year, font=fy, fill=INK)
                for k, line in enumerate(wrap(label, fl, w * 0.84 / len(events) * 0.9)):
                    d.text((x - fl.getlength(line) / 2, y + h * 0.04 + k * fl.size * 1.2), line, font=fl, fill=INK)
        elif kind == "compare":
            sides = [spec.get("left") or {}, spec.get("right") or {}]
            d.line([(w / 2, h * 0.18), (w / 2, h * 0.18 + h * 0.64 * ease(t / 0.5))], fill=(180, 175, 165), width=3)
            for i, side in enumerate(sides):
                a = ease((t - 0.2 - 0.4 * i) / 0.4)
                if a <= 0:
                    continue
                x = w * (0.25 + 0.5 * i) + (1 - a) * w * 0.03 * (1 if i else -1)
                head = str(side.get("title", "")).upper()
                d.text((x - mid.getlength(head) / 2, h * 0.2), head, font=mid, fill=RED if i else INK)
                for k, item in enumerate([str(x).upper() for x in side.get("items", [])][:3]):
                    if t > 0.5 + 0.4 * i + 0.25 * k:
                        d.text((x - small.getlength(item) / 2, h * (0.44 + 0.12 * k)), item, font=small, fill=INK)
        yield img


def wrap(text, f, width):
    lines, cur = [], ""
    for word in text.split():
        if cur and f.getlength(f"{cur} {word}") > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return lines + [cur] if cur else lines


def quote_frames(spec, frames, w, h):
    """Words someone said or wrote, typed onto a sheet of old paper on the desk, the last line underlined in red."""
    desk = wood(w, h)
    f, fs = font(h // 17, False), font(h // 32, False)
    text = f"\"{str(spec.get('text', '')).strip()}\""
    lines = wrap(text, f, w * 0.5)[:6]
    pw, ph = round(w * 0.6), round(len(lines) * f.size * 1.35 + h * 0.2)
    sheet = Image.new("RGBA", (pw, ph), (241, 233, 214, 255))
    shade = Image.new("RGBA", (pw + 60, ph + 60))
    shade.paste((0, 0, 0, 140), (40, 44, pw + 40, ph + 44))
    shade = shade.filter(ImageFilter.GaussianBlur(14))
    total = sum(len(x) for x in lines)
    for n in range(frames):
        t = n / FPS
        img = desk.copy().convert("RGBA")
        page = sheet.copy()
        d = ImageDraw.Draw(page)
        shown = round(total * ease(t / 1.6))
        y = h * 0.08
        for k, line in enumerate(lines):
            part, shown = line[:max(0, shown)], shown - len(line)
            d.text((pw * 0.08, y), part, font=f, fill=INK)
            if k == len(lines) - 1 and t > 1.7:
                u = ease((t - 1.7) / 0.5)
                d.line([(pw * 0.08, y + f.size * 1.15), (pw * 0.08 + f.getlength(line) * u, y + f.size * 1.15)],
                       fill=RED, width=max(3, h // 220))
            y += f.size * 1.35
        source = str(spec.get("source", "")).upper()
        if t > 1.2:
            d.text((pw * 0.08, ph - h * 0.08), source, font=fs, fill=(110, 100, 90))
        x0, y0 = round((w - pw) / 2), round((h - ph) / 2)
        img.alpha_composite(shade, (x0 - 30, y0 - 30))
        img.alpha_composite(page, (x0, y0))
        yield img.convert("RGB")


def draw_label(text, f, at, a):
    layer = shadowed((round(f.getlength(text)) + 20, f.size + 20), [((4, 4), text, f, WHITE)], 4)
    layer.putalpha(layer.getchannel("A").point(lambda v: round(v * a)))
    return layer, at, layer


def stamp_frames(spec, frames, w, h):
    """A typed record on the desk, then a red rubber stamp slams onto it (the frame shakes once on impact)."""
    desk = wood(w, h)
    f, big = font(h // 18, False), font(h // 7)
    lines = wrap(str(spec.get("text", "")).strip(), f, w * 0.44)[:5]
    pw, ph = round(w * 0.52), round(h * 0.74)
    page = Image.new("RGBA", (pw, ph), (241, 233, 214, 255))
    d = ImageDraw.Draw(page)
    d.text((pw * 0.08, ph * 0.08), str(spec.get("title", "")).upper(), font=font(h // 14), fill=INK)
    for k, line in enumerate(lines):
        d.text((pw * 0.08, ph * 0.26 + k * f.size * 1.5), line, font=f, fill=INK)
    word = str(spec.get("stamp", "")).upper()[:14]
    sw, sh = round(big.getlength(word) + h * 0.1), round(big.size * 1.35)
    mark = Image.new("RGBA", (sw, sh))
    md = ImageDraw.Draw(mark)
    md.rectangle([4, 4, sw - 5, sh - 5], outline=RED, width=max(5, h // 110))
    md.text((h * 0.05, sh * 0.08), word, font=big, fill=RED)
    worn = np.random.default_rng(5).random((sh, sw)) > 0.18  # ink that did not take
    mark.putalpha(Image.fromarray((np.asarray(mark.getchannel("A")) * worn * 0.85).astype("uint8")))
    mark = mark.rotate(14, Image.BICUBIC, expand=True)
    shade = Image.new("RGBA", (pw + 60, ph + 60))
    shade.paste((0, 0, 0, 140), (40, 44, pw + 40, ph + 44))
    shade = shade.filter(ImageFilter.GaussianBlur(14))
    x0, y0 = round((w - pw) / 2), round((h - ph) / 2)
    hit = 0.9
    for n in range(frames):
        t = n / FPS
        img = desk.copy().convert("RGBA")
        a = ease(t / 0.4)
        img.alpha_composite(shade, (x0 - 30, y0 - 30 + round((1 - a) * h * 0.04)))
        img.alpha_composite(page, (x0, y0 + round((1 - a) * h * 0.04)))
        if t >= hit:
            s = 1 + 1.0 * (1 - ease((t - hit) / 0.12))
            m = mark.resize((round(mark.width * s), round(mark.height * s)), Image.BILINEAR)
            img.alpha_composite(m, (round(x0 + pw * 0.62 - m.width / 2), round(y0 + ph * 0.7 - m.height / 2)))
        shake = round(h * 0.006 * math.sin(t * 90)) if hit + 0.1 < t < hit + 0.25 else 0
        yield img.convert("RGB").transform((w, h), Image.AFFINE, (1, 0, 0, 0, 1, shake))


def calendar_frames(spec, frames, w, h):
    """A tear off calendar page drops onto the cream card, then a red marker circles the day (or the year)."""
    month, day, year = (str(spec.get(k, "")).upper() for k in ("month", "day", "year"))
    label = str(spec.get("label", "")).upper()
    cw, ch = round(h * 0.5), round(h * 0.6)
    page = Image.new("RGBA", (cw, ch), (250, 248, 242, 255))
    d = ImageDraw.Draw(page)
    d.rectangle([0, 0, cw, ch * 0.24], fill=RED)
    for k in range(6):  # the rings that hold the pages
        x = cw * (0.15 + 0.14 * k)
        d.ellipse([x - 6, ch * 0.03, x + 6, ch * 0.03 + 12], fill=(60, 60, 65))
    fm, fd, fy = font(round(ch * 0.15)), font(round(ch * 0.46)), font(round(ch * 0.11))
    d.text(((cw - fm.getlength(month)) / 2, ch * 0.06), month, font=fm, fill=WHITE)
    main = day or year
    d.text(((cw - fd.getlength(main)) / 2, ch * 0.27), main, font=fd, fill=INK)
    if day:
        d.text(((cw - fy.getlength(year)) / 2, ch * 0.8), year, font=fy, fill=(110, 100, 90))
    target = (cw / 2 - fd.getlength(main) / 2 - cw * 0.08, ch * 0.3, cw / 2 + fd.getlength(main) / 2 + cw * 0.08, ch * 0.78)
    card = photo(page, cw, border=0.0, angle=-3)
    small = font(h // 18, False)
    for n in range(frames):
        t = n / FPS
        img = Image.new("RGB", (w, h), CREAM)
        a = ease(t / 0.45)
        x, y = round(w * 0.36 - card.width / 2), round(h * 0.5 - card.height / 2 - (1 - a) * h * 0.25)
        img.paste(card, (x, y), card)
        d = ImageDraw.Draw(img)
        off = (x + card.width * 0.06, y + card.height * 0.06)  # where the page sits inside the photo frame
        ring(d, [v + off[i % 2] for i, v in enumerate(target)], (t - 0.7) / 0.6, max(4, h // 120))
        la = ease((t - 0.5) / 0.4)
        for k, line in enumerate(wrap(label, small, w * 0.32) if la > 0 else []):
            d.text((w * 0.62, h * 0.42 + k * small.size * 1.3), line, font=small,
                   fill=tuple(round(c + (g - c) * la) for c, g in zip(CREAM, INK)))
        yield img
