"""Deep research for one documentary: many sources from the open web, read in full, turned into a ledger of claims.

1. Gemini turns the title into 24 searches from different angles (background, timeline, people, numbers, weapons,
   the dispute, primary sources, academic work, what happened after), and after the first round 14 more for what
   the sources found so far do not cover.
2. Free search engines (ddgs: DuckDuckGo, Bing, Brave, Yahoo and others in turn, no key) answer each; the
   references cited by the Wikipedia articles are added. Every page or PDF is downloaded and its text taken out
   (trafilatura, pypdf), up to DOC_SOURCES sources (150) with DOC_PER_SITE (4) per site.
3. Gemini reads every source on its own and lists what it states: the claim, the exact words it rests on, who says
   it, dates and numbers.
4. The claims are merged into one ledger. A claim found on two or more independent sites is "confirmed"; on one
   site it is "reported"; where sources disagree, or it is an accusation against a person, it is "disputed" or
   "alleged", and the script has to say so.
5. Religious claims (the Prophet, the Sahaba, matters of faith) are kept only when their source is one of the Sunni
   books or sites in sunni_books.json, and they carry the book's name into the script. Everything else is researched
   on the open web as above. DOC_SUNNI=0 turns this off.
6. Every claim also records its experts: academic and reference sites (university presses, journals, .edu,
   Britannica and the like) and named historians, scholars or chroniclers the source quotes. A claim on a single
   popular site with no expert behind it is "unverified" and is kept out of the script, so a legend copied from blog
   to blog does not become a fact. A religious claim keeps its Sunni book, and the historians' view of it when they
   have one.
7. A page that is a block or check page (Cloudflare "Just a moment", "Access denied", a captcha) is never a source.
Everything is kept in <video>/research/: sources.json, claims.json and sources.md for a human to check.
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

import requests

import writer

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"}
SUNNI = json.loads((Path(__file__).parent / "sunni_books.json").read_text(encoding="utf-8"))
# words of block, check and error pages; a real article that happens to use one is long, a block page is short
BLOCKED = ("just a moment", "checking your browser", "attention required", "access denied", "verify you are human",
           "enable javascript and cookies", "are you a robot", "unusual traffic", "content unavailable",
           "page not found", "404 not found", "please enable cookies", "cloudflare ray id", "captcha",
           "subscribe to continue reading", "this site can't be reached")
# academic and reference sources: a claim backed by one of them is not a blog's guess
EXPERT_SITES = (".edu", ".ac.uk", ".ac.in", ".ac.jp", "cambridge.org", "oup.com", "jstor.org", "brill.com", "tandfonline.com",
                "springer.com", "sciencedirect.com", "muse.jhu.edu", "persee.fr", "cairn.info", "academia.edu", "britannica.com",
                "metmuseum.org", "bl.uk", "loc.gov", "doi.org", "dumbartonoaks.org", "iranicaonline.org", "islamansiklopedisi.org.tr")
SCHOLAR = re.compile(r"\b(historian|professor|prof\.|scholar|dr\.|chronicler|imam|shaykh|sheikh|ibn \w+)\b", re.I)
SKIP = ("youtube.com", "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com", "pinterest.", "reddit.com",
        "quora.com", "linkedin.com")

QUESTIONS = """We are making a serious {minutes} minute documentary: "{title}".{about}
Write the web searches a careful researcher would run to cover it completely. {n} searches, each from a different
angle: the background, the full timeline day by day, each main person, army or organisation, the key numbers, the
weapons and tactics, the central dispute and each side of it, primary sources and eyewitness accounts, official
documents, archaeology, expert and academic analysis, myths versus facts, and what happened afterwards.
Short search phrases, the way people type them. Also say whether the story belongs to Islamic history (the Prophet,
the Sahaba, the caliphates, Muslim rulers and armies) and give its main subject in a few words.
Return JSON: {{"searches": ["...", "..."], "islamic": false, "subject": "..."}}"""

GAPS = """We are researching the documentary "{title}". These are the headlines of the sources found so far:
{found}
Write {n} more web searches for what is still missing: angles, people, events, numbers, primary sources, eyewitness
accounts, academic papers, opposing views and recent findings that none of these headlines cover.
Return JSON: {{"searches": ["...", "..."]}}"""

READ = """Documentary: "{title}". Below is one source. List every fact in it that matters for this story.
For each: the claim in one plain sentence; the exact words of the source it rests on (a short quote, copied);
who says it if the source attributes it to someone; any date; whether it is an accusation or an unproven
allegation against a person or organisation; and whether it is religious (about the Prophet, the Sahaba, matters of
faith, their virtues or intentions; battles, numbers and places are not religious). Only what this text itself
states; nothing from your own knowledge.
Return JSON: {{"published": "date of the article if stated, else empty", "claims": [{{"claim": "...", "quote": "...",
"said_by": "", "date": "", "allegation": false, "religious": false}}]}}

SOURCE ({site}): {headline}
{text}"""

MERGE = """Documentary: "{title}". Below are claims taken from different sources, each with its source number.
Merge ONLY claims that state the same fact into one entry listing every source number. Never drop a claim: every
distinct fact, date, number, name and detail keeps its own entry, so the ledger stays almost as long as the list.
Where sources contradict each other, keep one entry and describe the disagreement in "note".
Return JSON: {{"claims": [{{"claim": "...", "sources": [1, 4], "date": "", "allegation": false, "note": ""}}]}}

{claims}"""

BACKENDS = {"text": ["duckduckgo", "bing", "brave", "mojeek", "yahoo", "startpage", "google"],
            "news": ["duckduckgo", "bing", "yahoo"]}
_turn = {"text": 0, "news": 0}


def search(query, n=10):
    """Web and news results for one query: [{"url", "title"}]. A search engine that refuses or rate limits is
    skipped for the next one, so one engine's limit no longer loses half the searches."""
    from ddgs import DDGS
    seen, out = set(), []
    with DDGS(timeout=15) as d:
        for kind in ("news", "text"):
            engines = BACKENDS[kind]
            for k in range(len(engines)):
                engine = engines[(_turn[kind] + k) % len(engines)]
                try:
                    rows = list(getattr(d, kind)(query, max_results=n, backend=engine))
                except Exception:
                    rows = []
                if rows:
                    _turn[kind] = (_turn[kind] + k + 1) % len(engines)  # spread the load over the engines
                    break
                time.sleep(1)
            else:
                writer.say(f"no {kind} results for '{query[:50]}'")
                rows = []
            for r in rows:
                url = r.get("url") or r.get("href") or ""
                if url and url not in seen and not any(x in url for x in SKIP):
                    seen.add(url)
                    out.append({"url": url, "title": r.get("title", "")})
    return out


def wiki_refs(title):
    """The outside links cited by the Wikipedia articles on the subject: books, papers and pages that Wikipedia
    itself rests on, which a web search rarely finds."""
    api = "https://en.wikipedia.org/w/api.php"
    try:
        hits = requests.get(api, params={"action": "query", "list": "search", "srsearch": title, "srlimit": 3,
                                         "format": "json"}, headers=UA, timeout=20).json()["query"]["search"]
        out = []
        for h in hits:
            links = requests.get(api, params={"action": "parse", "page": h["title"], "prop": "externallinks",
                                              "format": "json"}, headers=UA, timeout=20).json()["parse"]["externallinks"]
            out += [{"url": "https://en.wikipedia.org/wiki/" + h["title"].replace(" ", "_"), "title": h["title"]}]
            out += [{"url": u, "title": ""} for u in links if u.startswith("http") and not any(x in u for x in SKIP)
                    and "wikipedia.org" not in u and "wikidata.org" not in u and not u.startswith("https://books.google")]
        return out
    except Exception as err:
        writer.say(f"wikipedia references not read ({type(err).__name__})")
        return []


def read(url):
    """(text, headline, date) of a page or a PDF, or ("", "", "") for a paywall, a script only page or an error."""
    import trafilatura
    try:
        r = requests.get(url, headers=UA, timeout=25)
        if r.status_code >= 400:
            return "", "", ""
        if "pdf" in r.headers.get("content-type", "") or url.lower().endswith(".pdf"):
            import io
            from pypdf import PdfReader
            pdf = PdfReader(io.BytesIO(r.content))
            text = "\n".join((pg.extract_text() or "") for pg in pdf.pages[:40])
            return text, (pdf.metadata or {}).get("/Title", "") or "", ""
        got = trafilatura.extract(r.text, include_comments=False, include_tables=True, output_format="json",
                                  with_metadata=True)
        got = json.loads(got) if got else {}
        text = got.get("text") or ""
        if blocked(text):
            return "", "", ""
        return text, got.get("title") or "", got.get("date") or ""
    except Exception:
        return "", "", ""


def blocked(text):
    """A block, check or error page instead of the article."""
    head = text[:1500].lower()
    return len(text.split()) < 600 and any(b in head for b in BLOCKED)


def site(url):
    return urlparse(url).netloc.removeprefix("www.")


def gather(title, minutes, folder):
    """Sources with their text, from two rounds of searches (the second aimed at what the first missed) and the
    references of the Wikipedia articles. At most PER_SITE pages per site, so one outlet cannot carry the film."""
    out = folder / "sources.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    most, per = int(os.getenv("DOC_SOURCES", "150")), int(os.getenv("DOC_PER_SITE", "4"))
    found, per_site, tried = {}, {}, set()

    def take(results):
        if len(found) >= most:
            return
        fresh = [r for r in results if r["url"] not in tried and per_site.get(site(r["url"]), 0) < per]
        tried.update(r["url"] for r in fresh)
        with ThreadPoolExecutor(8) as ex:
            for r, (text, head, date) in zip(fresh, ex.map(lambda r: read(r["url"]), fresh)):
                if len(text.split()) < 250 or per_site.get(site(r["url"]), 0) >= per or len(found) >= most:
                    continue
                per_site[site(r["url"])] = per_site.get(site(r["url"]), 0) + 1
                found[r["url"]] = {"id": len(found) + 1, "url": r["url"], "site": site(r["url"]),
                                   "headline": r["title"] or head, "published": date, "words": len(text.split()),
                                   "text": text}

    known = os.getenv("DOC_SUBJECT", "")  # the event behind a headline that does not name it ("Battle of Mu'tah, 629")
    about = f"\nIt is about: {known}. Every search must be about this event." if known else ""
    plan = writer.llm(QUESTIONS.format(title=title, minutes=minutes, n=24, about=about), as_json=True, temp=0.4)
    plan["subject"] = known or plan.get("subject")
    asks = ([known] if known else []) + plan["searches"][:24]
    if plan.get("islamic") and os.getenv("DOC_SUNNI", "1") == "1":  # the Sunni books, searched by name
        subject = plan.get("subject") or title.split(":")[0]
        asks += [f"{subject} site:sunnah.com", f"{subject} Ibn Kathir al-Bidaya wa al-Nihaya", f"{subject} al-Tabari history",
                 f"{subject} Ibn Hisham sirah", f"{subject} Ibn Sa'd Tabaqat", f"{subject} al-Dhahabi Siyar", f"{subject} site:archive.org",
                 f"{subject} Cambridge History of Islam", f"{subject} Encyclopaedia of Islam"]
    subject = plan.get("subject") or title.split(":")[0]  # what historians themselves write
    asks += [f"{subject} historian analysis", f"{subject} site:edu", f"{subject} journal article", f"{subject} Britannica"]
    for q in [title, *asks]:
        writer.say(f"searching: {q}")
        take(search(q))
    writer.say("wikipedia references")
    take(wiki_refs(known or title)[:200])
    heads = "\n".join(f"- {s['headline']} ({s['site']})" for s in found.values())
    more = writer.llm(GAPS.format(title=title, found=heads, n=14), as_json=True, temp=0.5)["searches"][:14]
    for q in more:
        writer.say(f"searching what is missing: {q}")
        take(search(q))
    sources = list(found.values())
    writer.say(f"{len(sources)} sources from {len(per_site)} sites read ({len(tried)} pages tried)")
    out.write_text(json.dumps(sources, ensure_ascii=False, indent=1), encoding="utf-8")
    return sources


def ledger(title, sources, folder):
    """claims.json: every claim with its sources and how sure it is."""
    out = folder / "claims.json"
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    sources[:] = [s for s in sources if not blocked(s["text"])]  # sources saved before the block check
    def study(s):
        cache = folder / f"read_{s['id']:03d}.json"
        if not cache.exists():
            try:
                got = writer.llm(READ.format(title=title, site=s["site"], headline=s["headline"],
                                             text=" ".join(s["text"].split()[:6000])), as_json=True, temp=0.1, fast=True)
            except (Exception, SystemExit) as err:
                writer.say(f"source {s['id']} not read ({str(err)[:60]})")
                return []
            cache.write_text(json.dumps(got, ensure_ascii=False), encoding="utf-8")
        got = json.loads(cache.read_text(encoding="utf-8"))
        s["published"] = s.get("published") or got.get("published", "")
        # a quote that is not in the page was made up by the model: such a claim is dropped
        flat = re.sub(r"\W+", " ", s["text"].lower())
        return [{**c, "source": s["id"]} for c in got.get("claims", [])
                if len(w := re.sub(r"\W+", " ", str(c.get("quote", "")).lower()).split()) >= 4 and " ".join(w[:6]) in flat]

    with ThreadPoolExecutor(4) as ex:
        raw = [c for got in ex.map(study, sources) for c in got]
    writer.say(f"{len(raw)} checked claims, merging")
    merged = []
    for k in range(0, len(raw), 50):  # small pieces: a long list made the model fold 214 claims into 85
        piece = raw[k:k + 50]
        lines = "\n".join(f"[{c['source']}] {c['claim']}" + (" (allegation)" if c.get("allegation") else "")
                          + (f" ({c['date']})" if c.get("date") else "") for c in piece)
        try:
            got = writer.llm(MERGE.format(title=title, claims=lines), as_json=True, temp=0.1)["claims"]
        except (Exception, SystemExit):
            got = []
        if len(got) < 0.4 * len(piece):  # the model dropped facts: keep the piece as it came
            got = [{"claim": c["claim"], "sources": [c["source"]], "date": c.get("date", ""),
                    "allegation": bool(c.get("allegation")), "note": ""} for c in piece]
        merged += got
    kept = []  # the same fact from two pieces becomes one entry
    for c in merged:
        twin = next((x for x in kept if SequenceMatcher(None, x["claim"].lower(), c["claim"].lower()).quick_ratio() > 0.9
                     and SequenceMatcher(None, x["claim"].lower(), c["claim"].lower()).ratio() > 0.85), None)
        if twin:
            twin["sources"] = sorted(set(twin.get("sources", [])) | set(c.get("sources", [])))
        else:
            kept.append(c)
    merged = kept
    by_source, site_of = {}, {s["id"]: s["site"] for s in sources}
    for c in raw:
        by_source.setdefault(c["source"], []).append(c)
    for c in merged:  # the words each source uses for this claim: the screenshot highlights them
        best = {i: max(by_source.get(i, []), default=None,
                       key=lambda r: SequenceMatcher(None, r["claim"].lower(), c["claim"].lower()).ratio())
                for i in c.get("sources", [])}
        c["quotes"] = {str(i): r["quote"] for i, r in best.items() if r}
        c["religious"] = any(r.get("religious") for r in best.values() if r)
        c["experts"] = list(dict.fromkeys(
            [site_of[i] for i in best if any(site_of.get(i, "").endswith(x) or x + "." in site_of.get(i, "") for x in EXPERT_SITES)]
            + [r["said_by"] for r in best.values() if r and SCHOLAR.search(str(r.get("said_by", "")))]))
    dropped = sunni(merged, sources) if os.getenv("DOC_SUNNI", "1") == "1" else []
    merged = [c for c in merged if c not in dropped]
    where = {s["id"]: s["site"] for s in sources}
    for n, c in enumerate(merged, 1):
        c["sources"] = [i for i in c.get("sources", []) if i in where]
        sites = {where[i] for i in c["sources"]}
        c["id"] = n
        c["status"] = ("alleged" if c.get("allegation") else "disputed" if c.get("note") else
                       "confirmed" if len(sites) >= 2 else
                       "reported" if c.get("experts") or c.get("books") else "unverified")
    merged = [c for c in merged if c["sources"]]
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=1), encoding="utf-8")
    md = [f"# Sources for: {title}", "", f"{len(sources)} sources, {len(merged)} claims "
          f"({sum(c['status'] == 'confirmed' for c in merged)} confirmed by two or more sites, "
          f"{sum(bool(c.get('experts')) for c in merged)} backed by historians or academic sources, "
          f"{sum(c['status'] == 'unverified' for c in merged)} unverified (one popular site, kept out of the script), "
          f"{sum(bool(c.get('books')) for c in merged)} religious from the Sunni books, "
          f"{len(dropped)} religious claims dropped for want of a Sunni source).", ""]
    md += [f"{s['id']}. [{s['headline'] or s['site']}]({s['url']}) ({s['site']}{', ' + s['published'] if s.get('published') else ''})"
           for s in sources]
    md += ["", "## Claims", ""] + [f"- **{c['status']}** {c['claim']} [{', '.join(map(str, c['sources']))}]"
                                   + (f" Book: {', '.join(c['books'])}" if c.get("books") else "")
                                   + (f" Experts: {', '.join(c['experts'])}" if c.get("experts") else "")
                                   + (f" Note: {c['note']}" if c.get("note") else "") for c in merged]
    if dropped:
        md += ["", "## Religious claims dropped (no Sunni book or site behind them)", ""]
        md += [f"- {c['claim']} [{', '.join(map(str, c.get('sources', [])))}]" for c in dropped]
    (folder.parent / "sources.md").write_text("\n".join(md), encoding="utf-8")
    return merged


def sunni(claims, sources):
    """Religious claims need a source from the Sunni list: an approved site, or the source naming an approved book
    near the quoted words. Those keep the book names in "books"; the others are returned to be dropped."""
    text = {s["id"]: s["text"].lower() for s in sources}
    sites = {s["id"]: s["site"] for s in sources}
    dropped = []
    for c in claims:
        if not c.get("religious"):
            continue
        books = []
        for i in c.get("sources", []):
            t, q = text.get(i, ""), c.get("quotes", {}).get(str(i), "").lower()
            at = t.find(" ".join(q.split()[:6])) if q else -1
            near = t[max(0, at - 600):at + 600] if at >= 0 else ""
            named = [b["title"] for b in SUNNI["books"] if any(n in near for n in b["names"])]
            if named or any(sites.get(i, "").endswith(x) for x in SUNNI["sites"]):
                books += named or [sites[i]]
        if books:
            c["books"] = list(dict.fromkeys(books))
        else:
            dropped.append(c)
    return dropped
