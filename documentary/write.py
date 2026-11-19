"""The script of a documentary, written only from the claims ledger, and its picture list.

Every chapter is given the claims it may use, with their status: a "confirmed" claim is told as fact, a "reported"
one names who reported it, an "alleged" or "disputed" one is told as an allegation with both sides. "Unverified"
claims (one popular site, no historian behind them) are left out, so legends copied from blog to blog stay out. The length is
held to the voice's real pace (188 words a minute): a chapter that comes back short is sent back for more.
Pictures are literal: a beat that rests on an article shows that article's screenshot, with the very words the
narration rests on marked in yellow when they can be found on the page; the others get a realistic scene of what the
sentence says (no metaphors, no children, no animals the story does not have). A religious claim names its Sunni
book when it is told.
"""
import json
import os
import re

import assets
import research
import writer

SPOKEN = 188
# beats between two article screenshots; at 1 nearly every other beat was a page and half the film was screenshots
GAP = int(os.getenv("DOC_SOURCE_GAP", "4"))
WORDS = int(os.getenv("WORDS_PER_BEAT", "22"))
REFERENCE = ("wikipedia.org", "wikisource.org", "britannica.com", "worldhistory.org", "iranicaonline.org")  # one sentence or two per beat, each with its own picture

OUTLINE = """Plan a {minutes} minute documentary: "{title}". It is built only from the research ledger below.
{n} chapters that tell it as one story with rising stakes. The first chapter opens on the most gripping true moment
or the central question, in its first two sentences; background comes after that.
Give every chapter the numbers of all the ledger claims that belong to it, usually 60 to 120: the writer picks
from them, so a fuller list makes a richer, better sourced chapter. A claim that links two chapters may be in both.
Return JSON: {{"title": "clickable and true, under 70 characters", "thumb": "3 to 5 words",
"chapters": [{{"title": "...", "card": "2 to 4 words", "brief": "what this chapter covers and how it ends",
"claims": [1, 2, 3]}}]}}

LEDGER
{ledger}"""

CHAPTER = """You are writing chapter {i} of {n} of the documentary "{title}".
All chapters: {plan}
This chapter: {chapter}. {brief}
The narration before this chapter ended with: {prev}

Write about {words} spoken words. Use only the claims below; add no fact, number, name or quote of your own.
Use as many of them as the length allows: a claim behind at least every second sentence, details, numbers and
names over general lines, and claims confirmed by several sources first.
- confirmed: tell it as fact.
- reported: say who reported it ("according to Reuters", "the filing shows").
- alleged or disputed: say it is alleged, by whom, and what the other side answers. Never state it as fact.
- unverified: only one popular source says it; leave it out unless the story cannot go on without it, and then say
  "some accounts claim".
- a claim marked with a Book: name the book when you tell it ("according to Sahih al-Bukhari", "Ibn Kathir
  writes in al-Bidaya"), with respect for the Prophet and his Companions. If it also has Experts, say that
  historians agree; if its Note shows they differ, give both views.
- a claim marked with Experts may name them ("historian Steven Runciman writes", "Britannica notes").
Plain spoken English, short sentences, present tension, no headings, no lists, no "in this video", no greeting.
{extra}
After a sentence that rests on a claim, put its number in square brackets, like [12]. One paragraph per idea,
a blank line between paragraphs.

CLAIMS
{claims}"""

MORE = """This chapter has {have} words and needs about {words}. Write {gap} more words that continue it, using
claims from the list that it has not used yet, with the same rules and the same [number] marks. Only the new text.

CHAPTER SO FAR
{text}

CLAIMS
{claims}"""

SHOTS = """Documentary "{title}"{about}. For each numbered beat of narration, describe the one picture on screen:
a realistic documentary photograph of exactly what that beat says, starting "Wide shot of" or "Close shot of":
the place, the people as adults with their clothes, the objects. Begin every prompt with the year and the place
(for example "636 CE, the plain of al-Qadisiyyah, Sasanian Persia:"), so the picture keeps to that time.
Everything in it must exist in that year and place: weapons, armour, clothes, buildings, animals. Nothing from a
later century (no firearms, no cannons, no top hats, no modern uniforms) and no generic stand-in (a treasure chest
for "wealth", a lone thinker for "strategy").
Literal, never a metaphor (no chess pieces, no animals, no children, no scales) and no text in the picture.
People who are real and living are shown from behind or far away, never as a recognisable face.
Return JSON: {{"prompts": ["...", "..."]}} with exactly {n} prompts.

{beats}"""


def lines(claims, ids=None):
    return "\n".join(f"{c['id']}. ({c['status']}) {c['claim']}" + (f" [{c['date']}]" if c.get("date") else "")
                     + (f" Book: {', '.join(c['books'])}" if c.get("books") else "")
                     + (f" Experts: {', '.join(c['experts'][:3])}" if c.get("experts") else "")
                     + (f" Note: {c['note']}" if c.get("note") else "")
                     for c in claims if ids is None or c["id"] in ids)


def script(title, minutes, claims, sources, folder):
    """script.md in the History Studio format, from the ledger."""
    parts = folder / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    sure = [c for c in claims if c["status"] != "unverified"]
    claims = sure if len(sure) >= 150 else claims  # a thin ledger keeps them, marked unverified
    f = parts / "outline.json"
    n = max(5, round(minutes / 4))
    if not f.exists():
        writer.say(f"planning {n} chapters")
        f.write_text(json.dumps(writer.llm(OUTLINE.format(minutes=minutes, title=title, n=n, ledger=lines(claims)),
                                           as_json=True, temp=0.6), ensure_ascii=False, indent=1), encoding="utf-8")
    plan = json.loads(f.read_text(encoding="utf-8"))
    chapters = plan["chapters"]
    words = minutes * SPOKEN // len(chapters)
    by_id = {c["id"]: c for c in claims}
    # only pages from an edited reference, a university, a museum or a Sunni hadith site go on screen as proof
    shot_of = {s["id"]: s for s in sources if s.get("shot") and research.trusted(s["url"])}
    summary = " | ".join(f"{i}. {c['title']}" for i, c in enumerate(chapters, 1))
    prev, used, cards = "(this is the opening)", set(), []
    md = [f"# {plan['title']}", f"thumb: {plan['thumb']}", ""]
    for i, c in enumerate(chapters, 1):
        f = parts / f"ch{i:02d}.txt"
        own = lines(claims, set(c.get("claims", []))) or lines(claims)
        if not f.exists():
            writer.say(f"writing chapter {i} of {len(chapters)}")
            extra = ("Open on the hook: the most gripping true moment or the question, in the first two sentences."
                     if i == 1 else "End the film on what is known, what is still open and one last thought."
                     if i == len(chapters) else "End on an open question that leads into the next chapter.")
            text = writer.llm(CHAPTER.format(i=i, n=len(chapters), title=plan["title"], plan=summary, chapter=c["title"],
                                             brief=c.get("brief", ""), prev=prev, words=words, extra=extra, claims=own))
            for _ in range(2):  # a short chapter is the reason a "25 minute" video came out at 15
                have = len(re.sub(r"\[\d+\]", "", text).split())
                if have >= 0.9 * words:
                    break
                writer.say(f"chapter {i} has {have} of {words} words, asking for more")
                text += "\n\n" + writer.llm(MORE.format(have=have, words=words, gap=words - have, text=text,
                                                        claims=own + "\n" + lines(claims)))
            f.write_text(text, encoding="utf-8")
        text = f.read_text(encoding="utf-8").strip()
        prev = re.sub(r"\[\d+\]", "", text)[-500:]

        beats = [b for par in writer.beats(text, WORDS) for b in par]
        f = parts / f"shots{i:02d}.json"
        if not f.exists():
            writer.say(f"picture list for chapter {i} of {len(chapters)}")
            clean = [re.sub(r"\s*\[\d+(?:,\s*\d+)*\]", "", b) for b in beats]
            about = f" (about: {os.environ['DOC_SUBJECT']})" if os.getenv("DOC_SUBJECT") else ""
            got = writer.llm(SHOTS.format(title=plan["title"], about=about, n=len(beats),
                                          beats="\n".join(f"{k}. {b}" for k, b in enumerate(clean, 1))),
                             as_json=True, temp=0.6, fast=True)["prompts"]
            f.write_text(json.dumps((got + [c["title"]] * len(beats))[:len(beats)], ensure_ascii=False), encoding="utf-8")
        pics = json.loads(f.read_text(encoding="utf-8"))

        md += [f"## {c['title']}", f"card: {c.get('card') or c['title']}", f"visuals: {c['title']}", ""]
        since = 9
        for beat, pic in zip(beats, pics):
            cited = [int(x) for group in re.findall(r"\[([\d,\s]+)\]", beat) for x in group.split(",") if x.strip()]
            # the article itself, for a beat that rests on one: each article once, with GAP beats between them
            src, cid = next(((shot_of[s], cid) for cid in cited for s in by_id.get(cid, {}).get("sources", [])
                             if s in shot_of and s not in used), (None, None))
            if src and since >= GAP:
                used.add(src["id"])
                cards.append((len(md), src, cid, by_id[cid].get("quotes", {}).get(str(src["id"]), "")))
                # the beat keeps its picture too: the page shows briefly, then the scene it speaks of
                # an encyclopedia's date is when its page was first made (Wikipedia showed 2002), so none is shown
                date = "" if any(r in src["site"] for r in REFERENCE) else src.get("published", "")
                pic, since = f"source: {src['shot']} | {src['site']} | {date} | {pic}", 0
            else:
                since += 1
            md += [f"> {pic}", re.sub(r"\s*\[[\d,\s]+\]", "", beat)]
        md.append("")
    marked = assets.quote_shots([(src, cid, q) for _, src, cid, q in cards], folder / "research" / "shots")
    for at, src, cid, _ in cards:  # the card shows the marked words where the page has them
        if (src["id"], cid) in marked:
            md[at] = md[at].replace(src["shot"], str(marked[(src["id"], cid)]))
    (folder / "script.md").write_text("\n".join(md), encoding="utf-8")
    total = sum(len(l.split()) for l in md if l and not l.startswith(("#", ">", "card:", "visuals:", "thumb:")))
    print(f"  script: {total} words, about {total / SPOKEN:.1f} minutes, {len(used)} source screenshots in it", flush=True)
    return folder / "script.md"
