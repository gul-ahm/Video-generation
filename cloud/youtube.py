"""A finished film to YouTube, scheduled for its publish slot: the upload stage of the worker's pipeline.

The title, description, hashtags, tags and captions come from the film; the rest from /settings on Telegram
(visibility, category, made for kids, language, license, notify subscribers, embedding). Paid promotion and
altered or synthetic content are No. Age restriction and comments are not open to the API: set them in Studio.
"""
import json
import os
import time
from pathlib import Path

import requests

import drive

UPLOAD = "https://www.googleapis.com/upload/youtube/v3"
CATEGORY = {"Education": "27", "Entertainment": "24", "People & Blogs": "22", "Film & Animation": "1"}
LANGUAGE = {"English": "en", "Hindi": "hi", "Urdu": "ur"}
CHUNK = 32 * 1024 * 1024


def tags(words, room=480):
    """As many tags as fit YouTube's 500 characters (a tag with a space counts its quotes)."""
    out, used = [], 0
    for t in dict.fromkeys(w.strip() for w in words if w and w.strip()):
        cost = len(t) + (2 if " " in t else 0) + 1
        if used + cost > room:
            break
        out.append(t)
        used += cost
    return out


def body(pack, title, publish_at, settings):
    public = settings.get("visibility", "Public") == "Public"
    lang = LANGUAGE.get(settings.get("language"), "en")
    return {
        "snippet": {"title": title[:100], "description": pack.get("description", "")[:5000],
                    "tags": tags(pack.get("tags", [])), "categoryId": CATEGORY.get(settings.get("category"), "27"),
                    "defaultLanguage": lang, "defaultAudioLanguage": lang},
        # a public film waits private until its slot, then YouTube publishes it; an unlisted one is unlisted at once
        "status": {"privacyStatus": "private" if public else "unlisted", **({"publishAt": publish_at} if public else {}),
                   "selfDeclaredMadeForKids": settings.get("made_for_kids") == "Yes",
                   "license": "creativeCommon" if settings.get("license") == "Creative Commons" else "youtube",
                   "embeddable": settings.get("embedding", "Yes") == "Yes", "containsSyntheticMedia": False},
        "paidProductPlacementDetails": {"hasPaidProductPlacement": False},
    }


def send(video, meta, notify):
    """A resumable upload in chunks: a dropped connection goes on from the last byte YouTube has."""
    size = video.stat().st_size
    auth = {"Authorization": f"Bearer {drive.token('youtube')}"}
    r = requests.post(f"{UPLOAD}/videos", params={"uploadType": "resumable", "part": ",".join(meta),
                                                  "notifySubscribers": str(notify).lower()},
                      headers={**auth, "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
                      json=meta, timeout=60)
    if not r.ok:
        raise RuntimeError(f"YouTube refused the upload: {r.status_code} {r.text[:300]}")
    where, at, fails = r.headers["Location"], 0, 0
    with open(video, "rb") as f:
        while True:
            f.seek(at)
            piece = f.read(CHUNK)
            try:
                r = requests.put(where, data=piece, timeout=600, headers={
                    **auth, "Content-Range": f"bytes {at}-{at + len(piece) - 1}/{size}"})
            except requests.RequestException:
                r = None
            if r is not None and r.status_code in (200, 201):
                return r.json()["id"]
            if r is not None and r.status_code == 308:
                at = int(r.headers["Range"].split("-")[1]) + 1 if "Range" in r.headers else 0
                fails = 0
                continue
            fails += 1
            if fails > 6 or (r is not None and r.status_code < 500):
                raise RuntimeError(f"the upload stopped at {at} of {size} bytes: {r.status_code if r is not None else 'no answer'}"
                                   f" {r.text[:200] if r is not None else ''}")
            time.sleep(10 * fails)
            asked = requests.put(where, headers={**auth, "Content-Range": f"bytes */{size}"}, timeout=60)
            if asked.status_code in (200, 201):
                return asked.json()["id"]
            at = int(asked.headers["Range"].split("-")[1]) + 1 if "Range" in asked.headers else 0


def extras(vid, thumbnail, subtitles, lang):
    """The thumbnail and the captions; either can fail (a channel without phone verification cannot set thumbnails)
    without the film going again. Returns what did not go."""
    auth, missed = {"Authorization": f"Bearer {drive.token('youtube')}"}, []
    if thumbnail and thumbnail.exists():
        r = requests.post(f"{UPLOAD}/thumbnails/set", params={"videoId": vid, "uploadType": "media"},
                          headers={**auth, "Content-Type": "image/jpeg"}, data=thumbnail.read_bytes(), timeout=120)
        missed += [] if r.ok else [f"thumbnail ({r.status_code} {r.text[:120]})"]
    if subtitles and subtitles.exists():
        meta = json.dumps({"snippet": {"videoId": vid, "language": lang, "name": ""}})
        r = requests.post(f"{UPLOAD}/captions", params={"part": "snippet", "uploadType": "multipart"}, headers=auth,
                          files={"metadata": (None, meta, "application/json"),
                                 "media": ("subtitles.srt", subtitles.read_bytes(), "application/octet-stream")}, timeout=120)
        missed += [] if r.ok else [f"captions ({r.status_code} {r.text[:120]})"]
    return missed


def upload(folder, slug, title, publish_at, settings):
    """Every file of the film from Drive, then to YouTube. Returns the video id and what did not go with it."""
    folder.mkdir(parents=True, exist_ok=True)
    files = {k: folder / f"{slug}{k}" for k in (".mp4", "_youtube_pack.json", "_thumbnail.jpg", "_subtitles.srt")}
    for f in files.values():
        if not f.exists():
            drive.download(f.name, f)
    if not files[".mp4"].exists():
        raise RuntimeError(f"{slug}.mp4 is not on Drive")
    pack = json.loads(files["_youtube_pack.json"].read_text()) if files["_youtube_pack.json"].exists() else {}
    vid = send(files[".mp4"], body(pack, title, publish_at, settings), settings.get("notify_subscribers", "Yes") == "Yes")
    return vid, extras(vid, files["_thumbnail.jpg"], files["_subtitles.srt"], LANGUAGE.get(settings.get("language"), "en"))


if __name__ == "__main__":
    import sys
    run = json.loads(sys.argv[1])
    print(upload(Path(os.getenv("UPLOAD_DIR", "upload")), run["slug"], run["title"], run["publish_at"], run["settings"]))
