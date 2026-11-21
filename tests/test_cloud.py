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

    def test_a_download_that_stalls_goes_on_from_the_byte_it_reached(self):
        import drive
        import requests
        body, asked = b"0123456789", []

        class Stream:
            def __init__(self, start):
                self.start, self.status_code = start, 206 if start else 200
                self.headers = {"Content-Length": str(len(body) - start)}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, n):
                yield body[self.start:self.start + 4]
                if not self.start:  # the first stream stops after four bytes, as Drive did at 226 MB
                    raise requests.ConnectionError("Read timed out.")
                yield body[self.start + 4:]

        def get(url, headers=None, **kw):
            start = int(headers.get("Range", "bytes=0-")[6:-1])
            asked.append(start)
            return Stream(start)

        with tempfile.TemporaryDirectory() as d, mock.patch.object(drive, "token", return_value="t"), \
                mock.patch.object(drive, "find", return_value="f1"), mock.patch.object(drive.requests, "get", side_effect=get), \
                mock.patch("time.sleep"):
            self.assertTrue(drive.download("job.tgz", Path(d) / "job.tgz"))
            self.assertEqual((Path(d) / "job.tgz").read_bytes(), body)
        self.assertEqual(asked, [0, 4])

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


def qa_md(fails=()):
    """A qa.md with all 15 checks, the named ones failing."""
    import qa
    rows = [f"| {name} | 1.00 {'FIX' if name in fails else 'ok'} | {lo} to {hi} | {ref} |"
            for name, (lo, hi, ref) in qa.TARGETS.items()]
    return "\n".join([f"{len(rows) - len(fails)} of {len(rows)} checks pass", "",
                      "| check | this video | target | reference |", "|---|---|---|---|", *rows])


class Sync(unittest.TestCase):
    def test_an_unchanged_dataset_is_not_uploaded_again(self):
        import cloud
        with tempfile.TemporaryDirectory() as d:
            sent = []
            with mock.patch.object(cloud, "ROOT", Path(d)), mock.patch.object(cloud, "pack"), \
                    mock.patch.object(cloud, "dataset", side_effect=lambda name, tgz: sent.append(name)), \
                    mock.patch.object(cloud, "stamp", side_effect=lambda *a: "same"), \
                    mock.patch.object(Path, "home", return_value=Path(d)):
                (Path(d) / "secrets").mkdir()
                (Path(d) / ".env.example").write_text("LLM_API_KEY=k\nHF_TOKEN=t\n")
                cloud.sync()
                cloud.sync()
            self.assertEqual(sent, ["code", "assets", "secrets"])

    def test_an_empty_key_stops_the_sync_before_anything_is_uploaded(self):
        import cloud
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".env.example").write_text("# key\nLLM_API_KEY=\nHF_TOKEN=t\n")
            with mock.patch.object(cloud, "ROOT", Path(d)), mock.patch.object(cloud, "dataset") as up, \
                    self.assertRaisesRegex(SystemExit, "LLM_API_KEY empty"):
                cloud.sync()
            up.assert_not_called()


class Gate(unittest.TestCase):
    def check(self, text):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "qa.md").write_text(text)
            return kernel().quality(Path(d))

    def test_qa_has_fifteen_checks_and_the_kernel_expects_fifteen(self):
        import qa
        self.assertEqual(len(qa.TARGETS), 15)
        self.assertEqual(kernel().CHECKS, 15)

    def test_only_fifteen_of_fifteen_is_offered(self):
        summary, fix, blocking = self.check(qa_md())
        self.assertEqual((summary, fix, blocking), ("15 of 15 checks pass", [], []))

    def test_one_failed_check_holds_the_film(self):
        _, _, blocking = self.check(qa_md(["frames with movement (%)"]))
        self.assertEqual(blocking, ["frames with movement (%): 1.00 FIX (target 60 to 100)"])

    def test_a_qa_with_missing_checks_holds_the_film(self):
        text = "\n".join(l for l in qa_md().splitlines() if "pictures that fit" not in l)
        self.assertEqual(self.check(text)[2], ["only 14 of 15 checks ran"])

    def test_a_held_film_is_reported_not_offered(self):
        with tempfile.TemporaryDirectory() as d:
            k = kernel(home=d)
            f = Path(d) / "output" / "battle"
            f.mkdir(parents=True)
            (f / "final.mp4").write_bytes(b"v")
            (f / "qa.md").write_text(qa_md(["longest same picture (s)"]))
            fake = types.SimpleNamespace(upload=mock.Mock(return_value=("id", "LINK")))
            with mock.patch.dict(sys.modules, {"drive": fake}), mock.patch.object(k, "hook") as hook:
                with self.assertRaises(k.Quality) as caught:
                    k.deliver()
            self.assertIn("LINK", str(caught.exception))
            self.assertIn("longest same picture", str(caught.exception))
            self.assertEqual(fake.upload.call_args_list[0].args[1], "battle_held.mp4")
            hook.assert_not_called()


class Checks(unittest.TestCase):
    """The five checks added after the test film: what the eye sees in a documentary."""

    def test_the_same_picture_is_one_picture_through_its_pose_frames(self):
        import qa
        p = Path("img/00/s1a2b3.jpg")
        shots = [[p, 3.0], [p.with_name("s1a2b3.p1.jpg"), 3.0], [p.with_name("s1a2b3.p2.jpg"), 3.0],
                 [Path("img/00/s9z8y7.jpg"), 4.0], [p, 2.0]]
        self.assertEqual(qa.longest_same(shots), 9.0)

    def test_an_article_page_is_not_counted_as_a_designed_card(self):
        import qa
        kinds = {"img/00/card_a.json": {"kind": "source", "title": "en.wikipedia.org"},
                 "img/00/card_b.json": {"kind": "stat"}}
        self.assertEqual([qa.kind([Path(k), 1.0], kinds) for k in kinds], ["source", "stat"])
        self.assertEqual(qa.kind([Path("img/00/s1.jpg"), 1.0], kinds), "picture")

    def test_only_reference_works_and_hadith_sites_count_as_trusted(self):
        sys.path.insert(0, str(ROOT / "documentary"))
        import research
        got = {u: research.trusted(u) for u in ("en.wikipedia.org", "https://sunnah.com/bukhari", "history.ucla.edu",
                                                 "pakasiayouthforum.com", "the-epoch-institute.org", "notwikipedia.org")}
        self.assertEqual(list(got.values()), [True, True, True, False, False, False])

    def test_a_film_about_636_may_not_show_guns_or_top_hats(self):
        import visuals
        with mock.patch.dict(os.environ, {"DOC_SUBJECT": "Battle of al-Qadisiyyah, 636", "STYLE": "documentary"}):
            avoid = visuals.style()[2]
        self.assertIn("cannon", avoid)
        self.assertIn("top hat", avoid)
        with mock.patch.dict(os.environ, {"DOC_SUBJECT": "Fall of Constantinople, 1453", "STYLE": "documentary"}):
            self.assertNotIn("cannon", visuals.style()[2])  # the Ottomans had them

    def test_a_long_beat_gets_more_than_one_picture_and_an_article_beat_keeps_its_own(self):
        import make
        ch = {"shots": [("Wide shot of the camp", 40, "forty words", ["pose"]),
                        ("source: a.png | en.wikipedia.org | | Close shot of a letter", 10, "ten words", []),
                        ("Close shot of a sword", 12, "twelve", [])]}
        with mock.patch.object(make, "lit", lambda ch, plan: [x[0] for x in ch["shots"]]):
            got = make.pictures_of(ch, {})
        self.assertEqual([(beat, poses) for _, beat, poses in got], [(0, ["pose"]), (0, []), (0, []), (1, []), (2, [])])
        self.assertTrue(got[1][0].endswith(make.VIEWS[0]))
        self.assertEqual(got[3][0], "Close shot of a letter")
        self.assertEqual(make.source_parts("source: a.png | bbc.com | 2024-01-02")[:3], ("a.png", "bbc.com", "2024-01-02"))

    def test_a_picture_that_misses_its_line_is_drawn_again_and_counted(self):
        import make
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            pic = folder / "img" / "00" / "s1.jpg"
            pic.parent.mkdir(parents=True)
            pic.write_bytes(b"x")
            (pic.parent / "s1.fg.png").write_bytes(b"x")
            # a cannon is a fault; the redraw comes back with a sunny sky the prompt did not ask for, which is not one
            answers = iter([{"later": "a cannon", "writing": "", "body": "", "unrelated": ""},
                            {"later": "", "writing": "", "body": "", "unrelated": "", "light": "sunny, not stormy"}])
            redrawn = []

            def draw(todo, moves):
                redrawn.extend(todo)
                for _, _, dst in todo:
                    dst.write_bytes(b"y")

            script = {"title": "t", "chapters": [{"shots": [("Wide shot", 10, "the line", [])]}]}
            with mock.patch.object(make, "pictures_of", lambda ch, plan: [("Wide shot", 0, [])]), \
                    mock.patch.object(make.visuals, "pending", lambda p, f, poses=None: ([pic], [], [])), \
                    mock.patch.object(make.writer, "see", lambda q, path: next(answers)), \
                    mock.patch.object(make.kaggle_gpu, "pictures", draw), \
                    mock.patch.dict(os.environ, {"DOC_SUBJECT": "Battle of al-Qadisiyyah, 636"}):
                make.check_pictures(script, [{}], folder)
            got = json.loads((folder / "picture_check.json").read_text())
            self.assertEqual((got["checked"], got["fit"], got["misses"]), (1, 1, []))
            self.assertEqual(redrawn[0][1], 1101)  # another seed
            self.assertFalse((pic.parent / "s1.fg.png").exists())  # the old cut out went with the old picture

    def checked(self, gpu_answers, gemini_answers, cache=None, pictures=3):
        """check_pictures over a few pictures: the GPU model answers some (by writing the cache, as
        vision_local.py does), the Gemini key the rest. Returns what each looked at and the result."""
        import make
        d = Path(tempfile.mkdtemp())
        pics = []
        for k in range(pictures):
            pic = d / "img" / "00" / f"s{k}.jpg"
            pic.parent.mkdir(parents=True, exist_ok=True)
            pic.write_bytes(f"picture {k}".encode())
            pics.append(pic)
        if cache:
            (d / "picture_check_cache.json").write_text(json.dumps(cache(pics)))
        gpu, gemini = [], []

        def run(cmd, check):
            todo = json.loads(Path(cmd[2]).read_text())
            seen = json.loads(Path(cmd[3]).read_text()) if Path(cmd[3]).exists() else {}
            for item in todo:
                gpu.append(Path(item["path"]).name)
                if Path(item["path"]).name in gpu_answers:
                    seen[Path(item["path"]).name] = {"md5": item["md5"], "answer": gpu_answers[Path(item["path"]).name]}
            Path(cmd[3]).write_text(json.dumps(seen))
            return types.SimpleNamespace(returncode=0)

        def see(q, path):
            gemini.append(path.name)
            return gemini_answers[path.name]
        script = {"title": "t", "chapters": [{"shots": [(f"shot {k}", 10, f"line {k}", []) for k in range(pictures)]}]}
        with mock.patch.object(make, "pictures_of", lambda ch, plan: [(f"shot {k}", k, []) for k in range(pictures)]), \
                mock.patch.object(make.visuals, "pending", lambda p, f, poses=None: (pics, [], [])), \
                mock.patch.object(make.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
                mock.patch.object(make.subprocess, "run", run), mock.patch.object(make.writer, "see", see), \
                mock.patch.object(make.kaggle_gpu, "pictures", lambda todo, moves: 0), \
                mock.patch.dict(os.environ, {"PICTURE_REDRAW": "0"}):
            make.check_pictures(script, [{}], d)
        return gpu, gemini, json.loads((d / "picture_check.json").read_text())

    def test_the_gpu_model_looks_first_and_the_gemini_key_only_at_what_it_could_not(self):
        ok = '{"later": "", "writing": "", "body": "", "unrelated": ""}'
        gpu, gemini, got = self.checked({"s0.jpg": ok, "s1.jpg": '{"later": "folding chairs"}'},
                                        {"s2.jpg": {"later": ""}})
        self.assertEqual((gpu, gemini), (["s0.jpg", "s1.jpg", "s2.jpg"], ["s2.jpg"]))
        self.assertEqual((got["fit"], [m["problem"] for m in got["misses"]]), (2, ["later: folding chairs"]))

    def test_a_check_that_stopped_goes_on_from_the_picture_it_reached(self):
        import make
        ok = '{"later": ""}'
        seen = lambda pics: {p.name: {"md5": make.md5(p), "answer": ok} for p in pics[:2]}
        gpu, gemini, got = self.checked({"s2.jpg": ok}, {}, cache=seen)
        self.assertEqual((gpu, gemini, got["fit"]), (["s2.jpg"], [], 3))

    def test_a_picture_drawn_again_is_looked_at_again(self):
        import make
        stale = lambda pics: {p.name: {"md5": "an older picture", "answer": '{"later": ""}'} for p in pics}
        gpu, _, _ = self.checked({f"s{k}.jpg": '{"later": ""}' for k in range(3)}, {}, cache=stale)
        self.assertEqual(gpu, ["s0.jpg", "s1.jpg", "s2.jpg"])

    def test_an_article_shows_briefly_then_the_beat_has_its_own_pictures(self):
        import make
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            page = folder / "page.png"
            page.write_bytes(b"x")
            ch = {"title": "c", "visuals": ["v"],
                  "shots": [(f"source: {page} | en.wikipedia.org | | Wide shot of the river", 36, "beat one", []),
                            ("Close shot of a sword", 12, "beat two", [])]}
            words = [(k * 0.33, k * 0.33 + 0.3, "w") for k in range(48)]
            fake = lambda prompts, folder, poses=None: [folder / f"pic{k}.jpg" for k in range(len(prompts))]
            with mock.patch.object(make.visuals, "scenes", fake), mock.patch.object(make.motion, "prepare"), \
                    mock.patch.object(make.motion, "air", return_value=None), \
                    mock.patch.object(make.sound, "plan", return_value=([], [])), \
                    mock.patch.object(make, "plan_fx", return_value=[]), \
                    mock.patch.dict(os.environ, {"CLIPS": "0", "POSES": "0"}):
                shots, _, _ = make.timeline(ch, 16.0, words, folder)
        self.assertEqual(shots[0][0].suffix, ".json")
        self.assertLessEqual(shots[0][1], make.SOURCE_MAX)
        first_beat = [x[0].name for x in shots[1:] if x[0].name.startswith("pic") and x[0].name < "pic2"]
        self.assertEqual(sorted(set(first_beat)), ["pic0.jpg", "pic1.jpg"])  # 36 words: two views of the scene
        import qa
        self.assertLessEqual(qa.longest_same(shots), 8.0)

    def test_the_report_measures_all_fifteen_on_a_real_video(self):
        import subprocess
        import qa
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            subprocess.run([qa.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=12:size=320x180:rate=24",
                            "-f", "lavfi", "-i", "sine=frequency=300:duration=12", "-shortest", "-c:v", "libx264",
                            "-c:a", "aac", str(folder / "final.mp4")], check=True)
            (folder / "picture_check.json").write_text(json.dumps({"checked": 10, "fit": 9, "misses": []}))
            page, a, b = Path("img/00/card_p.json"), Path("img/00/s1.jpg"), Path("img/00/s2.jpg")
            kinds = {str(page): {"kind": "source", "title": "pakasiayouthforum.com"}}
            shots = [[page, 3.0], [a, 3.0], [a.with_name("s1.p1.jpg"), 3.0], [b, 3.0]]
            with mock.patch.object(qa, "side_by_side", lambda folder: []):
                got = qa.report(folder, [(shots, ([], [], []))], 12.0, None, kinds)
            text = (folder / "qa.md").read_text()
            self.assertEqual(len(got), 15)
            self.assertEqual(text.count(" ok |") + text.count(" FIX |"), 15)
            self.assertEqual(got["longest same picture (s)"], 6.0)
            self.assertEqual(got["time on article screenshots (%)"], 25.0)
            self.assertEqual(got["different pictures per minute"], 10.0)
            self.assertEqual(got["pictures that fit their line (%)"], 90.0)
            self.assertEqual(got["pages from trusted sites (%)"], 0.0)
            self.assertEqual(got["full screen cards per minute"], 0.0)

    def vision(self, answer, listed=("gemini-9-flash",), env=None):
        """writer.see with a fake API: answer(model, is_probe) returns the text or raises."""
        import writer
        calls = []

        def create(model, messages, **kw):
            probe = "ok" in messages[0]["content"][0]["text"]
            calls.append((model, probe))
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(
                content=answer(model, probe)))])

        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)),
                                       models=types.SimpleNamespace(list=lambda: [types.SimpleNamespace(id=f"models/{m}")
                                                                                  for m in listed]))
        env = {"LLM_FAST": "a", "LLM_MODEL": "b", "LLM_FALLBACKS": "c", "LLM_VISION": "", "LLM_VISION_RPM": "6000",
               **(env or {})}
        d = tempfile.mkdtemp()
        from PIL import Image
        pic = Path(d) / "p.jpg"
        Image.new("RGB", (64, 36)).save(pic)
        with mock.patch.object(writer, "_client", client), mock.patch.object(writer, "_eyes", {}), \
                mock.patch.dict(os.environ, env), mock.patch("time.sleep") as slept:
            try:
                return writer.see("fits?", pic), calls, slept
            except SystemExit as err:
                return str(err), calls, slept

    def test_a_retired_or_silent_model_is_left_out_and_a_listed_one_found(self):
        from openai import NotFoundError
        gone = NotFoundError("404 no longer available", response=mock.Mock(status_code=404, request=None), body=None)

        def answer(model, probe):
            if model == "a":
                raise gone  # retired, as gemini-2.5-flash-lite
            if model == "b":
                raise TimeoutError()  # hangs, as gemini-flash-latest
            if model == "c":
                raise Exception("503 UNAVAILABLE high demand")
            return '{"ok": true}' if probe else '{"fits": true}'
        got, calls, _ = self.vision(answer, listed=("gemini-9-flash", "gemini-9-flash-tts", "imagen-4"))
        self.assertEqual(got, {"fits": True})
        self.assertEqual([m for m, probe in calls if not probe], ["gemini-9-flash"])
        self.assertNotIn("gemini-9-flash-tts", [m for m, _ in calls])

    def test_a_busy_round_is_waited_out_then_every_model_is_named(self):
        busy = {"n": 0}

        def answer(model, probe):
            if probe:
                return '{"ok": true}'
            busy["n"] += 1
            if busy["n"] <= 3:  # all three busy in the first round, as on the test film's night
                raise Exception("503 UNAVAILABLE: high demand")
            return '{"fits": false}'
        got, _, slept = self.vision(answer, listed=())
        self.assertEqual(got, {"fits": False})
        self.assertIn(30, [c.args[0] for c in slept.call_args_list])
        got, _, _ = self.vision(lambda m, probe: '{"ok": true}' if probe else (_ for _ in ()).throw(Exception("503 busy")),
                                listed=())
        self.assertTrue(got.startswith("service busy:"), got)
        self.assertIn("a: 503 busy", got)

    def test_free_calls_used_up_for_the_day_say_when_they_come_back(self):
        spent = Exception("Error code: 429 - [{'error': {'code': 429, 'message': 'You exceeded your current quota', "
                          "'details': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}, "
                          "{'retryDelay': '73342s'}]}}]")

        def answer(model, probe):
            if probe and model == "a":
                return '{"ok": true}'
            raise spent
        got, _, _ = self.vision(answer, listed=())
        self.assertTrue(got.startswith("service busy: wait=73342 "), got)

    def test_no_model_free_is_asked_again_once_a_minute_not_at_every_picture(self):
        import writer
        probes = []

        def answer(model, probe):
            probes.append(model) if probe else None
            raise Exception("503 UNAVAILABLE")
        with mock.patch.object(writer, "eyes", wraps=writer.eyes):
            self.vision(answer, listed=())
        self.assertLessEqual(len(probes), 3 * 2, "one look at the start and at most one more")

    def test_a_screenshot_covered_by_a_dialog_is_refused(self):
        sys.path.insert(0, str(ROOT / "documentary"))
        import assets
        text = "the battle of qadisiyyah began when the persian army crossed the river near the town " * 20
        page = mock.Mock()
        page.inner_text.return_value = text
        page.evaluate.return_value = {"text": text, "covered": True}
        self.assertFalse(assets.genuine(page, {"text": text})[0])
        page.evaluate.return_value = {"text": "accept cookies personalize your experience sign in " * 5, "covered": False}
        self.assertFalse(assets.genuine(page, {"text": text})[0])
        page.evaluate.return_value = {"text": text, "covered": False}
        self.assertTrue(assets.genuine(page, {"text": text})[0])


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
