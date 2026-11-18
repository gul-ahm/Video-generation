"""python -m unittest discover tests   (no Kaggle, Drive or Telegram needed: they are faked)

The pieces that broke in the cloud runs: the empty Drive upload, the quality gate, a kernel report that has to say
what failed, the card browser that was left half started, the strict build.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "cloud")]


def kernel(run=None, home=None):
    """cloud/kernel.py as a module, with RUN filled in and without running main()."""
    code = (ROOT / "cloud" / "kernel.py").read_text().replace("__RUN__", json.dumps(run or {
        "id": "battle", "title": "Battle", "slug": "battle", "stage": "part", "part": 2, "nonce": "n1", "day": 3,
        "minutes": 22}))
    code = code.rsplit("\nmain()", 1)[0]
    m = types.ModuleType("kernel")
    exec(compile(code, "kernel.py", "exec"), m.__dict__)
    if home:
        m.HOME, m.W = Path(home), Path(home)
    return m


class Answer:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.ok, self._body, self.headers = status, status < 400, body or {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


class Drive(unittest.TestCase):
    def test_an_empty_file_finishes_its_upload(self):
        import drive
        calls = []

        def put(url, data=None, **kw):
            calls.append(len(data))
            return Answer(200, {"id": "f1"})

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(drive, "token", return_value="t"), mock.patch.object(drive, "find", return_value=None), \
                mock.patch.object(drive, "folder", return_value="dir"), \
                mock.patch.object(drive.requests, "post", return_value=Answer(200, headers={"Location": "u"})), \
                mock.patch.object(drive.requests, "put", side_effect=put):
            f = Path(d) / "STOP"
            f.write_text("")
            self.assertEqual(drive.upload(f)[0], "f1")
            self.assertEqual(calls, [0])

    def test_a_large_file_goes_up_in_pieces(self):
        import drive
        sizes = []

        def put(url, data=None, headers=None, **kw):
            sizes.append(len(data))
            return Answer(308) if len(sizes) == 1 else Answer(200, {"id": "f2"})

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(drive, "token", return_value="t"), mock.patch.object(drive, "find", return_value=None), \
                mock.patch.object(drive, "folder", return_value="dir"), \
                mock.patch.object(drive.requests, "post", return_value=Answer(200, headers={"Location": "u"})), \
                mock.patch.object(drive.requests, "put", side_effect=put):
            f = Path(d) / "big.bin"
            f.write_bytes(b"x" * (32 * 2 ** 20 + 5))
            self.assertEqual(drive.upload(f)[0], "f2")
            self.assertEqual(sizes, [32 * 2 ** 20, 5])


QA = """# Quality check: battle

{n} of 10 checks pass

| check | this video | target | reference |
|---|---|---|---|
| cuts per minute | 11.61 ok | 10 to 16 | 12.7 |
| frames with movement (%) | 56.87 FIX | 60 to 100 | 94 in the cartoon |
| time with caption or card (%) | {cap} | 45 to 100 | about 60 |
| full screen cards per minute | 3.55 ok | 1.0 to 4.0 | about 1.7 |
| longest still moment (s) | 0.17 ok | 0.0 to 2.0 | huashu |
"""


class Gate(unittest.TestCase):
    def check(self, n, cap):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "qa.md").write_text(QA.format(n=n, cap=cap))
            return kernel().quality(Path(d))

    def test_a_good_film_passes_with_its_small_misses_listed(self):
        summary, fix, blocking = self.check(9, "63.52 ok")
        self.assertEqual(summary, "9 of 10 checks pass")
        self.assertEqual(fix, ["frames with movement (%): 56.87 FIX (target 60 to 100)"])
        self.assertEqual(blocking, [])

    def test_a_film_without_its_cards_is_held(self):
        _, _, blocking = self.check(7, "21.00 FIX")
        self.assertEqual(blocking, ["time with caption or card (%): 21.00 FIX (target 45 to 100)"])

    def test_a_held_film_is_reported_not_offered(self):
        with tempfile.TemporaryDirectory() as d:
            k = kernel(home=d)
            f = Path(d) / "output" / "battle"
            f.mkdir(parents=True)
            (f / "final.mp4").write_bytes(b"v")
            (f / "qa.md").write_text(QA.format(n=7, cap="21.00 FIX"))
            fake = types.SimpleNamespace(upload=mock.Mock(return_value=("id", "LINK")))
            with mock.patch.dict(sys.modules, {"drive": fake}), mock.patch.object(k, "hook") as hook:
                with self.assertRaises(k.Quality) as caught:
                    k.deliver()
            self.assertIn("LINK", str(caught.exception))
            self.assertEqual(fake.upload.call_args_list[0].args[1], "battle_held.mp4")
            hook.assert_not_called()


class Reports(unittest.TestCase):
    def test_a_report_carries_the_nonce_and_is_tried_again(self):
        k = kernel()
        answers = [OSError("down"), Answer(502), Answer(200)]

        def post(url, json=None, **kw):
            a = answers.pop(0)
            if isinstance(a, Exception):
                import requests
                raise requests.ConnectionError(str(a))
            post.body = json
            return a

        fake = types.SimpleNamespace(env=lambda name: {"WORKER_URL": "https://w", "HOOK_SECRET": "s"}, tell=mock.Mock())
        with mock.patch.dict(sys.modules, {"drive": fake}), mock.patch("requests.post", side_effect=post), \
                mock.patch("time.sleep"):
            k.hook("done")
        self.assertEqual((post.body["nonce"], post.body["part"], post.body["event"]), ("n1", 2, "done"))
        fake.tell.assert_not_called()

    def test_a_failed_build_says_what_broke(self):
        k = kernel()
        script = "import sys; print('  visuals 1/6: 4.1 min'); print('SystemExit: card x could not be drawn'); sys.exit(1)"
        with tempfile.TemporaryDirectory() as d, mock.patch.object(k, "HOME", Path(d)), mock.patch.object(k, "hook"):
            real = k.subprocess.Popen
            with mock.patch.object(k.subprocess, "Popen", lambda cmd, **kw: real([sys.executable, "-c", script], **kw)):
                with self.assertRaises(RuntimeError) as caught:
                    k.work("part")
        self.assertIn("card x could not be drawn", str(caught.exception))

    def test_progress_lines_go_to_the_worker(self):
        k = kernel()
        script = "print('  render 2/6 done'); print('  some detail'); print('  voice 1/6 took 9.6 min')"
        sent = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(k, "HOME", Path(d)), \
                mock.patch.object(k, "hook", side_effect=lambda e, **x: sent.append(x.get("line"))):
            real = k.subprocess.Popen
            with mock.patch.object(k.subprocess, "Popen", lambda cmd, **kw: real([sys.executable, "-c", script], **kw)):
                k.work("part")
        self.assertEqual(sent, ["render 2/6 done", "voice 1/6 took 9.6 min"])


class Cards(unittest.TestCase):
    def test_a_browser_that_does_not_start_is_closed_again(self):
        import hf
        stopped = []
        pw = types.SimpleNamespace(chromium=types.SimpleNamespace(launch=mock.Mock(side_effect=RuntimeError("no chrome"))),
                                   stop=lambda: stopped.append(1))
        fake = types.ModuleType("playwright.sync_api")
        fake.sync_playwright = lambda: types.SimpleNamespace(start=lambda: pw)
        with mock.patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.sync_api": fake}), \
                mock.patch.object(hf, "_browser", None):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    hf.page()
        self.assertEqual(stopped, [1, 1])

    def test_strict_build_stops_on_a_card_it_cannot_draw(self):
        import make
        with mock.patch.object(make, "STRICT", True), \
                mock.patch.object(make.hf, "render", side_effect=RuntimeError("browser gone")) as render:
            with self.assertRaises(SystemExit) as caught:
                make.card_frames("vox.t1.stat", {}, Path("x"))
        self.assertIn("browser gone", str(caught.exception))
        self.assertEqual(render.call_count, 2)

    def test_on_the_mac_a_card_falls_back_to_a_caption(self):
        import make
        with mock.patch.object(make, "STRICT", False), mock.patch.object(make.hf, "render", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                make.card_frames("vox.t1.stat", {}, Path("x"))

    def test_strict_effects_stop_when_not_set_up(self):
        import fx
        with mock.patch.dict(os.environ, {"STRICT": "1", "FX": "1"}), mock.patch.object(fx, "node", return_value=None):
            with self.assertRaises(SystemExit):
                fx.ready()


if __name__ == "__main__":
    unittest.main()
