"""Kaggle helpers for cloud.py. Kernels are pushed by the Cloudflare worker only (cloud/cloudflare/worker.js); the
slug and the job id here are made the same way it makes them, so both find the same files on Drive.
"""
import json
import re
import sys
from pathlib import Path


def user():
    return json.loads((Path.home() / ".kaggle" / "kaggle.json").read_text())["username"]


def kaggle():
    exe = Path(sys.executable).with_name("kaggle")
    return str(exe) if exe.exists() else "kaggle"


def job(title, minutes=22, style="documentary", subject="", day=None):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    return {"id": slug[:40], "title": title, "minutes": minutes, "style": style, "slug": slug, "subject": subject,
            "day": day}
