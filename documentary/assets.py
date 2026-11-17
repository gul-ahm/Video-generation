"""Proof on screen: a screenshot of each article the script leans on, taken at its headline.

A headless Chromium opens the page, closes the cookie banner if it finds one, hides bars that stick to the screen,
scrolls to the headline and saves what a reader would see (1600 x 900). The film shows it as a source card with the
outlet and date, which is how documentaries cite the press; photos are never lifted out of an article.
"""
import time
from pathlib import Path

CLEAN = """
() => {
  for (const b of document.querySelectorAll('button, a')) {
    const t = (b.innerText || '').trim().toLowerCase();
    if (/^(accept|accept all|agree|i agree|allow all|got it|ok|continue|reject all)$/.test(t)) { try { b.click(); } catch (e) {} }
  }
  for (const el of document.querySelectorAll('*')) {
    const s = getComputedStyle(el);
    if ((s.position === 'fixed' || s.position === 'sticky') && el.offsetHeight < innerHeight * 0.6) el.style.display = 'none';
  }
  const h = document.querySelector('h1');
  if (h) scrollTo(0, Math.max(0, h.getBoundingClientRect().top + scrollY - 120));
  return h ? h.innerText.trim().slice(0, 160) : '';
}
"""


def screenshots(sources, folder):
    """<folder>/s<id>.png for every source that opens. Adds "shot" and the page's own headline to each source."""
    from playwright.sync_api import sync_playwright
    folder.mkdir(parents=True, exist_ok=True)
    todo = [s for s in sources if not (folder / f"s{s['id']:02d}.png").exists()]
    if todo:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for s in todo:
                if s["url"].lower().endswith(".pdf"):
                    continue
                if not browser.is_connected():  # one bad page can take the browser down; the rest still get shot
                    browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1600, "height": 900})  # a fresh tab per page
                try:
                    try:
                        page.goto(s["url"], wait_until="domcontentloaded", timeout=30000)
                    except Exception as err:  # the Mac lost the network for a moment: wait and try once more
                        if "INTERNET_DISCONNECTED" not in str(err) and "NETWORK" not in str(err):
                            raise
                        time.sleep(30)
                        page.goto(s["url"], wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2500)
                    head = page.evaluate(CLEAN)
                    page.wait_for_timeout(700)
                    page.evaluate(CLEAN)
                    if head:  # no headline on screen means a wall or an error page, not worth showing
                        page.screenshot(path=str(folder / f"s{s['id']:02d}.png"))
                        s["headline"] = head
                except Exception as err:
                    print(f"  no screenshot of {s['site']} ({type(err).__name__}: {str(err).splitlines()[0][:80]})", flush=True)
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
            browser.close()
    for s in sources:
        shot = folder / f"s{s['id']:02d}.png"
        s["shot"] = str(shot) if shot.exists() else ""
    print(f"  screenshots: {sum(bool(s['shot']) for s in sources)} of {len(sources)} sources", flush=True)
    return sources
