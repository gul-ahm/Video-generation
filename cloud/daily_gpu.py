# History Studio: the day's GPU session as a notebook that Kaggle starts itself, on its schedule or from Save Version.
# The same work pushed through the API can sit in Kaggle's queue for hours, while a notebook saved in the browser
# starts at once. This asks the worker which videos wait for the GPU and runs the kernel.py stage it hands back.
import tarfile
from pathlib import Path

import requests


def read(name, archive):
    """A file from the mounted datasets, unpacked by Kaggle or still in its archive."""
    f = next(Path("/kaggle/input").rglob(name), None)
    if f:
        return f.read_text()
    with tarfile.open(next(Path("/kaggle/input").rglob(archive))) as t:
        return t.extractfile(next(m for m in t.getmembers() if m.name.endswith(name))).read().decode()


w = dict(l.split("=", 1) for l in read("worker.env", "secrets.tgz").split() if "=" in l)
r = requests.post(w["WORKER_URL"] + "/claim", json={"version": read("VERSION", "code.tgz").strip()},
                  headers={"X-Studio-Secret": w["HOOK_SECRET"]}, timeout=60)
r.raise_for_status()
got = r.json()
print(got["message"], flush=True)
if got.get("code"):
    exec(compile(got["code"], "kernel.py", "exec"), {"__name__": "__main__"})
