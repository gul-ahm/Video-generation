"""Push one stage of a video to Kaggle as its own private script kernel, with the three datasets attached.

The kernel is cloud/kernel.py with the stage and the job written into it. Only the gpu stage asks for the T4s; each
stage has a time limit, so a stuck run closes by itself.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# one machine rendering all chapters took over 4 hours; one chapter takes about 40 minutes
LIMIT = {"research": 3 * 3600, "gpu": 3 * 3600, "part": 3 * 3600, "join": 2 * 3600, "render": 9 * 3600}


def user():
    return json.loads((Path.home() / ".kaggle" / "kaggle.json").read_text())["username"]


def kaggle():
    exe = Path(sys.executable).with_name("kaggle")
    return str(exe) if exe.exists() else "kaggle"


def push(stage, job):
    """job: {"id", "title", "minutes", "style", "slug", "subject"}."""
    sys.path.insert(0, str(HERE))
    import drive
    if drive.find("STOP"):  # the brake: a file named STOP in the Drive folder and no kernel starts another
        raise SystemExit("STOP is in the Drive folder, nothing is pushed")
    me, slug = user(), f"studio-render-{job['part']}" if stage == "part" else f"studio-{stage}"
    run = {**job, "stage": stage}
    with tempfile.TemporaryDirectory() as d:
        code = (HERE / "kernel.py").read_text().replace("__RUN__", json.dumps(run))
        (Path(d) / "kernel.py").write_text(code)
        gpu = stage == "gpu"
        (Path(d) / "kernel-metadata.json").write_text(json.dumps({
            "id": f"{me}/{slug}", "title": slug, "code_file": "kernel.py", "language": "python", "kernel_type": "script",
            "is_private": True, "enable_gpu": gpu, "enable_internet": True, "competition_sources": [], "kernel_sources": [],
            "dataset_sources": [f"{me}/history-studio-{x}" for x in ("code", "assets", "secrets")],
            **({"machine_shape": "NvidiaTeslaT4"} if gpu else {})}))
        cmd = [kaggle(), "kernels", "push", "-p", d, "-t", str(LIMIT[stage])] + (["--accelerator", "NvidiaTeslaT4"] if gpu else [])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    print(f"  pushed {slug} for \"{job['title']}\": {(r.stdout or r.stderr).strip()[-160:]}", flush=True)
    if r.returncode:
        raise SystemExit(f"kaggle push of {slug} failed")


def job(title, minutes=22, style="documentary", subject="", day=None):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    return {"id": slug[:40], "title": title, "minutes": minutes, "style": style, "slug": slug, "subject": subject,
            "day": day}
