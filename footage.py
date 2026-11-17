import hashlib
import json
import os
import re
import subprocess

import requests

from render import FFMPEG

UA = {"User-Agent": f"HistoryStudio/1.0 ({os.getenv('CONTACT') or 'personal documentary tool'})"}
refused = set()


def pexels(query):
    key = os.getenv("PEXELS_API_KEY")
    if not key:
        return []
    r = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": key}, timeout=30,
                     params={"query": query, "orientation": "landscape", "per_page": 8}).json()
    found = []
    for v in r.get("videos", []):
        files = [f for f in v["video_files"] if f.get("width") and f["width"] <= 1920 and f["file_type"] == "video/mp4"]
        if files and v["duration"] >= 4:
            best = max(files, key=lambda f: f["width"])
            found.append((best["link"], f"Video by {v['user']['name']} on Pexels"))
    return found


def pixabay(query):
    key = os.getenv("PIXABAY_API_KEY")
    if not key:
        return []
    r = requests.get("https://pixabay.com/api/videos/", timeout=30,
                     params={"key": key, "q": query, "per_page": 8, "safesearch": "true"}).json()
    return [(h["videos"]["large"]["url"] or h["videos"]["medium"]["url"], f"Video by {h['user']} on Pixabay")
            for h in r.get("hits", []) if h["duration"] >= 4]


SYNTHETIC = re.compile(r"animation|visuali[sz]ation|simulation|render|computer.generated|diagram|svs\d|satellite|"
                       r"timelapse of earth|nasa|music|song|lecture|interview|slideshow|screencast|experiment|spectrum|srep", re.I)


def commons(query):
    """Commons search is loose ("sea waves" also finds "gravitational wave"), so every word of the query has to
    appear in the file's title or description, and animations or screen recordings are left out."""
    words = [w.lower().rstrip("s") for w in query.split() if len(w) > 2]
    r = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=30, params={
        "action": "query", "generator": "search", "gsrsearch": f"{query} filetype:video", "gsrnamespace": 6,
        "gsrlimit": 10, "prop": "videoinfo", "viprop": "url|size|derivatives|extmetadata", "format": "json"}).json()
    found = []
    for p in sorted(r.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0)):
        info = p["videoinfo"][0]
        sizes = [d for d in info.get("derivatives", []) if "webm" in d["type"] and 720 <= d.get("height", 0) <= 1080]
        if not sizes or info.get("duration", 0) < 4:
            continue
        meta = info.get("extmetadata", {})
        about = re.sub(r"<[^>]+>", " ", p["title"] + " " + meta.get("ImageDescription", {}).get("value", "") + " " +
                       meta.get("Categories", {}).get("value", "")).lower()
        if not all(w in about for w in words) or SYNTHETIC.search(about):
            continue
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "unknown")).strip()
        lic = meta.get("LicenseShortName", {}).get("value", "")
        found.append((max(sizes, key=lambda d: d["height"])["src"], f"{p['title'][5:]}, {artist}, {lic}, Wikimedia Commons"))
    return found


def seconds(path):
    out = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0


def clips(query, need, folder):
    """Real footage for one beat: licensed stock first, then Wikimedia Commons. Returns [(path, credit)]."""
    folder.mkdir(parents=True, exist_ok=True)
    index = folder / "clips.json"
    known = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {}
    if not known.get(query):
        words = query.split()
        tries, found = list(dict.fromkeys([query, " ".join(words[:2]), " ".join(words[-2:])])), []
        for source in (pexels, pixabay, commons):
            for q in tries:
                try:
                    found += source(q)
                except (requests.RequestException, KeyError, ValueError):
                    pass
                if len(found) >= need + 2:
                    break
            if len(found) >= need + 2:
                break
        got = []
        for url, credit in found:
            host = url.split("/")[2]
            dst = folder / f"v{hashlib.md5(url.encode()).hexdigest()[:10]}{os.path.splitext(url.split('?')[0])[1] or '.mp4'}"
            if host in refused:
                continue
            if not dst.exists():
                try:
                    r = requests.get(url, headers=UA, timeout=120, stream=True)
                    if r.status_code == 429:
                        r.close()
                        refused.add(host)
                        print(f"    {host} refused downloads (429). Add CONTACT in .env or a PEXELS_API_KEY.")
                        continue
                    with r:
                        r.raise_for_status()
                        with open(dst, "wb") as f:
                            for chunk in r.iter_content(1 << 20):
                                f.write(chunk)
                except requests.RequestException:
                    dst.unlink(missing_ok=True)
                    continue
            if seconds(dst) >= 3:
                got.append((dst.name, credit))
            if len(got) >= need:
                break
        known[query] = got
        index.write_text(json.dumps(known, indent=1), encoding="utf-8")
        print(f"    footage '{query}': {len(got)} clip(s)")
    return [(folder / name, credit) for name, credit in known[query]]
