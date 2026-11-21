import json
import os
import re
import time
from collections import Counter
from pathlib import Path
from threading import Thread

import requests
from openai import (AuthenticationError, BadRequestError, NotFoundError, OpenAI,
                    PermissionDeniedError)

import hf

UA = {"User-Agent": f"HistoryStudio/1.0 ({os.getenv('CONTACT') or 'personal documentary tool'})"}
WPM = 150
STYLE = {
    "en": "simple, vivid spoken English for a US and UK audience. Short words, short sentences.",
    "hi": "simple everyday spoken Hindi in Devanagari script. Avoid heavy Sanskrit words.",
}

RULES = """How this channel writes (follow closely):
1. Write for the ear. Mix very short sentences with medium ones. Never two long sentences in a row.
2. Talk to the viewer directly: "Now imagine...", "Think about that for a second.", "Remember what I said about...?"
3. Every section opens with a question or a puzzle and the answer arrives a little later. Keep one open question running at all times.
4. Ground every big number with a comparison the viewer knows from modern life in the US or UK.
5. Use real people and small physical moments (an axe hits stone, a door creaks) to turn facts into scenes.
6. Bust at least one popular myth about the topic.
7. Around the middle, add the most surprising or unsettling true detail to wake the viewer up.
8. Use "..." for a dramatic pause and put a line break between paragraphs. Keep paragraphs to 2 to 4 sentences.
9. No headings, lists, stage directions, emojis or brackets. Write years and numbers the way a narrator says them."""

OUTLINE = """You are the head writer of a fast growing history channel known for videos like
"The Entire History of the Maya Civilization in 15 Minutes".
Plan a {minutes} minute narrated documentary on: "{topic}".

Use ONLY facts found in the source material. Leave out anything it does not support.

Structure that works for this channel:
1. Hook (first 20 seconds): a paradox list ("They had no iron, no wheel, no horses... yet they built..."), a pop culture link the
   viewer already knows, a mystery, then a clear promise of what the video will reveal.
2. Cold open: a real discovery or turning point told as a scene, then a modern reveal that changes the picture.
3. Then a clear timeline where each chapter answers the question the last one raised.
4. End: the fall or the legacy told like falling dominoes, plus one closing thought that lingers.

Return JSON:
{{
  "title": "YouTube style title, e.g. The Entire History of X in {minutes} Minutes",
  "thumb": "3 to 5 word thumbnail text, all caps, curiosity driven",
  "chapters": [
    {{
      "title": "short chapter title",
      "card": "chapter card text, max 5 words",
      "brief": "what this chapter covers: the question it opens with, key facts, names, dates from the source",
      "visuals": ["4 to 6 short English search queries for real artifacts, ruins, old maps or period paintings on Wikimedia Commons"]
    }}
  ],
  "cast": "one line describing how recurring people look (age, clothing, hair) so every illustration keeps them consistent"
}}

Exactly {n} chapters. Narration language: {lang}

SOURCE MATERIAL:
{source}"""

CHAPTER = """Write narration for chapter {i} of {n} of a history video. It will be read by a voice actor.

Video: {title}
All chapters: {plan}
This chapter: "{chapter}"
Brief: {brief}
The previous chapter ended with: "{prev}"

Length: about {words} words. Language: {lang}
{rules}
{extra}
Only use facts from the source material. Never invent quotes, numbers or dates.

SOURCE MATERIAL:
{source}"""

SHOTS = """You are the storyboard artist of a narrated history documentary.
Video: {title}
Recurring people: {cast}

For every numbered narration beat below, draw ONE picture that shows exactly what the narrator says at that moment.
{clip_rule}
Each picture prompt has at most 35 words and names:
1. who is in it, described the same way every time from the recurring people list,
2. the exact action from the narration, acted out with the body: if the narrator says someone reads a letter, show that
   person holding the open letter and reading it; if a ship sails away, show the ship leaving and people watching from shore,
3. their expression, the place and the era, with the clothes of that era named for every person
   ("in a 1518 linen dress and white headscarf"), never modern clothes,
4. {camera}
When the narration is abstract (a number, a comparison, a question), show the people or objects it is about doing something.
Never ask for text, letters or numbers in the image.
{pose_rule}
Beats:
{beats}"""

CAMERA = "one camera choice (wide shot, medium shot, close up, over the shoulder, low angle, aerial view). Vary the camera."
WIDE = ("one wide camera choice (wide shot, long shot, full body shot, aerial view, low angle wide shot). People are seen "
        "head to toe and take about a third of the frame height, with plenty of scenery around them. Vary the camera.")

ANSWER = """
Return JSON: {{"prompts": ["...", ...]}} with exactly {n} items in order.
"""

POSE_RULE = """
Some beats show a visible physical action: someone reads, writes, walks, rows, digs, fights, carves, points, a ship sails.
For those, also give 2 or 3 pose steps that move the action forward, each under 12 words. Same people, same place, same
camera; only bodies and objects change. Example for "she opened the letter and read it":
["her hands break the wax seal", "she unfolds the letter", "she reads, eyes on the page"].
Give an empty list for beats without a visible action. Use poses for about one beat in three.

Return JSON: {{"prompts": ["...", ...], "poses": [["...", ...], [], ...]}} with exactly {n} items in each list, in order.
"""

CLIP_RULE = """Use "clip:" (real stock footage) for roughly one beat in {share}, but ONLY when real modern footage can honestly
show it: landscapes, sea, rivers, jungle, weather, fire, ruins as they stand today, museum artifacts, old maps, night sky.
Write a 2 to 4 word search, like "clip: stormy ocean waves", most important words first.
Never use a clip for a named person or a specific past event, and never for modern objects like planes or stadiums."""

META = """Write YouTube upload metadata for this history video aimed at the US, UK and Europe.
Title: {title}
Chapters: {chapters}

Return JSON:
{{"titles": ["5 title options under 70 characters. Pattern ideas: 'The Entire History of X in N Minutes', 'Why X Vanished', 'What Life Was Really Like in X'"],
  "description": "2 short paragraphs that hook the viewer and use the main search keywords naturally",
  "tags": ["15 to 20 tags"]}}"""

FALLBACKS = "gemini-3.1-flash-lite,gemini-3.5-flash-lite"
_client = None
_fails = Counter()
_missing = set()


EDIT = """You are the editor of a narrated history documentary made like modern history explainer channels, where
proof appears on screen the moment the narrator states it.
Video: {title}
This is chapter {chapter}. Below are its numbered narration sentences. Give a sentence at most one element:
- title: the video's name over a picture. Only in chapter 1, on the first sentence that names the subject.
  "title" max 3 words, "sub" max 5.
- map: "places": 1 to 3 real places, each {{"name": "...", "lat": 0.0, "lon": 0.0}}, "country": ISO 3166 alpha-2 code
  of the country to colour, "route": true only when the sentence describes travel from the first place to the last.
- card: a designed motion card (stop motion paper style) for a number, a date, a list, a comparison, an order, a
  quote or a chapter moment. "card": an id from the menu below that fits the sentence best, never one already shown;
  "slots": text for its slots, each within its max characters. Fill only from the sentence: short labels, no made up
  names or figures. Leave "source" empty unless the sentence names where the fact comes from. Images are added
  by the code, leave image slots out. Use <em>one phrase</em> for emphasis in headline or label slots.
- headline: a short shocking line on a torn paper strip over the picture, for a reveal. "title" max 5 words.
- document: a famous real artwork, photo, map or record of this exact event or person, shown on a desk. Only when it
  surely exists on Wikimedia Commons; "query": 2 to 5 words that name it exactly (place and year, or artist and
  title), "title" max 4 words, "sub" max 4 words. A plain name of an unknown person is not a document.
Every element also has "word": the word of the sentence it should appear on (the number, the name, the place), and
"why": why the viewer needs it there, max 10 words.
Rules: use only facts the sentences state. About one sentence in four gets an element, at least three sentences
apart, and vary the kinds and cards.
A map only the first time a place appears in the whole video; never show a place, date or number again.
Already shown in earlier chapters: {shown}
Sentences:
{sentences}

Card menu (id: slot<=max characters):
{menu}

Return JSON with exactly {n} items, one per sentence in order, null where a sentence gets nothing, for example:
{{"edits": [null, {{"kind": "map", "places": [{{"name": "Strasbourg", "lat": 48.58, "lon": 7.75}}], "country": "FR",
"route": false, "word": "Strasbourg", "why": "shows where the story starts"}}, null, null, {{"kind": "card",
"card": "t1.stat.calendar", "slots": {{"kicker": "The first dancer", "kicker-month": "July", "stat": "14",
"label": "Frau Troffea <em>steps into the street</em>", "source": ""}}, "word": "fourteenth", "why": "the day it began"}},
...]}}
"""

BEATS = """You plan the captions and the sound of a narrated history documentary, sentence by sentence.
Video: {title}
For each numbered sentence return:
- "label": a fact label shown in the lower left corner, {{"title": "max 4 words", "sub": "max 4 words"}}, or null.
  Give labels to about half of the sentences, first to every sentence with a year, date, place, name or number.
  "title" is the key fact (a year, a place, a name, an amount with its unit), "sub" a little context.
- "mood": the feeling of the moment, one of calm, mystery, tension, sad, wonder, triumph.
- "ambience": the background sound of the place, one of crowd, wind, fire, water, church, market, battle, night,
  rain, forest, or none.
- "turn": true only for a reveal, a shock or a turning point (at most one sentence in eight).
- "sound": a sound the sentence says happens, one of {moments}, or null; "sound_word": the word it happens on.
  Only when the sound is really in the sentence (a scream, a heartbeat, swords clashing, coins), not for mood.
Use only facts the sentences state.
Sentences:
{sentences}

Return JSON with exactly {n} items in order: {{"beats": [{{"label": {{"title": "July 1518", "sub": "Strasbourg"}},
"mood": "mystery", "ambience": "crowd", "turn": false, "sound": null, "sound_word": null}}, ...]}}
"""


CUTS = ("zoom-blur", "cross-zoom", "film-burn", "dreamy-zoom", "iris", "clock-wipe", "wipe", "fade")
OPENS = ("book-flip", "film-burn", "iris", "zoom-blur", "dreamy-zoom", "clock-wipe")
MOVES = ("push", "pull", "pan-left", "pan-right", "rise", "dive", "drift", "shake")
ACCENTS = ("whoosh", "hit", "swell", "pop", "paper", "tick")
AIRS = ("smoke", "embers", "rain", "snow", "leaves", "butterflies", "dust", "birds", "none")

DIRECT = """You are the director and lead editor of a narrated history documentary on YouTube. The goal: a viewer who
opens the video stays to the end. That comes from something changing on screen every few seconds, motion that
follows the meaning of each sentence, and sound that lands exactly on the words. Restraint is part of it: an effect
on every sentence feels cheap and tires the eye, so save the big moves for the moments that earn them.
Video: {title}
Chapter {chapter} of {chapters}: {name}
Picture style: {style}

For each numbered sentence decide:
- "move": the camera on its picture. push (slow zoom in: focus, tension, a secret), pull (zoom out: reveals scale,
  an army, a city), pan-left / pan-right (travel, a line of soldiers, a landscape), rise (tilt up: awe, a tower,
  a king), dive (tilt down: a fall, a defeat), drift (almost still: calm, grief), shake (impact, a blast, a charge;
  rare). Do not give the same move to two sentences in a row.
- "pace": quick (a new picture every 2 to 3 s: action, lists, chaos), normal (4 s), slow (6 s: emotion, wonder).
- "cut": a transition into this sentence's picture, only at a real change (new place, time jump, turning point),
  at most one sentence in six, never two within five sentences: zoom-blur (a sudden shock), cross-zoom (speed,
  a charge), film-burn (a memory, a jump in time), dreamy-zoom (legend, a dream), iris (focus on one person or
  thing), clock-wipe (time passing), wipe (another place), fade (a death, an ending), or null for a hard cut.
- "leak": true for warm light over a moment of glory, hope or sunrise (at most two in the chapter).
- "caption_fx": how its caption appears, if it gets one: typewriter (dates, letters, documents), number (counts and
  amounts), slam (a shocking line), marker (a circle around the key word), or plain.
- "accent": one sound design hit on the key word, only where it lifts the moment, about one sentence in three:
  whoosh (movement, a reveal), hit (impact, a hard fact), swell (a riser into a turn), pop (a light fact),
  paper (a letter, a map), tick (time running out), or null. "accent_word": the word it lands on.
- "air": what drifts over the picture: smoke, embers, rain, snow, leaves, butterflies, dust, birds, or none.
For the chapter:
- "open": the transition from the previous chapter: book-flip (only for the storybook style), film-burn, iris,
  zoom-blur, dreamy-zoom, clock-wipe.
- "drop": the number of the one sentence before which the music falls silent for a breath, so its reveal hits
  harder, or null. Use it only for the strongest reveal of the chapter.
{hook}
Sentences:
{sentences}

Return JSON with exactly {n} items in "direct", one per sentence in order:
{{"open": "film-burn", "drop": 7, "direct": [{{"move": "push", "pace": "normal", "cut": null, "leak": false,
"caption_fx": "typewriter", "accent": "whoosh", "accent_word": "1453", "air": "smoke"}}, ...]}}
"""
HOOK = """This is the opening chapter: the first 3 sentences are the hook. Give them quick pace, strong moves and an
accent each, so nothing on screen stands still in the first ten seconds."""


def say(msg):
    print(f"  {time.strftime('%H:%M:%S')} {msg}", flush=True)


def objects(text):
    """The JSON in a model's answer. Some answers carry one object per sentence one after another instead of a list;
    those come back as {"items": [...]}."""
    found, i, dec = [], text.find("{"), json.JSONDecoder()
    while 0 <= i < len(text):
        obj, end = dec.raw_decode(text, i)
        found.append(obj)
        i = text.find("{", end)
    if not found:
        raise ValueError("no JSON in the answer")
    return found[0] if len(found) == 1 else {"items": found}


def llm(prompt, as_json=False, temp=0.8, fast=False):
    """Models are tried in order: LLM_FAST first for small jobs, then LLM_MODEL, then LLM_FALLBACKS. A model that keeps
    failing (503 overloaded, 429 free limit, no answer within LLM_TIMEOUT seconds) moves to the back and only gets
    another chance after three good answers from others, so one stuck model cannot hold the whole video up."""
    global _client
    if not os.getenv("LLM_API_KEY"):
        raise SystemExit("LLM_API_KEY is empty. Get a free key at aistudio.google.com (Get API key) and put it in .env.example")
    limit = float(os.getenv("LLM_TIMEOUT", "150"))
    _client = _client or OpenAI(base_url=os.getenv("LLM_BASE_URL"), api_key=os.getenv("LLM_API_KEY"),
                                timeout=limit, max_retries=0)
    names = ([os.getenv("LLM_FAST", "gemini-3.1-flash-lite")] if fast else []) + [os.getenv("LLM_MODEL", "")]
    names += os.getenv("LLM_FALLBACKS", FALLBACKS).split(",")
    extra = {"response_format": {"type": "json_object"}} if as_json else {}
    for attempt in range(5):
        models = [m for m in dict.fromkeys(n.strip() for n in names) if m and m not in _missing]
        models.sort(key=lambda m: _fails[m])
        if not models:
            raise SystemExit("none of the models in LLM_MODEL and LLM_FALLBACKS exist, check their names")
        for model in models:
            t0 = time.time()
            say(f"waiting for {model} ({len(prompt) // 1000}k characters sent)")
            try:
                text = ask(model, prompt, temp, extra, limit)
                say(f"answer in {time.time() - t0:.0f}s, {len(text.split())} words")
                for m in _fails:
                    _fails[m] = 0 if m == model else max(0, _fails[m] - 1 / 3)
                return objects(text) if as_json else text
            except NotFoundError:
                say(f"{model} does not exist for this key, skipping it")
                _missing.add(model)
            except (BadRequestError, AuthenticationError, PermissionDeniedError) as e:
                raise SystemExit(f"the LLM refused the request, check LLM_API_KEY: {str(e)[:300]}")
            except Exception as e:
                _fails[model] += 1
                say(f"{model} failed after {time.time() - t0:.0f}s ({str(e)[:100] or 'no answer in time'})")
        say(f"all models busy, waiting {30 * (attempt + 1)}s")
        time.sleep(30 * (attempt + 1))
    raise SystemExit("the LLM kept failing, try again in a few minutes")


_seen = []  # times of the last calls to a model that sees, shared by every thread
_eyes = {}  # the models that answered a picture in this run, fastest first, and why the others were dropped


def eyes(refresh=False):
    """The models that can look at a picture now. A fixed list in .env.example goes stale: gemini-2.5-flash-lite was
    retired (404) and gemini-flash-latest stopped answering, and the test film's check failed on them. So the list is
    asked from the API once a run: the .env.example models first, then every listed flash model, each tried with a
    one pixel picture under a wall clock limit; those that answer are kept, fastest first."""
    import base64
    import io
    from PIL import Image
    if "at" in _eyes and not refresh:  # an empty list is an answer too: asking again at every picture spent calls
        return _eyes["live"]
    named = [os.getenv(k, "") for k in ("LLM_VISION", "LLM_FAST", "LLM_MODEL")] + os.getenv("LLM_FALLBACKS", FALLBACKS).split(",")
    try:
        listed = sorted((m.id.split("/")[-1] for m in _client.models.list()), reverse=True)
    except Exception:
        listed = []
    listed = [m for m in listed if re.fullmatch(r"gemini-(\d[\d.]*-)?flash(-lite)?(-latest)?", m)]
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (150, 110, 60)).save(buf, "JPEG")
    tiny = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    timed, dropped = [], _eyes.setdefault("dropped", {})
    for model in [m for m in dict.fromkeys(x.strip() for x in named + listed) if m][:int(os.getenv("LLM_VISION_TRY", "8"))]:
        t0 = time.time()
        try:
            ask(model, [{"type": "text", "text": 'Answer {"ok": true}'}, {"type": "image_url", "image_url": {"url": tiny}}],
                0, {"response_format": {"type": "json_object"}}, 30)
            timed.append((time.time() - t0, model))
            dropped.pop(model, None)
        except Exception as err:
            dropped[model] = (str(err)[:90] or "no answer in 30s").replace("\n", " ")
            reset = re.search(r"retryDelay'?\W+(\d+)s", str(err)) if "PerDay" in str(err) else None
            if reset:
                _eyes.setdefault("resets", {})[model] = reset.group(1)
    live = [m for _, m in sorted(timed)]
    if live != _eyes.get("live"):  # Telegram hears of a change, not of every look
        print(f"  vision models: {', '.join(live) or 'none'}" +
              "".join(f"; {m} left out ({why})" for m, why in dropped.items()), flush=True)
    _eyes["live"], _eyes["at"] = live, time.time()
    return live


def see(prompt, image):
    """A question about a picture, answered as JSON by one of eyes(). Calls are spread to LLM_VISION_RPM a minute and
    each gets LLM_VISION_LIMIT seconds of wall clock (a model that hangs cost 150 s a picture before). A busy model
    (503, 429) is a normal state of a free API: the next model takes the call, and when all are busy the round is
    repeated for about ten minutes, asking the API again which models answer. A model that is gone (404) or out of its
    daily quota leaves the list. Only then the API counts as down, and the error names every model and its reason."""
    import base64
    import io
    import threading
    from PIL import Image
    global _client
    _client = _client or OpenAI(base_url=os.getenv("LLM_BASE_URL"), api_key=os.getenv("LLM_API_KEY"),
                                timeout=float(os.getenv("LLM_TIMEOUT", "150")), max_retries=0)
    img = Image.open(image).convert("RGB")
    img.thumbnail((768, 768))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    msg = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": url}}]
    gap = 60 / float(os.getenv("LLM_VISION_RPM", "12"))
    limit = float(os.getenv("LLM_VISION_LIMIT", "60"))
    lock = see.__dict__.setdefault("lock", threading.Lock())
    why = {}
    for i, pause in enumerate((0, 30, 90, 180, 300)):
        if _eyes.get("down"):  # another picture already waited ten minutes for nothing: no second wait
            raise SystemExit(_eyes["down"])
        time.sleep(pause)
        with lock:  # after a busy round the API is asked again which models answer, by one thread for all
            models = eyes(refresh=i > 0 and time.time() - _eyes.get("at", 0) > 60)
        for model in list(models):
            with lock:  # the next call waits its turn so the threads together stay under the rate
                time.sleep(max(0, (_seen[-1] + gap - time.time()) if _seen else 0))
                _seen[:] = [time.time()]
            try:
                return objects(ask(model, msg, 0.1, {"response_format": {"type": "json_object"}}, limit))
            except Exception as err:
                why[model] = (str(err)[:120] or f"no answer in {limit:.0f}s").replace("\n", " ")
                if isinstance(err, NotFoundError) or "PerDay" in str(err):  # gone, or no calls left today
                    with lock:
                        if model in _eyes.get("live", []):
                            _eyes["live"].remove(model)
                        _eyes.setdefault("dropped", {})[model] = why[model][:90]
                        reset = re.search(r"retryDelay'?\W+(\d+)s", str(err))
                        if reset:
                            _eyes.setdefault("resets", {})[model] = reset.group(1)
    # every model out of its free calls for the day: Google says when they come back, so the stage waits for that
    # instead of booting a GPU machine every half hour to be refused again
    resets = [int(x) for x in _eyes.get("resets", {}).values()]
    spent = bool(why) and all("PerDay" in w or "exceeded your current quota" in w for w in why.values())
    if spent and not resets:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))  # the free calls come back at midnight in California
        resets = [int((now.replace(hour=0, minute=0, second=0) + timedelta(days=1) - now).total_seconds())]
    wait = f" wait={min(resets)}" if resets and (spent or not _eyes.get("live")) else ""
    _eyes["down"] = (f"service busy:{wait} no model could look at " + Path(image).name + " for ten minutes; " +
                     "; ".join(f"{m}: {w}" for m, w in why.items()))[:700]
    raise SystemExit(_eyes["down"])


def ask(model, prompt, temp, extra, limit):
    """The API timeout only counts silence between bytes, so a slow answer could take 20 minutes. This gives up after
    `limit` seconds of wall clock time; the abandoned request finishes in the background."""
    box = {}

    def call():
        try:
            box["r"] = _client.chat.completions.create(model=model, temperature=temp,
                                                       messages=[{"role": "user", "content": prompt}], **extra)
        except Exception as e:
            box["e"] = e

    t = Thread(target=call, daemon=True)
    t.start()
    t.join(limit)
    if t.is_alive():
        raise TimeoutError()
    if "e" in box:
        raise box["e"]
    return box["r"].choices[0].message.content.strip()


def relevant(source, about, limit=20000):
    """The paragraphs of the source that share the most words with this chapter, in their original order.
    Sending 20k characters instead of the full 60k makes every chapter request faster and less likely to fail."""
    if len(source) <= limit:
        return source
    keys = {w for w in re.findall(r"[a-z]{4,}", about.lower())}
    pars = [p for p in source.split("\n") if p.strip()]
    ranked = sorted(range(len(pars)), key=lambda i: -sum(w in keys for w in re.findall(r"[a-z]{4,}", pars[i].lower())))
    keep, size = set(), 0
    for i in ranked:
        if size + len(pars[i]) > limit:
            break
        keep.add(i)
        size += len(pars[i])
    return "\n".join(pars[i] for i in sorted(keep))


def wiki_get(**params):
    """Wikipedia is sometimes slow from India, so wait longer and try a few times before giving up."""
    for attempt in range(4):
        try:
            r = requests.get("https://en.wikipedia.org/w/api.php", headers=UA, timeout=(15, 90),
                             params={**params, "format": "json"})
            r.raise_for_status()
            return r.json()["query"]
        except (requests.RequestException, KeyError, ValueError) as e:
            if attempt < 3:
                say(f"wikipedia slow, retry {attempt + 1} of 3 ({type(e).__name__})")
                time.sleep(10 * (attempt + 1))
    return {}


def wiki(titles, limit=30000):
    parts = []
    for t in titles:
        say(f"reading wikipedia: {t}")
        hits = wiki_get(action="query", list="search", srsearch=t, srlimit=1).get("search")
        if not hits:
            continue
        pages = wiki_get(action="query", prop="extracts", explaintext=1, redirects=1, titles=hits[0]["title"])
        text = next(iter(pages.get("pages", {}).values()), {}).get("extract", "")
        text = re.split(r"\n== (See also|References|Notes|Further reading) ==", text)[0]
        if text:
            parts.append(f"### {hits[0]['title']}\n{text[:limit]}")
    if not parts:
        raise SystemExit("could not reach Wikipedia, check the internet and run the same command again")
    return "\n\n".join(parts)


def beats(text, limit=int(os.getenv("WORDS_PER_IMAGE", "40"))):
    """Split narration into beats of whole sentences, one illustration per beat."""
    out = []
    for par in [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]:
        cur, chunks = [], []
        for sent in re.split(r"(?<=[.!?])\s+", par):
            if cur and len(" ".join(cur + [sent]).split()) > limit:
                chunks.append(" ".join(cur))
                cur = []
            cur.append(sent)
        chunks.append(" ".join(cur))
        out.append(chunks)
    return out


def write_script(topic, folder, minutes):
    lang = STYLE[os.getenv("LANGUAGE", "en")]
    parts = folder / "parts"
    parts.mkdir(parents=True, exist_ok=True)

    src_file = parts / "source.txt"
    if not src_file.exists():
        say("step 1: finding wikipedia articles")
        q = llm(f'List the 3 English Wikipedia article titles that best cover this history topic: "{topic}". '
                'Return JSON: {"titles": ["...", "...", "..."]}', as_json=True, temp=0, fast=True)
        src_file.write_text(wiki(q["titles"]), encoding="utf-8")
    source = src_file.read_text(encoding="utf-8")
    if len(source) < 2000:
        print("  warning: very little source text found, check facts carefully")

    plan_file = parts / "outline.json"
    n = max(4, round(minutes / 5))
    if not plan_file.exists():
        say(f"step 2: planning {n} chapters")
        plan = llm(OUTLINE.format(minutes=minutes, topic=topic, n=n, lang=lang, source=source), as_json=True, temp=0.7)
        plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    plan = json.loads(plan_file.read_text(encoding="utf-8"))

    chapters = plan["chapters"]
    words = minutes * WPM // len(chapters)
    summary = " | ".join(f"{i + 1}. {c['title']}" for i, c in enumerate(chapters))
    prev = "(this is the opening)"
    for i, c in enumerate(chapters, 1):
        f = parts / f"ch{i:02d}.txt"
        if not f.exists():
            say(f"step 3: writing chapter {i} of {len(chapters)}")
            extra = ("Start with the hook from the plan. Do not greet the viewer." if i == 1 else
                     "Finish the story and end on one lingering thought. No call to subscribe." if i == len(chapters) else
                     "End with a small open question that pulls the viewer into the next chapter.")
            f.write_text(llm(CHAPTER.format(title=plan["title"], plan=summary, i=i, n=len(chapters), chapter=c["title"],
                                            brief=c["brief"], prev=prev, words=words, lang=lang, rules=RULES,
                                            extra=extra, source=relevant(source, f"{c['title']} {c['brief']}"))),
                         encoding="utf-8")
        c["text"] = f.read_text(encoding="utf-8").strip()
        prev = c["text"][-500:]

        c["beats"] = beats(c["text"])
        flat = [b for par in c["beats"] for b in par]
        moving = os.getenv("POSES") == "1"
        f = parts / f"shots{i:02d}{'p' if moving else ''}.json"
        if not f.exists():
            say(f"step 4: picture list for chapter {i} of {len(chapters)}")
            numbered = "\n".join(f"{k + 1}. {b}" for k, b in enumerate(flat))
            clips = CLIP_RULE.format(share=os.getenv("CLIP_SHARE", "3")) if os.getenv("CLIPS", "1") == "1" else ""
            got = llm(SHOTS.format(title=plan["title"], cast=plan.get("cast", ""), n=len(flat), beats=numbered,
                                   camera=WIDE if os.getenv("WIDE") == "1" else CAMERA,
                                   clip_rule=clips, pose_rule=(POSE_RULE if moving else ANSWER).format(n=len(flat))),
                      as_json=True, temp=0.7, fast=True)
            pics = (got["prompts"] + [c["title"]] * len(flat))[:len(flat)]
            moves = (list(got.get("poses", [])) + [[]] * len(flat))[:len(flat)]
            f.write_text(json.dumps([[p, m[:3] if isinstance(m, list) else []] for p, m in zip(pics, moves)], ensure_ascii=False), encoding="utf-8")
        c["prompts"] = iter([x, []] if isinstance(x, str) else x for x in json.loads(f.read_text(encoding="utf-8")))

    md = [f"# {plan['title']}", f"thumb: {plan['thumb']}", ""]
    for c in chapters:
        md += [f"## {c['title']}", f"card: {c['card']}", f"visuals: {' | '.join(c['visuals'])}", ""]
        for par in c["beats"]:
            for b in par:
                pic, moves = next(c["prompts"])
                md += [f"> {pic}", *[f">> {m}" for m in moves], b]
            md.append("")
    (folder / "script.md").write_text("\n".join(md), encoding="utf-8")


def add_pictures(folder):
    """A script written by hand may have no picture lines ("> ..."). Each such chapter gets them from Gemini, one per
    beat, the same way as a script Gemini writes; the hand written file is kept as script_original.md."""
    path = folder / "script.md"
    raw = path.read_text(encoding="utf-8")
    head, *blocks = re.split(r"^## ", raw, flags=re.M)
    if all(re.search(r"^>", b, re.M) for b in blocks):
        return
    script = read_script(path)
    (folder / "parts").mkdir(exist_ok=True)
    md = [head.rstrip(), ""]
    for i, (block, c) in enumerate(zip(blocks, script["chapters"]), 1):
        if re.search(r"^>", block, re.M):
            md += [f"## {block.rstrip()}", ""]
            continue
        pars = beats(c["text"])
        flat = [b for par in pars for b in par]
        f = folder / "parts" / f"shots{i:02d}.json"
        if not f.exists():
            say(f"picture list for chapter {i} of {len(blocks)} (your script had none)")
            numbered = "\n".join(f"{k + 1}. {b}" for k, b in enumerate(flat))
            got = llm(SHOTS.format(title=script["title"], cast="", n=len(flat), beats=numbered,
                                   camera=WIDE if os.getenv("WIDE") == "1" else CAMERA, clip_rule="",
                                   pose_rule=ANSWER.format(n=len(flat))), as_json=True, temp=0.7, fast=True)
            f.write_text(json.dumps((got["prompts"] + [c["title"]] * len(flat))[:len(flat)], ensure_ascii=False))
        prompts = iter(json.loads(f.read_text()))
        md += [f"## {c['title']}", f"card: {c['card']}", f"visuals: {' | '.join(c['visuals'])}", ""]
        for par in pars:
            for b in par:
                p = next(prompts)
                md += [f"> {p[0] if isinstance(p, list) else p}", b]
            md.append("")
    (folder / "script_original.md").write_text(raw, encoding="utf-8")
    path.write_text("\n".join(md), encoding="utf-8")


def check_script(text):
    """Problems that stop a hand written script, in plain words (empty list when it is fine)."""
    problems = []
    if not re.search(r"^# \S", text, re.M):
        problems.append('the first line must be the title, starting with "# "')
    chapters = re.findall(r"^## (.+)$", text, re.M)
    if not chapters:
        problems.append('no chapters: every chapter starts with a line "## Chapter name"')
    words = len(re.sub(r"^(#|>|card:|visuals:|thumb:).*$", "", text, flags=re.M).split())
    if words < 150:
        problems.append(f"only {words} words of narration; a minute of video needs about 188")
    return problems


def sentences(text):
    return [x for x in re.split(r"(?<=[.!?])\s+", text.replace("\n", " ")) if x.strip()]


MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
UNITS = r"(?:people|men|women|children|soldiers|ships|miles|kilometres|kilometers|km|days|weeks|months|years|dead|" \
        r"deaths|died|victims|houses|towns|tons|pounds|dollars|coins|horses|guns|feet|metres|meters|dancers|times)"
FACTS = [  # (what, pattern, title group, sub group); the first match makes the label
    ("date", rf"\b(?P<y2>\d{{2,4}})\s*(?P<era>BC|BCE|AD|CE)\b"
             rf"|\b(?:(?P<m>{MONTHS})\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?(?:of\s+)?)?(?P<y>1\d{{3}}|20\d{{2}})\b(?!\s*{UNITS})"),
    ("number", rf"\b(?P<n>\d[\d,]*(?:\.\d+)?)\s*(?P<u>%|percent|thousand|million|billion)?\s+(?P<what>{UNITS})\b"),
    ("name", r"\b(?P<who>(?:named|called|King|Queen|Emperor|Empress|Pope|General|Captain|Admiral|Sir|Saint|Frau|Herr|"
             r"Lord|Lady|Doctor)\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"),
    ("place", rf"\b(?:in|at|to|from|near|into|across|outside)\s+(?P<at>(?!(?:{MONTHS}|The|A|An)\b)[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"),
]


def backup_label(sentence):
    """A caption the code finds itself when the plan left a sentence with a date, number, name or place bare."""
    found = {}
    for what, pat in FACTS:
        m = re.search(pat, sentence)
        if m:
            found[what] = m
    if not found or set(found) == {"place"}:
        return None
    place = found["place"].group("at") if "place" in found else ""
    if "date" in found and (len(re.findall(r"\b1\d{3}\b|\b20\d{2}\b", sentence)) > 1 or " between " in sentence):
        del found["date"]
    if "date" in found:
        m = found["date"]
        title = f"{m.group('y2')} {m.group('era')}" if m.group("era") else f"{m.group('m') or ''} {m.group('y')}".strip()
        return {"kind": "caption", "title": title, "sub": place, "word": title.split()[0], "why": "backup: a date"}
    if "number" in found:
        m = found["number"]
        title = " ".join(x for x in (m.group("n"), m.group("u"), m.group("what")) if x)
        return {"kind": "caption", "title": title, "sub": place, "word": m.group("n"), "why": "backup: a number"}
    if "name" in found:
        who = re.sub(r"^(named|called)\s+", "", found["name"].group("who"))
        return {"kind": "caption", "title": who, "sub": place, "word": who.split()[-1], "why": "backup: a name"}
    return None  # a place alone is often said in passing; the edit plan and the map handle places


# sounds of things that happen in the story, one folder each in assets/sfx/moments/
MOMENTS = sorted(p.name for p in Path("assets/sfx/moments").glob("*") if p.is_dir() and any(p.iterdir())) \
    if Path("assets/sfx/moments").exists() else []

CHECK = {"number": "number", "bars": "number", "map": "place", "timeline": "date", "calendar": "date", "quote": "quote",
         "document": "quote", "stamp": "quote", "compare": "comparison"}


def headline(e):
    """The few words of a card, to show as a caption when the card is dropped."""
    if e.get("places"):
        return e["places"][0].get("name", "")
    if e.get("value"):
        return f"{e['value']} {e.get('label', '')}".strip()
    if e.get("kind") == "card":
        text = [re.sub(r"<[^>]+>", "", str(v)) for v in (e.get("slots") or {}).values() if v]
        return next((t for t in text if not re.search(r"\.(jpe?g|png)$", t)), "")[:40]
    if e.get("month") or e.get("year"):
        return f"{e.get('month', '')} {e.get('day', '')} {e.get('year', '')}".replace("  ", " ").strip()
    return str(e.get("title") or e.get("source") or e.get("text", ""))[:30]


def checked(e, d):
    """The second opinion of colibri (GLiNER2.5-Decide) on a card. When it is sure the sentence is plain story
    ("scene"), the card becomes a caption; otherwise the card stays and the check goes into "why"."""
    if not d or e.get("kind") not in CHECK:
        return e
    if d["show"] == "scene" and d["sure"] >= float(os.getenv("DECIDE_SURE", "0.6")):
        return {"kind": "caption", "title": headline(e), "sub": e.get("sub", ""), "word": e.get("word"),
                "why": f"{e.get('why', '')}; check: story, not proof ({d['sure']}), so a caption"}
    agree = "agrees" if d["show"] == CHECK[e["kind"]] else f"said {d['show']}"
    return {**e, "why": f"{e.get('why', '')}; check {agree} ({d['sure']})"}


def edits(folder, script):
    """The edit plan of every chapter. Three answers per chapter, kept in parts/: edit01.json (proof cards on chosen
    sentences, each with the word it lands on and why), beats01.json (a caption, mood, place sound and turning point
    for every sentence) and direct01.json (camera move, pace, transition, light leak, caption animation, sound hit
    and particles per sentence, the chapter opening and the music drop). The strongest Kaggle model answers all of
    them in one run (director.py); what it could not answer, the Gemini key plans. colibri checks every card
    (decide01.json) and the code adds a caption where a sentence with a date, number, name or place was left bare.
    Positions are word numbers in the chapter text, which match the narration's words one to one.
    Returns per chapter {"elements": [(word, element)], "moods": [(word, mood)], "ambience": [(word, name)],
    "turns": [word], "sounds": [(word, moment)], "lines": [(word, sentence)], "direct": [(word, choices)],
    "accents": [(word, sound)], "open": transition or None, "drop": word or None}."""
    import decide
    import director
    (folder / "parts").mkdir(exist_ok=True)
    out, maps, shown = [], set(), []
    steps = (("edit", EDIT, "edits"), ("beats", BEATS, "beats"), ("direct", DIRECT, "direct"))
    ask = lambda i, ch, prompt, lines: prompt.format(
        title=script["title"], chapter=i, chapters=len(script["chapters"]), name=ch["title"], n=len(lines),
        sentences="\n".join(f"{k + 1}. {x}" for k, x in enumerate(lines)), moments=", ".join(MOMENTS) or "none",
        shown="; ".join(shown) or "nothing", menu=hf.menu(), style=os.getenv("STYLE", "storybook"),
        hook=HOOK if i == 1 else "")
    todo = {f"{name}{i:02d}.json": (ask(i, ch, prompt, sentences(ch["text"])), key)
            for i, ch in enumerate(script["chapters"], 1) for name, prompt, key in steps
            if not (folder / "parts" / f"{name}{i:02d}.json").exists()}
    director.plan(folder, todo)
    for i, ch in enumerate(script["chapters"], 1):
        lines = sentences(ch["text"])
        planned = []
        for name, prompt, key in steps:
            f = folder / "parts" / f"{name}{i:02d}.json"
            if not f.exists():
                say(f"{name} plan for chapter {i} of {len(script['chapters'])}")
                try:
                    got = llm(ask(i, ch, prompt, lines), as_json=True, temp=0.5, fast=True)
                    got = got if key == "direct" else got.get(key) or got.get("items") or []
                    f.write_text(json.dumps(got, ensure_ascii=False, indent=1), encoding="utf-8")
                except (Exception, SystemExit) as err:  # the backup labels below still give the chapter captions
                    say(f"  {name} plan failed ({str(err)[:80]}), going on with the backup plan")
            items = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
            whole = items if isinstance(items, dict) else {}
            items = (whole.get("direct") or whole.get("items") or []) if whole else items
            planned.append([x if isinstance(x, dict) else {} for x in items[:len(lines)]] + [{}] * (len(lines) - len(items[:len(lines)])))
        chosen = whole  # the direct file's chapter choices (it is read last)
        try:
            checks = decide.decide(lines, folder / "parts" / f"decide{i:02d}.json") if os.getenv("DECIDE", "1") == "1" else None
        except Exception:
            checks = None
        checks = checks or [None] * len(lines)
        starts = [sum(len(x.split()) for x in lines[:k]) for k in range(len(lines))]
        plan = {"elements": [], "moods": [], "ambience": [], "turns": [], "sounds": [], "lines": list(zip(starts, lines)),
                "direct": [], "accents": [], "open": chosen.get("open") if chosen.get("open") in OPENS else None,
                "drop": None}
        drop = chosen.get("drop")
        if isinstance(drop, int) and 1 < drop <= len(lines):
            plan["drop"] = starts[drop - 1]
        last_card, labelled = -9, set()
        for k, (e, beat, look, d) in enumerate(zip(*planned, checks)):
            e = checked(e, d)
            look = {"move": look.get("move") if look.get("move") in MOVES else None,
                    "pace": look.get("pace") if look.get("pace") in ("quick", "normal", "slow") else None,
                    "cut": look.get("cut") if look.get("cut") in CUTS else None, "leak": look.get("leak") is True,
                    "caption_fx": look.get("caption_fx"), "air": look.get("air") if look.get("air") in AIRS else None}
            plan["direct"].append((starts[k], look))
            if planned[2][k].get("accent") in ACCENTS:
                plan["accents"].append((place(lines, starts, k, planned[2][k].get("accent_word")), planned[2][k]["accent"]))
            if e.get("kind") == "card":
                cid = hf.resolve(str(e.get("card", "")))
                ok = (cid and hf.catalog()[cid]["tier"] == "tier1" and f"card {cid}" not in maps
                      and hf.filled(cid, e.get("slots") or {}) >= 0.7)
                e = {**e, "card": cid} if ok else {**e, "kind": "caption", "title": headline(e)}
                maps.add(f"card {cid}")
            if e.get("kind") == "map":  # the same map twice is a repeat, the second becomes a caption
                where = tuple(sorted(x.get("name", "") for x in e.get("places", [])))
                e = {**e, "kind": "caption", "title": headline(e)} if where in maps else e
                maps.add(where)
            label = beat.get("label") if isinstance(beat.get("label"), dict) and beat["label"].get("title") else None
            if e.get("kind") and (k - last_card >= 3 or e["kind"] in ("title", "caption")):
                if e["kind"] == "title" and (i > 1 or any(x[1]["kind"] == "title" for x in plan["elements"])):
                    e = {**e, "kind": "caption"}
                last_card = k if e["kind"] != "caption" else last_card
            elif label:
                e = {**label, "kind": "caption", "word": str(label["title"]).split()[0]}
            elif k - 1 not in labelled:
                e = backup_label(lines[k]) or {}
            if e.get("kind"):
                plan["elements"].append((place(lines, starts, k, e.get("word"), labelled), e))
                labelled.add(k)
            # colibri scores tension from 0 (calm) to 3 (shocking)
            guess = ("calm", "mystery", "tension", "tension")[min(3, max(0, round(d["mood"])))] if d else "calm"
            plan["moods"].append((starts[k], beat.get("mood") or guess))
            plan["ambience"].append((starts[k], beat.get("ambience") or "none"))
            recent = any(t >= starts[max(0, k - 8)] for t in plan["turns"])  # colibri adds a missed turn, one in 8 at most
            if beat.get("turn") or (d and d["turn"] >= 0.85 and not recent):
                plan["turns"].append(starts[k])
            if beat.get("sound") in MOMENTS:
                plan["sounds"].append((place(lines, starts, k, beat.get("sound_word")), beat["sound"]))
        shown += [f"{e['kind']}: {headline(e)}" for _, e in plan["elements"] if e["kind"] not in ("caption", "title")]
        out.append(plan)
    return out


def anchor(sentence, word):
    """How many words into the sentence the element should land: the first word that matches, or None."""
    clean = lambda x: re.sub(r"\W", "", x.lower())
    target = clean(str(word or "").split()[0]) if word else ""
    return next((k for k, x in enumerate(sentence.split()) if target and target in clean(x)), None)


def place(lines, starts, k, word, taken=()):
    """Word index for an element planned on sentence k. The model sometimes puts it one sentence off, so the word
    is looked for in sentence k first, then the one before and after (unless that sentence already has an element);
    without a match it lands at the start of k."""
    for j in (k, k - 1, k + 1):
        if 0 <= j < len(lines) and (j == k or j not in taken) and (a := anchor(lines[j], word)) is not None:
            return starts[j] + a
    return starts[k]


def read_script(path):
    head, *blocks = re.split(r"^## ", path.read_text(encoding="utf-8"), flags=re.M)
    thumb = re.search(r"^thumb: (.+)", head, re.M)
    script = {"title": re.search(r"^# (.+)", head, re.M).group(1).strip(), "chapters": []}
    script["thumb"] = thumb.group(1).strip() if thumb else script["title"]
    for b in blocks:
        name, body = b.split("\n", 1)
        meta = dict(re.findall(r"^(card|visuals): (.*)$", body, re.M))
        body = re.sub(r"^(card|visuals|scenes): .*$", "", body, flags=re.M)
        pars, shots = [], []
        for par in re.split(r"\n\s*\n", body.strip()):
            lines = []
            for line in par.splitlines():
                if line.startswith(">>") and shots:
                    shots[-1][3].append(line[2:].strip())
                elif line.startswith(">"):
                    shots.append([line[1:].strip(), 0, "", []])
                elif line.strip():
                    if not shots or (lines == [] and shots[-1][1]):
                        shots.append([None, 0, "", []])
                    shots[-1][1] += len(line.split())
                    shots[-1][2] += line + " "
                    lines.append(line.strip())
            if lines:
                pars.append(" ".join(lines))
        script["chapters"].append({
            "title": name.strip(),
            "card": meta.get("card", name).strip(),
            "visuals": [v.strip() for v in meta.get("visuals", name).split("|") if v.strip()],
            "text": "\n".join(pars),
            "shots": [(p, n, t.strip(), moves) for p, n, t, moves in shots if n],
        })
    return script


def stamp(sec):
    h, rem = divmod(int(sec), 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def youtube_meta(script, starts, credits, out):
    meta = llm(META.format(title=script["title"], chapters=[c["title"] for c in script["chapters"]]), as_json=True, fast=True)
    lines = ["TITLE OPTIONS", *meta["titles"], "", "DESCRIPTION", meta["description"], "", "Chapters"]
    lines += [f"{stamp(t)} {c['title']}" for t, c in zip(starts, script["chapters"])]
    lines += ["", "Image credits", *credits, "", "TAGS", ", ".join(meta["tags"])]
    out.write_text("\n".join(lines), encoding="utf-8")
