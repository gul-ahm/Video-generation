"""Proof on screen: screenshots of the articles the script leans on, checked before they are used.

A headless Chromium opens each page, closes the cookie banner if it finds one and hides bars that stick to the
screen. Before anything is saved, the page the browser really got is checked against the text research read: a
Cloudflare check, an "Access denied" wall or a captcha shares almost no words with the article, so it is refused.
Earlier the text and the screenshot came from two different fetches, and a check page once reached the film.

screenshots()  one verified shot per source at its headline (s<id>.png)
quote_shots()  for each source card the script uses: the exact words the narration rests on, found on the page,
               marked in yellow and scrolled to the middle of the screen (s<id>_c<claim>.png); when the words
               cannot be found, the verified headline shot stays
Photos are never lifted out of an article: the film shows the page as a source card with the outlet and date.
"""
import re
import time

CLEAN = """
() => {
  for (const b of document.querySelectorAll('button, a')) {
    const t = (b.innerText || '').trim().toLowerCase();
    if (/^(accept|accept all|agree|i agree|allow all|got it|ok|continue|reject all)$/.test(t)) { try { b.click(); } catch (e) {} }
  }
  const nag = /cookie|consent|privacy|personali[sz]e|subscribe|newsletter|sign up|log in|allow notifications/i;
  for (const el of document.querySelectorAll('*')) {
    const s = getComputedStyle(el);
    const floating = s.position === 'fixed' || s.position === 'sticky';
    const dialog = el.matches('[role=dialog], [role=alertdialog], [aria-modal=true], dialog');
    // bars that stick to the screen, and any overlay that asks for something, whatever its size
    if (dialog || (floating && (el.offsetHeight < innerHeight * 0.6 || nag.test(el.innerText || '')))) el.style.display = 'none';
  }
  document.documentElement.style.overflow = document.body.style.overflow = 'auto';
  const h = document.querySelector('h1');
  if (h) scrollTo(0, Math.max(0, h.getBoundingClientRect().top + scrollY - 120));
  return h ? h.innerText.trim().slice(0, 160) : '';
}
"""

# finds the words on the page, lays a yellow mark over them and centres them; returns false when they are not there
MARK = """
(texts) => {
  for (const t of texts) {
    getSelection().removeAllRanges();
    scrollTo(0, 0);
    if (!window.find(t, false, false, true)) continue;
    const range = getSelection().getRangeAt(0);
    const box = range.getBoundingClientRect();
    scrollTo(0, Math.max(0, box.top + scrollY - innerHeight / 2));
    for (const r of range.getClientRects()) {
      const m = document.createElement('div');
      Object.assign(m.style, {position: 'absolute', left: (r.left + scrollX - 3) + 'px', top: (r.top + scrollY - 2) + 'px',
        width: (r.width + 6) + 'px', height: (r.height + 4) + 'px', background: 'rgba(255, 214, 0, 0.45)',
        borderRadius: '3px', zIndex: 2147483647, pointerEvents: 'none'});
      document.body.appendChild(m);
    }
    getSelection().removeAllRanges();
    return true;
  }
  return false;
}
"""


# the words actually on screen, and whether the middle of the screen is covered by a dialog
VISIBLE = """
() => {
  const out = [], walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walk.nextNode()) {
    const n = walk.currentNode, el = n.parentElement;
    if (!el || !n.textContent.trim()) continue;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth && s.visibility !== 'hidden'
        && Number(s.opacity) > 0.1) out.push(n.textContent);
  }
  const mid = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
  return {text: out.join(' '), covered: !!(mid && mid.closest('[role=dialog], [aria-modal=true], dialog'))};
}
"""


def words(text):
    return {w for w in re.findall(r"[a-z]{4,}", text.lower())}


def genuine(page, source):
    """Is the page on screen the article research read, and not a block, check or error page?"""
    import research
    seen = page.inner_text("body", timeout=5000)
    article = words(" ".join(source["text"].split()[:3000]))
    share = len(article & words(seen)) / max(1, len(article))
    if share < 0.4 or (share < 0.7 and any(b in seen[:2000].lower() for b in research.BLOCKED)):
        return False, f"page does not match the article ({share:.0%} of its words)"
    # the whole page can match while a cookie or sign up box covers what the screenshot shows
    view = page.evaluate(VISIBLE)
    on_screen = words(view["text"])
    mine = len(on_screen & article) / max(1, len(on_screen))
    if view["covered"] or mine < 0.35:  # menus and sidebars are on screen too
        return False, f"the screen shows a dialog or other text, not the article ({mine:.0%} of its words)"
    return True, ""


def opened(browser, url):
    page = browser.new_page(viewport={"width": 1600, "height": 900})  # a fresh tab per page
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as err:  # the Mac lost the network for a moment: wait and try once more
        if "INTERNET_DISCONNECTED" not in str(err) and "NETWORK" not in str(err):
            page.close()
            raise
        time.sleep(30)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)
    return page


def each(todo, work):
    """Run work(browser, item) for every item, one tab each; a crashed browser is started again."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for item in todo:
            if not browser.is_connected():
                browser = p.chromium.launch()
            work(browser, item)
        browser.close()


def screenshots(sources, folder):
    """<folder>/s<id>.png for every source whose page is genuine. Adds "shot" and the page's own headline."""
    folder.mkdir(parents=True, exist_ok=True)
    refused = folder / "refused.txt"
    skip = set(refused.read_text().split()) if refused.exists() else set()
    todo = [s for s in sources if not (folder / f"s{s['id']:02d}.png").exists() and str(s["id"]) not in skip
            and not s["url"].lower().endswith(".pdf")]

    def work(browser, s):
        try:
            page = opened(browser, s["url"])
        except Exception as err:
            return print(f"  no screenshot of {s['site']} ({type(err).__name__}: {str(err).splitlines()[0][:80]})", flush=True)
        try:
            head = page.evaluate(CLEAN)
            page.wait_for_timeout(700)
            page.evaluate(CLEAN)
            ok, why = genuine(page, s)
            if not ok:
                print(f"  refused {s['site']}: {why}", flush=True)
                with refused.open("a") as f:
                    f.write(f"{s['id']}\n")
            elif head:  # no headline on screen means a wall or an error page, not worth showing
                page.screenshot(path=str(folder / f"s{s['id']:02d}.png"))
                s["headline"] = head
        except Exception as err:
            print(f"  no screenshot of {s['site']} ({type(err).__name__}: {str(err).splitlines()[0][:80]})", flush=True)
        finally:
            page.close()

    if todo:
        each(todo, work)
    for s in sources:
        shot = folder / f"s{s['id']:02d}.png"
        s["shot"] = str(shot) if shot.exists() else ""
    print(f"  screenshots: {sum(bool(s['shot']) for s in sources)} of {len(sources)} sources", flush=True)
    return sources


def quote_shots(wanted, folder):
    """wanted: [(source, claim id, quote)]. Returns {(source id, claim id): path} for the quotes found and marked."""
    out, todo = {}, []
    for s, cid, quote in wanted:
        path = folder / f"s{s['id']:02d}_c{cid}.png"
        if path.exists():
            out[(s["id"], cid)] = path
        elif quote:
            todo.append((s, cid, quote, path))

    def work(browser, item):
        s, cid, quote, path = item
        w = quote.split()
        texts = [t for t in dict.fromkeys([quote.strip() if len(quote) <= 160 else "", " ".join(w[:10]), " ".join(w[:6])])
                 if t and len(t.split()) >= 4]
        try:
            page = opened(browser, s["url"])
        except Exception:
            return
        try:
            page.evaluate(CLEAN)
            page.wait_for_timeout(700)
            page.evaluate(CLEAN)
            if genuine(page, s)[0] and page.evaluate(MARK, texts):
                page.wait_for_timeout(400)
                page.screenshot(path=str(path))
                out[(s["id"], cid)] = path
        except Exception:
            pass
        finally:
            page.close()

    if todo:
        each(todo, work)
    print(f"  quote marked on {len(out)} of {len(wanted)} source cards", flush=True)
    return out
