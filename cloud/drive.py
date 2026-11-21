"""Google Drive and Telegram for the cloud runs, with the tokens in secrets/ (no extra packages: requests only).

Drive keeps what one Kaggle stage hands to the next (job_<id>.tgz) and the finished videos; Telegram tells you.
"""
import json
import time
from pathlib import Path

import requests

SECRETS = Path(__file__).resolve().parent.parent / "secrets"
API = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"


def env(name):
    """KEY=value lines from secrets/<name>.env."""
    f = SECRETS / f"{name}.env"
    return dict(l.split("=", 1) for l in f.read_text().split() if "=" in l) if f.exists() else {}


def token(use):
    g = json.loads((SECRETS / "google.json").read_text())
    r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "client_id": g["GOOGLE_CLIENT_ID"], "client_secret": g["GOOGLE_CLIENT_SECRET"], "grant_type": "refresh_token",
        "refresh_token": g.get(f"GOOGLE_REFRESH_TOKEN_{use.upper()}") or g["GOOGLE_REFRESH_TOKEN"]})
    r.raise_for_status()
    return r.json()["access_token"]


def folder():
    return json.loads((SECRETS / "google.json").read_text())["DRIVE_FOLDER_ID"]


def find(name):
    """The file id of <name> in the Drive folder, or None."""
    q = f"name = '{name}' and '{folder()}' in parents and trashed = false"
    r = requests.get(API, params={"q": q, "fields": "files(id)", "supportsAllDrives": "true"},
                     headers={"Authorization": f"Bearer {token('drive')}"}, timeout=60)
    r.raise_for_status()
    files = r.json()["files"]
    return files[0]["id"] if files else None


def upload(path, name=None):
    """Upload (or replace) a file in the Drive folder in 32 MB pieces. Returns (file id, link to watch it)."""
    path, name = Path(path), name or Path(path).name
    auth = {"Authorization": f"Bearer {token('drive')}"}
    old = find(name)
    meta = {"name": name} if old else {"name": name, "parents": [folder()]}
    url = f"{UPLOAD}/{old}" if old else UPLOAD
    start = (requests.patch if old else requests.post)(url, params={"uploadType": "resumable", "supportsAllDrives": "true"},
                                                       headers={**auth, "X-Upload-Content-Length": str(path.stat().st_size)},
                                                       json=meta, timeout=60)
    start.raise_for_status()
    where, size = start.headers["Location"], path.stat().st_size
    # an empty file (the markers) is one empty piece; the loop below sends none, and the upload never finished
    done = requests.put(where, data=b"", timeout=60, headers={"Content-Length": "0"}) if not size else None
    with path.open("rb") as f:
        for at in range(0, size, 32 * 2 ** 20):
            piece = f.read(32 * 2 ** 20)
            end = at + len(piece) - 1
            done = requests.put(where, data=piece, timeout=600,
                                headers={"Content-Length": str(len(piece)), "Content-Range": f"bytes {at}-{end}/{size}"})
            if done.status_code not in (200, 201, 308):
                done.raise_for_status()
    fid = done.json()["id"]
    return fid, f"https://drive.google.com/file/d/{fid}/view"


def download(name, dst):
    """Download <name> from the Drive folder to dst. False if it is not there. Drive sometimes stops sending in the
    middle of a big file (a 400 MB job folder stalled at 226 MB); the stream then goes on from the byte it reached."""
    fid = find(name)
    if not fid:
        return False
    dst, size = Path(dst), None
    dst.unlink(missing_ok=True)
    for attempt in range(6):
        have = dst.stat().st_size if dst.exists() else 0
        if size is not None and have >= size:
            return True
        try:
            with requests.get(f"{API}/{fid}", params={"alt": "media", "supportsAllDrives": "true"}, stream=True,
                              timeout=(30, 120), headers={"Authorization": f"Bearer {token('drive')}",
                                                          **({"Range": f"bytes={have}-"} if have else {})}) as r:
                r.raise_for_status()
                if have and r.status_code != 206:  # the range was not honoured: start over
                    have = 0
                size = have + int(r.headers.get("Content-Length", 0)) if r.headers.get("Content-Length") else None
                with open(dst, "ab" if have else "wb") as f:
                    for chunk in r.iter_content(2 ** 20):
                        f.write(chunk)
            if size is None or dst.stat().st_size >= size:
                return True
        except requests.RequestException as err:
            print(f"  drive: {name} stopped at {dst.stat().st_size if dst.exists() else 0} bytes ({str(err)[:80]}), "
                  "going on", flush=True)
        time.sleep(5 * (attempt + 1))
    raise SystemExit(f"Drive kept stopping in the middle of {name}")


def delete(name):
    """Remove <name> from the Drive folder (the job folder once the video is made; Drive's free 15 GB fills fast)."""
    fid = find(name)
    if fid:
        requests.delete(f"{API}/{fid}", params={"supportsAllDrives": "true"},
                        headers={"Authorization": f"Bearer {token('drive')}"}, timeout=60)


def tell(text, buttons=None):
    """A Telegram message to you; buttons: [(label, callback data)] in one row."""
    t = env("telegram")
    if not t.get("TELEGRAM_BOT_TOKEN"):
        return
    body = {"chat_id": t["TELEGRAM_CHAT_ID"], "text": text[:4000], "disable_web_page_preview": True}
    if buttons:
        body["reply_markup"] = {"inline_keyboard": [[{"text": a, "callback_data": b} for a, b in buttons]]}
    try:
        requests.post(f"https://api.telegram.org/bot{t['TELEGRAM_BOT_TOKEN']}/sendMessage", json=body, timeout=30)
    except requests.RequestException:
        pass
