"""Edit decisions for every sentence of the narration, answered by GLiNER2.5-Decide through colibri's System One API.

For each sentence: what the screen should show (a scene or a proof card), whether it is a turning point (fast zoom,
sound hit) and how tense it is (music and pace). The model never writes text, it only picks from these lists and
says how sure it is, so the edit can skip a card when the model is unsure. Local, free, no quota.

Needs colibri with the model (see README, "Edit decisions"). Without it, decide() returns None and the rest of the
pipeline keeps working.
"""
import hashlib
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

URL = os.getenv("DECIDE_URL", "http://127.0.0.1:8000")
COLIBRI = Path(os.getenv("COLIBRI_DIR", "~/colibri")).expanduser()
MODEL = Path(os.getenv("DECIDE_MODEL", "~/Models/GLiNER2.5-Decide")).expanduser()

QUESTIONS = {
    "show": {"type": "choice", "instructions": "What should the screen show while this sentence is spoken?",
             "criteria": {"scene": "people or places in action",
                          "date": "a specific date, year or length of time",
                          "number": "a count, amount, distance or statistic",
                          "place": "a city, region, route or location on a map",
                          "quote": "words someone said or wrote, a record or document",
                          "comparison": "two things set against each other"}},
    "turn": {"type": "noul", "instructions": "Is this a turning point, reveal or shock in the story?"},
    "mood": {"type": "score", "instructions": "How tense is this moment?",
             "criteria": ["calm", "uneasy", "tense", "shocking"]},
}


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'])", text) if len(s.split()) > 2]


def alive():
    try:
        return requests.get(f"{URL}/v1/models", timeout=3).ok
    except requests.RequestException:
        return False


def serve():
    """Start colibri with the decision model if it is installed and not running yet. Returns the process or None."""
    if alive():
        return None
    coli = COLIBRI / "c" / "coli"
    if not coli.exists() or not MODEL.exists():
        return None
    port = URL.rsplit(":", 1)[-1]
    proc = subprocess.Popen([str(coli), "serve", "--model", str(MODEL), "--port", port],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(90):
        if alive():
            return proc
        time.sleep(1)
    proc.terminate()
    return None


def ask(sentence):
    r = requests.post(f"{URL}/v1/systemone", json={"state": sentence, "questions": QUESTIONS}, timeout=60)
    r.raise_for_status()
    a = r.json()["answers"]
    return {"show": a["show"]["choice"], "sure": round(a["show"]["confidence"], 2),
            "turn": round(a["turn"]["noul"], 2), "mood": round(a["mood"]["score"], 2)}


def decide(lines, cache=None):
    """One decision per sentence, kept in cache (a json file) so a rerun asks only about new sentences."""
    cache = Path(cache) if cache else None
    known = json.loads(cache.read_text()) if cache and cache.exists() else {}
    todo = [s for s in dict.fromkeys(lines) if hashlib.md5(s.encode()).hexdigest()[:10] not in known]
    if todo:
        proc = serve()
        if not alive():
            return None
        try:
            with ThreadPoolExecutor(4) as ex:
                for s, d in zip(todo, ex.map(ask, todo)):
                    known[hashlib.md5(s.encode()).hexdigest()[:10]] = d
        finally:
            if proc:
                proc.terminate()
        if cache:
            cache.write_text(json.dumps(known))
    return [known[hashlib.md5(s.encode()).hexdigest()[:10]] for s in lines]
