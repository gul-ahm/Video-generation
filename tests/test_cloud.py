"""python -m unittest discover tests   (no Kaggle, Drive or Telegram needed: they are faked)

The pieces that broke in the cloud runs: the empty Drive upload, the quality gate, a kernel report that has to say
what failed, the card browser that was left half started, the strict build.
"""
import io
import collections
import json
import os
import sys
import tarfile
import tempfile
import time
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


class Deploy(unittest.TestCase):
    def commit(self, files):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name, text in files.items():
                data = text.encode()
                info = tarfile.TarInfo(f"Video-generation-abc123/{name}")
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    def deploy(self, root, files, tests_pass=True):
        import cloud
        tgz = self.commit(files)

        def get(url, timeout):
            return types.SimpleNamespace(json=lambda: {"sha": "abc1234567"}, content=tgz, raise_for_status=lambda: None)
        ran = types.SimpleNamespace(returncode=0 if tests_pass else 1, stderr="Ran 3 tests\n\nOK" if tests_pass else "FAILED")
        with mock.patch.object(cloud, "ROOT", root), mock.patch("requests.get", get), \
                mock.patch.object(cloud.subprocess, "run", return_value=ran), mock.patch.object(cloud, "sync") as synced, \
                mock.patch.object(cloud, "busy", return_value=[]), mock.patch("worker.deploy") as put_up:
            cloud.deploy("main")
        self.worker_deployed = put_up.called
        return synced

    def test_the_commit_is_written_and_the_keys_on_the_mac_stay(self):
        root = Path(tempfile.mkdtemp())
        (root / ".env.example").write_text("LLM_API_KEY=the-real-key\n")
        (root / "make.py").write_text("old")
        synced = self.deploy(root, {".env.example": "LLM_API_KEY=\n", "make.py": "new", "cloud/kernel.py": "k"})
        self.assertEqual((root / ".env.example").read_text(), "LLM_API_KEY=the-real-key\n")
        self.assertEqual(((root / "make.py").read_text(), (root / "cloud/kernel.py").read_text()), ("new", "k"))
        self.assertEqual((root / "DEPLOYED").read_text().strip(), "abc1234567")
        synced.assert_called_once()
        self.assertTrue(self.worker_deployed, "a changed kernel.py goes to the worker's store")
        with tarfile.open(next((root / "_ref").glob("before_*.tgz"))) as b:
            self.assertEqual(b.extractfile("make.py").read(), b"old")

    def test_no_sync_while_a_film_runs_on_the_code_dataset(self):
        import cloud
        root = Path(tempfile.mkdtemp())
        tgz = self.commit({"make.py": "new"})
        get = lambda url, timeout: types.SimpleNamespace(json=lambda: {"sha": "abc1234567"}, content=tgz,
                                                         raise_for_status=lambda: None)
        ran = types.SimpleNamespace(returncode=0, stderr="OK")
        with mock.patch.object(cloud, "ROOT", root), mock.patch("requests.get", get), \
                mock.patch.object(cloud.subprocess, "run", return_value=ran), mock.patch.object(cloud, "sync") as synced, \
                mock.patch.object(cloud, "busy", return_value=["Day 5"]):
            cloud.deploy("main")
        synced.assert_not_called()

    def test_failed_tests_put_the_old_files_back(self):
        root = Path(tempfile.mkdtemp())
        (root / "make.py").write_text("old")
        with self.assertRaisesRegex(SystemExit, "tests failed"):
            self.deploy(root, {"make.py": "broken", "new_file.py": "x"}, tests_pass=False)
        self.assertEqual((root / "make.py").read_text(), "old")
        self.assertFalse((root / "new_file.py").exists())
        self.assertFalse((root / "DEPLOYED").exists())


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


    def test_a_smoke_film_is_delivered_with_failed_checks_but_not_with_missing_ones(self):
        run = {"id": "smoke", "title": "Smoke", "slug": "smoke", "stage": "join", "nonce": "n", "day": 0, "smoke": True}
        for text, held in ((qa_md(["longest same picture (s)"]), False),
                           ("\n".join(l for l in qa_md().splitlines() if "pictures that fit" not in l), True)):
            with tempfile.TemporaryDirectory() as d:
                k = kernel(run, home=d)
                f = Path(d) / "output" / "smoke"
                f.mkdir(parents=True)
                (f / "final.mp4").write_bytes(b"v")
                (f / "qa.md").write_text(text)
                fake = types.SimpleNamespace(upload=mock.Mock(return_value=("id", "LINK")))
                with mock.patch.dict(sys.modules, {"drive": fake}), mock.patch.object(k, "hook") as hook:
                    if held:
                        with self.assertRaises(k.Quality):
                            k.deliver()
                    else:
                        k.deliver()
                        self.assertEqual(hook.call_args.args[0], "ready")


class Promote(unittest.TestCase):
    def promote(self, passed):
        import cloud
        kv = {"smoke_passed": passed}
        cf = types.SimpleNamespace(kv=lambda: "s", get=lambda space, k: kv.get(k), put=lambda space, k, v: kv.update({k: v}))
        up = mock.Mock()
        with mock.patch.dict(sys.modules, {"worker": types.SimpleNamespace(Cloudflare=lambda: cf)}), \
                mock.patch.object(cloud, "stamp", return_value="v1"), mock.patch.object(cloud, "pack"), \
                mock.patch.object(cloud, "dataset", up):
            cloud.promote()
        return kv, up

    def test_the_code_a_smoke_film_passed_goes_to_the_daily_videos(self):
        kv, up = self.promote("v1")
        self.assertEqual(kv["good_version"], "v1")
        self.assertEqual((up.call_args.args[0], up.call_args.kwargs["file"]), ("code-good", "code.tgz"))

    def test_other_code_than_the_smoke_film_passed_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "not the code the last smoke film passed"):
            self.promote("v0")


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
        ch = {"shots": [("Wide shot of the camp", 50, "fifty words", ["pose"]),
                        ("source: a.png | en.wikipedia.org | | Close shot of a letter", 10, "ten words", []),
                        ("Close shot of a sword", 12, "twelve", [])]}
        with mock.patch.object(make, "lit", lambda ch, plan: [x[0] for x in ch["shots"]]):
            got = make.pictures_of(ch, {})
        self.assertEqual([(beat, poses) for _, beat, poses in got], [(0, ["pose"]), (0, []), (0, []), (1, []), (2, [])])
        self.assertTrue(got[1][0].endswith(make.VIEWS[0]))
        self.assertEqual(got[3][0], "Close shot of a letter")
        ch["lengths"] = [10, 3, 16.0]  # the sword is spoken slowly: one picture every 7.5 s
        with mock.patch.object(make, "lit", lambda ch, plan: [x[0] for x in ch["shots"]]):
            self.assertEqual([beat for _, beat, _ in make.pictures_of(ch, {})], [0, 0, 0, 1, 2, 2, 2])
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
                for _, _, dst, _ in todo:
                    dst.write_bytes(b"y")

            script = {"title": "t", "chapters": [{"shots": [("Wide shot", 10, "the line", [])]}]}
            with mock.patch.object(make, "pictures_of", lambda ch, plan: [("Wide shot", 0, [])]), \
                    mock.patch.object(make.visuals, "pending", lambda p, f, poses=None: ([pic], [], [])), \
                    mock.patch.object(make.writer, "see", lambda parts: next(answers)), \
                    mock.patch.object(make.kaggle_gpu, "pictures", draw), \
                    mock.patch.dict(os.environ, {"DOC_SUBJECT": "Battle of al-Qadisiyyah, 636"}):
                make.check_pictures(script, [{}], folder)
            got = json.loads((folder / "picture_check.json").read_text())
            self.assertEqual((got["checked"], got["fit"], got["misses"]), (1, 1, []))
            self.assertEqual(redrawn[0][1], 1101)  # another seed
            self.assertIn("a cannon", redrawn[0][3])  # and what was wrong is kept out of the new one
            self.assertIn("firearms", redrawn[0][3])
            self.assertFalse((pic.parent / "s1.fg.png").exists())  # the old cut out went with the old picture

    def checked(self, gpu_answers, gemini_answers, cache=None, pictures=3, sees_letters=True, local="1", look=None):
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
                name = Path(item["path"]).name
                if name == "canary_writing.png":  # the test picture with big letters goes first
                    self.assertEqual(todo[0], item)
                    answer = '{"writing": true, "what": "ROME 1999"}' if sees_letters else '{"writing": false}'
                    seen[name] = {"md5": item["md5"], "answer": answer, "by": "Qwen-test"}
                    continue
                gpu.append(name)
                if name in gpu_answers:
                    seen[name] = {"md5": item["md5"], "answer": gpu_answers[name], "by": "Qwen-test"}
            Path(cmd[3]).write_text(json.dumps(seen))
            return types.SimpleNamespace(returncode=0)

        def see(parts):  # one call for a group of pictures, as the check sends them
            group = [x for x in parts if isinstance(x, Path)]
            gemini.extend(x.name for x in group)
            if len(group) == 1 and "Picture 1." not in parts[0]:
                return gemini_answers[group[0].name]
            return {"pictures": [{**(make.parse(gemini_answers[x.name]) or {}), "n": k} for k, x in enumerate(group, 1)]}
        script = {"title": "t", "chapters": [{"shots": [(f"shot {k}", 10, f"line {k}", []) for k in range(pictures)]}]}
        with mock.patch.object(make, "pictures_of", lambda ch, plan: [(f"shot {k}", k, []) for k in range(pictures)]), \
                mock.patch.object(make.visuals, "pending", lambda p, f, poses=None: (pics, [], [])), \
                mock.patch.object(make.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
                mock.patch.object(make.subprocess, "run", run), mock.patch.object(make.writer, "see", look or see), \
                mock.patch.object(make.kaggle_gpu, "pictures", lambda todo, moves: 0), \
                mock.patch.dict(os.environ, {"PICTURE_REDRAW": "0", "VISION_LOCAL": local}):
            make.check_pictures(script, [{}], d)
        return gpu, gemini, json.loads((d / "picture_check.json").read_text())

    def test_the_gpu_model_looks_first_and_the_gemini_key_only_at_what_it_could_not(self):
        ok = '{"later": "", "writing": "", "body": "", "unrelated": ""}'
        gpu, gemini, got = self.checked({"s0.jpg": ok, "s1.jpg": '{"later": "folding chairs"}'},
                                        {"s2.jpg": {"later": ""}})
        self.assertEqual((gpu, gemini), (["s0.jpg", "s1.jpg", "s2.jpg"], ["s2.jpg"]))
        self.assertEqual((got["fit"], [m["problem"] for m in got["misses"]]), (2, ["later: folding chairs"]))

    def test_a_gpu_model_that_misses_the_test_letters_is_not_trusted(self):
        ok = '{"later": false, "writing": false, "body": false, "unrelated": false, "what": ""}'
        gpu, gemini, got = self.checked({f"s{k}.jpg": ok for k in range(3)},
                                        {"s0.jpg": {"later": True, "what": "folding chairs"}, "s1.jpg": {"later": False},
                                         "s2.jpg": {"writing": False}}, sees_letters=False)
        self.assertEqual(sorted(gemini), ["s0.jpg", "s1.jpg", "s2.jpg"])
        self.assertEqual([m["problem"] for m in got["misses"]], ["later: folding chairs"])

    def test_an_unreadable_answer_goes_to_the_next_judge_instead_of_stopping_the_stage(self):
        broken = '{"later": false, "writing": false, "body": false, "unrelated": false,}'  # the trailing comma of that night
        gpu, gemini, got = self.checked({"s0.jpg": broken}, {"s0.jpg": {"later": False}, "s1.jpg": {"later": False},
                                                              "s2.jpg": {"later": False}})
        self.assertEqual((sorted(gemini), got["fit"]), (["s0.jpg", "s1.jpg", "s2.jpg"], 3))

    def test_pictures_are_looked_at_five_a_call_and_one_left_out_goes_again_alone(self):
        import make
        ok = '{"later": false, "writing": false, "body": false, "unrelated": false}'
        answers = {f"s{k}.jpg": ok for k in range(7)}
        calls = []

        def see(parts):
            group = [x.name for x in parts if isinstance(x, Path)]
            calls.append(group)
            if len(group) == 1:
                return make.parse(answers[group[0]])
            return {"pictures": [{**make.parse(answers[n]), "n": k} for k, n in enumerate(group, 1) if n != "s3.jpg"]}
        gpu, gemini, got = self.checked({}, answers, pictures=7, local="0", look=see)
        self.assertEqual(sorted(calls, key=len, reverse=True),
                         [["s0.jpg", "s1.jpg", "s2.jpg", "s3.jpg", "s4.jpg"], ["s5.jpg", "s6.jpg"], ["s3.jpg"]])
        self.assertEqual(got["fit"], 7)

    def test_each_job_keeps_to_its_own_models_and_waits_for_them_when_their_day_is_spent(self):
        import writer
        sent = []
        spent = Exception("429 You exceeded your current quota GenerateRequestsPerDayPerProjectPerModel retryDelay': '5000s'")

        def create(model, messages, temperature, **extra):
            sent.append((model, extra.get("reasoning_effort")))
            if model == "r1":
                raise spent
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='{"a": 1}'))])
        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        env = {"LLM_API_KEY": "k", "LLM_READ": "r1,r2", "LLM_WRITE": "w1", "LLM_SEE": "s1"}
        with mock.patch.object(writer, "_client", client), mock.patch.dict(os.environ, env), mock.patch("time.sleep"):
            self.assertEqual(writer.llm("facts", as_json=True), {"a": 1})
            self.assertEqual(sent[-1], ("r2", "low"), "the next read model, with no thinking")
            writer.llm("a chapter")
            self.assertEqual(sent[-1], ("w1", None), "a chapter is written with thinking")
            with mock.patch.dict(os.environ, {"LLM_READ": "r1"}), self.assertRaises(SystemExit) as err:
                writer.llm("facts", as_json=True)
        self.assertTrue(str(err.exception).startswith("service busy: wait=5000 the read models (r1)"), err.exception)
        self.assertNotIn("s1", [m for m, _ in sent], "never another job's model")

        def busy(model, messages, temperature, **extra):
            raise Exception("Error code: 503 This model is currently experiencing high demand")
        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=busy)))
        with mock.patch.object(writer, "_client", client), mock.patch.dict(os.environ, {**env, "LLM_READ": "r9"}), \
                mock.patch("time.sleep"), self.assertRaises(SystemExit) as err:
            writer.llm("facts", as_json=True)
        self.assertTrue(str(err.exception).startswith("service busy: the read models (r9) kept failing"),
                        "an overloaded model is a wait, not a failed stage")

    def test_calls_keep_to_the_rate_of_their_model_and_are_counted_for_the_worker(self):
        import writer
        with mock.patch.object(writer, "_pace", {}), mock.patch("time.time", return_value=1000.0), \
                mock.patch("time.sleep") as slept, mock.patch.dict(os.environ, {"LLM_RPM_LITE": "12", "LLM_RPM": "4"}):
            for _ in range(3):
                writer.wait_turn("gemini-3.5-flash-lite")
            writer.wait_turn("gemini-flash-latest")
            writer.wait_turn("gemini-flash-latest")
        self.assertEqual([round(c.args[0]) for c in slept.call_args_list], [0, 5, 10, 0, 15])
        posted = []
        with mock.patch.object(writer, "_spent", collections.Counter({"m": 7})), mock.patch.object(writer, "_told", collections.Counter({"m": 2})), \
                mock.patch.dict(os.environ, {"WORKER_URL": "https://w", "HOOK_SECRET": "s"}), \
                mock.patch("requests.post", side_effect=lambda url, json, **kw: posted.append((url, json)) or Answer(200)):
            writer.tell_usage()
            writer.tell_usage()
        self.assertEqual(posted, [("https://w/usage", {"m": 5})], "only what the worker has not heard yet")

    def test_the_gpu_model_stays_off_unless_asked_for(self):
        ok = '{"later": false, "writing": false, "body": false, "unrelated": false}'
        gpu, gemini, got = self.checked({}, {f"s{k}.jpg": ok for k in range(3)}, local="0")
        self.assertEqual(gpu, [])
        self.assertEqual(sorted(gemini), ["s0.jpg", "s1.jpg", "s2.jpg"])

    def test_a_look_asks_for_no_thinking_and_its_cost_is_counted(self):
        import writer
        self.assertEqual(writer.effort("gemini-2.5-flash-lite"), {"reasoning_effort": "none"})
        self.assertEqual(writer.effort("gemini-3.5-flash-lite"), {"reasoning_effort": "low"})
        self.assertEqual(writer.effort("gemini-flash-latest"), {"reasoning_effort": "low"})
        sent = []

        def create(model, messages, temperature, **extra):
            sent.append(extra)
            if model == "slow":
                time.sleep(0.3)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="{}"))],
                                         usage=types.SimpleNamespace(prompt_tokens=300, completion_tokens=20, total_tokens=420,
                                                                     completion_tokens_details=types.SimpleNamespace(reasoning_tokens=100)))
        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
        with mock.patch.object(writer, "_client", client), \
                mock.patch.object(writer, "_usage", {"calls": 0, "in": 0, "out": 0, "think": 0, "late": 0}):
            writer.ask("gemini-3.5-flash-lite", "x", 0, writer.effort("gemini-3.5-flash-lite"), 5)
            with self.assertRaises(TimeoutError):
                writer.ask("slow", "x", 0, {}, 0.05)
            self.assertEqual(sent[0]["reasoning_effort"], "low")
            self.assertEqual(writer.usage(), "gemini: 2 calls, 0k tokens in, 0k out (0k of it thinking), 1 given up at the time limit")
            self.assertEqual(writer._usage, {"calls": 2, "in": 300, "out": 120, "think": 100, "late": 1})

    def test_a_model_out_of_its_day_is_not_asked_again_when_the_list_is_refreshed(self):
        def answer(model, probe):
            if model == "a":
                raise Exception("429 RESOURCE_EXHAUSTED GenerateRequestsPerDay retryDelay': '3600s'")
            return '{"fits": true}'
        _, calls, _ = self.vision(answer, listed=())
        self.assertEqual([m for m, probe in calls if probe and m == "a"], ["a"], "probed once, then left alone")

    def test_a_check_that_stopped_goes_on_from_the_picture_it_reached(self):
        import make
        ok = '{"later": ""}'
        seen = lambda pics: {p.name: {"md5": make.md5(p), "answer": ok} for p in pics[:2]}
        gpu, gemini, got = self.checked({"s2.jpg": ok}, {}, cache=seen)
        self.assertEqual((gpu, gemini, got["fit"]), (["s2.jpg"], [], 3))

    def test_a_picture_drawn_again_is_looked_at_again(self):
        stale = lambda pics: {p.name: {"md5": "an older picture", "answer": '{"later": ""}'} for p in pics}
        gpu, _, _ = self.checked({f"s{k}.jpg": '{"later": ""}' for k in range(3)}, {}, cache=stale)
        self.assertEqual(gpu, ["s0.jpg", "s1.jpg", "s2.jpg"])

    def test_the_plan_gate_draws_no_effect_even_on_a_machine_without_them(self):
        import fx
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"PLAN_ONLY": "1", "STRICT": "1"}), \
                mock.patch.object(fx, "ready", side_effect=SystemExit("effects are not set up")):
            self.assertEqual(fx.frames("prism", {"text": "Day one"}, Path(d) / "t"), (Path(d) / "t").resolve())

    def test_a_film_that_would_be_held_is_stopped_before_any_chapter_is_rendered(self):
        import make
        d = Path(tempfile.mkdtemp())
        (d / "picture_check.json").write_text(json.dumps({"checked": 10, "fit": 10, "misses": []}))
        pics = [d / f"s{k}.jpg" for k in range(15)]
        drawn = []

        def timeline(ch, dur, words, folder, plan, first):  # fifteen pictures of 4 s, no card: 0 cards a minute
            self.assertEqual(os.environ.get("PLAN_ONLY"), "1")  # nothing is drawn while the plan is measured
            return [(p, 4.0, 0.0, {"caption": {"title": "x"}}) for p in pics], [], ([0] * 10, [(0, 60)], [])
        with mock.patch.object(make, "timeline", timeline), mock.patch.object(make, "STRICT", True), \
                mock.patch.object(make.fx, "frames", lambda *a: drawn.append(a)):
            with self.assertRaisesRegex(SystemExit, r"plan gate: .*full screen cards per minute: 0.00 FIX"):
                make.plan_gate({"chapters": [{"title": "c"}]}, [{}], [("a.wav", 60 - make.render.PAD, [])], d)
        got = json.loads((d / "plan_gate.json").read_text())
        self.assertEqual(len(got["measured"]), 12)
        self.assertEqual((got["measured"]["different pictures per minute"], drawn), (15.0, []))
        self.assertNotIn("PLAN_ONLY", os.environ)

    def test_before_the_pictures_the_gate_leaves_their_check_for_later_and_draws_none(self):
        import make
        import visuals
        d = Path(tempfile.mkdtemp())
        pics = [d / f"s{k}.jpg" for k in range(12)]
        timeline = lambda ch, dur, words, folder, plan, first: (
            [(p, 5.0, 0.0, {"caption": {"title": "x"}}) for p in pics], [], ([0] * 10, [(0, 60)], []))
        with mock.patch.object(make, "timeline", timeline), mock.patch.object(make, "STRICT", True):
            with self.assertRaises(SystemExit) as err:
                make.plan_gate({"chapters": [{"title": "c"}]}, [{}], [("a.wav", 60 - make.render.PAD, [])], d, pictures=False)
        self.assertNotIn("pictures that fit", str(err.exception))
        self.assertNotIn("pictures that fit their line (%)", json.loads((d / "plan_gate.json").read_text())["measured"])
        with mock.patch.dict(os.environ, {"PLAN_ONLY": "1"}), mock.patch.object(visuals, "local") as local, \
                mock.patch.object(visuals, "fetch") as fetch:
            got = visuals.scenes(["a fort at dawn", "a river crossing"], d / "img")
        self.assertEqual(len(got), 2)
        self.assertTrue(all(isinstance(x, Path) and not x.exists() for x in got))
        local.assert_not_called(), fetch.assert_not_called()

    def test_a_caption_stays_on_over_the_rest_of_its_sentence(self):
        import make
        import qa
        shots = [[Path(f"p{k}.jpg"), 3.0, 0.0, {}] for k in range(6)]
        first = make.put_caption(shots, 1.0, {"kind": "caption", "title": "633"})
        nxt = make.put_caption(shots, 13.0, {"kind": "caption", "title": "Hira"})
        title = make.put_caption(shots, 9.5, {"kind": "title", "title": "Night Raid"})
        held = make.hold_captions(shots, lambda t: 8.0 if t < 8 else 18.0)
        caps = [s[3].get("caption") for s in shots]
        self.assertEqual([c and c["title"] for c in caps], ["633", "633", "633", "Night Raid", "Hira", "Hira"])
        self.assertEqual([(c.get("held"), c.get("from"), c.get("dur")) for c in caps[:3]],
                         [(None, None, 8.0), (True, 2.0, 8.0), (True, 5.0, 8.0)])
        self.assertEqual((len(held[id(first)]), len(held[id(nxt)]), id(title) in held), (3, 2, False), "a title lands once")
        first["at"], title["at"], nxt["at"] = 1.0, 0.5, 1.0  # timeline sets them once the cuts are final
        self.assertEqual(qa.timing([(shots, None)]), 0.0, "a held caption is not a second landing")

    def test_a_card_is_planned_before_its_chapter_has_a_picture(self):
        import make
        img = Path(tempfile.mkdtemp()) / "img" / "00"  # the plan gate runs before any picture is drawn
        path, _ = make.card_spec({"kind": "list", "items": ["a", "b"]}, img, [])
        self.assertTrue(path.exists() and path.parent == img)

    def test_no_picture_stays_on_longer_than_eight_seconds(self):
        import make
        import qa
        pic = lambda name, n: [[Path(f"{name}.jpg"), 3.0, 0.0, {}] for _ in range(n)]
        shots = make.break_long_runs(pic("a", 3) + pic("b", 1) + pic("c", 2))
        self.assertEqual([s[0].stem for s in shots], ["a", "a", "b", "b", "c", "c"], "the next beat comes a little early")
        self.assertLessEqual(qa.longest_same(shots), 8.0)
        end = make.break_long_runs(pic("a", 1) + pic("b", 3))
        self.assertEqual([s[0].stem for s in end], ["a", "a", "b", "b"], "at the end, the beat before")
        one = make.break_long_runs([[Path("a.jpg"), 2.0, 0.0, {}], [Path("b.jpg"), 9.0, 0.0, {"caption": {"title": "x", "_t": 3.0}}],
                                    [Path("c.jpg"), 3.0, 0.0, {}]])
        self.assertEqual([(s[0].stem, s[1], "caption" in s[3]) for s in one],
                         [("a", 2.0, False), ("b", 4.5, True), ("c", 4.5, False), ("c", 3.0, False)], "one long shot in two")
        late = make.break_long_runs([[Path("b.jpg"), 9.0, 0.0, {"caption": {"title": "x", "_t": 6.0}}]])
        self.assertEqual(["caption" in s[3] for s in late], [False, True], "the caption stays on its word")
        self.assertEqual(make.beat_lengths({"shots": [("a", 3), ("b", 2)]}, 10.0, [(0, 0, "w")] * 3 + [(4.5, 0, "w")] * 2),
                         [4.5, 5.5])
        tight = make.break_long_runs([[Path("a.jpg"), 6.0, 0.0, {}], [Path("b.jpg"), 4.0, 0.0, {}], [Path("b.jpg"), 4.0, 0.0, {}],
                                      [Path("c.jpg"), 4.0, 0.0, {}], [Path("c.jpg"), 3.0, 0.0, {}]])
        self.assertEqual([(s[0].stem, s[1]) for s in tight], [("a", 6.0), ("b", 4.0), ("b", 3.5), ("c", 0.5), ("c", 4.0), ("c", 3.0)],
                         "only the half second too many goes to the next picture")
        card = make.break_long_runs(pic("a", 3) + [[Path("card_1.json"), 3.0, 0.0]])
        self.assertEqual([s[0].stem for s in card], ["a", "a", "a", "card_1"], "a card is never borrowed")

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
        env = {"LLM_SEE": ",".join(("a", "b", "c") + tuple(listed)), **(env or {})}
        d = tempfile.mkdtemp()
        from PIL import Image
        pic = Path(d) / "p.jpg"
        Image.new("RGB", (64, 36)).save(pic)
        with mock.patch.object(writer, "_client", client), mock.patch.object(writer, "_eyes", {}), \
                mock.patch.dict(os.environ, env), mock.patch("time.sleep") as slept:
            try:
                return writer.see(["fits?", pic]), calls, slept
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
        got, calls, _ = self.vision(answer, listed=("gemini-9-flash",))
        self.assertEqual(got, {"fits": True})
        self.assertEqual([m for m, probe in calls if not probe], ["gemini-9-flash"])

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

    def test_chapters_go_on_with_flash_lite_once_every_flash_model_is_spent_and_spent_ones_are_not_asked_again(self):
        import writer
        spent = Exception("429 You exceeded your current quota GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        asked, said = [], []

        def ask(model, *a):
            asked.append(model)
            if "lite" not in model:
                raise spent
            return "a chapter"
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "k", "LLM_WRITE": ""}), mock.patch.object(writer, "_client", object()), \
                mock.patch.object(writer, "ask", side_effect=ask), mock.patch.object(writer, "_out", {}), \
                mock.patch.object(writer, "_lite_told", set()), mock.patch.object(writer, "say", side_effect=said.append):
            self.assertEqual(writer.llm("write chapter 1"), "a chapter")
            first = list(asked)
            self.assertEqual(writer.llm("write chapter 2"), "a chapter")
        self.assertEqual(first[:5], ["gemini-flash-latest", "gemini-3.6-flash", "gemini-4-hydrogen", "gemini-3-flash-preview",
                                     "gemini-2.5-flash"])
        self.assertEqual(asked[len(first):], ["gemini-3.5-flash-lite"], "the spent Flash models are not asked again")
        self.assertEqual(sum(l.startswith("gemini: the Flash models have no calls left") for l in said), 1)

    def test_gemma_gets_no_json_mode_and_a_long_prompt_skips_it_and_researches_share_the_rate(self):
        """Gemma 4 has 14,400 calls a day but 16k tokens a minute and no JSON mode; three researches together each
        take a third of every model's rate."""
        import writer
        sent = []
        client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(
            create=lambda **kw: sent.append(kw) or types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='{"ok": true}'))], usage=None))))
        with mock.patch.object(writer, "_client", client), mock.patch.object(writer, "wait_turn"):
            writer.ask("gemma-4-31b-it", "x", 0, {"response_format": {"type": "json_object"}, "reasoning_effort": "low"}, 5)
            writer.ask("gemini-3.1-flash-lite", "x", 0, {"response_format": {"type": "json_object"}}, 5)
        self.assertNotIn("response_format", sent[0])
        self.assertNotIn("reasoning_effort", sent[0])
        self.assertIn("response_format", sent[1])
        self.assertFalse(writer.fits("gemma-4-31b-it", "word " * 20_000), "a 25k token prompt cannot go to Gemma")
        self.assertTrue(writer.fits("gemini-3.1-flash-lite", "word " * 20_000))
        with mock.patch.dict(os.environ, {"LLM_SHARE": "0.33"}):
            self.assertEqual([round(x, 2) for x in writer.rate("gemini-3.1-flash-lite")], [3.96, 66000.0])
        with mock.patch.object(writer, "_pace", {}), mock.patch("time.time", return_value=1000.0), \
                mock.patch("time.sleep") as slept, mock.patch.dict(os.environ, {"LLM_SHARE": ""}):
            writer.wait_turn("gemma-4-26b-a4b-it", 6400)  # half of the 12.8k tokens a minute it is paced to
            writer.wait_turn("gemma-4-26b-a4b-it", 100)
        self.assertEqual([round(c.args[0]) for c in slept.call_args_list], [0, 30])

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

    def test_once_the_api_is_down_no_other_picture_waits_ten_minutes_again(self):
        import writer
        from PIL import Image
        pic = Path(tempfile.mkdtemp()) / "p.jpg"
        Image.new("RGB", (32, 18)).save(pic)
        with mock.patch.object(writer, "_eyes", {"down": "service busy: no model could look at x.jpg", "live": ["a"]}), \
                mock.patch.object(writer, "_client", object()), mock.patch("time.sleep") as slept:
            with self.assertRaisesRegex(SystemExit, "service busy"):
                writer.see(["fits?", pic])
        slept.assert_not_called()

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

    def test_a_paused_research_keeps_what_it_made_on_drive(self):
        """A research that stops for want of calls saves its folder, so the next run reads no source twice."""
        k = kernel({"stage": "research", "nonce": "n1", "id": "d8", "title": "Day 8", "slug": "day-8", "day": 8})
        with tempfile.TemporaryDirectory() as d, mock.patch.object(k, "folder", return_value=Path(d)), \
                mock.patch.object(k, "state") as state, mock.patch.object(k, "hook") as hook:
            k.report(k.Busy("service busy: wait=600 the write models have no free calls left today"))
        state.assert_called_once_with("put")
        self.assertEqual((hook.call_args.args[0], hook.call_args.kwargs["kind"], hook.call_args.kwargs["wait"]),
                         ("failed", "busy", 600))

    def test_a_run_the_worker_replaced_stops_before_it_installs_anything(self):
        k = kernel({"stage": "gpu", "nonce": "old", "id": "d1", "title": "Day 1", "slug": "day-1", "day": 1})
        with mock.patch.object(k, "unpack"), mock.patch.object(k, "install") as install, \
                mock.patch.object(k, "video") as video, \
                mock.patch.object(k, "hook", return_value={"ok": True, "ignored": "not a run the worker is waiting for"}):
            k.main()
        install.assert_not_called()
        video.assert_not_called()

    def test_one_gpu_session_makes_every_video_and_one_failing_does_not_stop_the_others(self):
        jobs = [{"id": f"d{k}", "title": f"Day {k}", "slug": f"day-{k}", "day": k, "minutes": 22} for k in (1, 2, 3)]
        k = kernel({"stage": "gpu", "nonce": "n1", **jobs[0], "jobs": jobs})
        said, made = [], []

        def video(stage):
            made.append(k.RUN["id"])
            if k.RUN["id"] == "d2":
                raise RuntimeError("pictures failed")
            k.hook("done", chapters=1)
        hook = lambda event, **x: said.append((k.RUN["id"], event, x.get("kind")))
        with mock.patch.object(k, "unpack"), mock.patch.object(k, "install"), mock.patch.object(k, "video", video), \
                mock.patch.object(k, "hook", hook), mock.patch.object(k, "checks"):
            k.main()
        self.assertEqual(made, ["d1", "d2", "d3"])
        self.assertEqual(said, [("d1", "stage", None), ("d1", "done", None), ("d2", "stage", None),
                                ("d2", "failed", "error"), ("d3", "stage", None), ("d3", "done", None)])

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

    def test_no_card_stands_still_after_its_elements_land(self):
        """Day 6 was held for a one pin map that stood still for 3 s: every kind of card, as long as the longest card
        of a film, keeps moving for the film's still check."""
        import qa
        import render
        specs = [{"kind": "map", "country": "SAU", "places": [{"name": "Mecca", "lat": 21.4, "lon": 39.8}]},
                 {"kind": "list", "title": "Three armies", "items": ["Khalid", "Qaqa", "Abu Layla"]},
                 {"kind": "timeline", "title": "t", "events": [{"year": 633, "label": "a"}, {"year": 634, "label": "b"}]},
                 {"kind": "compare", "left": {"title": "a", "items": ["x"]}, "right": {"title": "b", "items": ["y"]}}]
        with tempfile.TemporaryDirectory() as d:
            for spec in specs:
                src = Path(d) / "card.json"
                src.write_text(json.dumps(spec))
                still = qa.motion(qa.frames(render.segment(src, int(5.7 * render.FPS), 0, Path(d) / "card.mp4")))["still"]
                self.assertLessEqual(still, qa.TARGETS["longest still moment (s)"][1], spec["kind"])

    def test_strict_effects_stop_when_not_set_up(self):
        import fx
        with mock.patch.dict(os.environ, {"STRICT": "1", "FX": "1"}), mock.patch.object(fx, "node", return_value=None):
            with self.assertRaises(SystemExit):
                fx.ready()


if __name__ == "__main__":
    unittest.main()


class Upload(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "cloud"))
        import youtube
        self.yt = youtube

    def test_the_settings_become_the_videos_snippet_and_status(self):
        pack = {"description": "d" * 6000, "tags": ["Battle of Hira", "Khalid ibn al-Walid"] + [f"tag{k}" for k in range(80)]}
        got = self.yt.body(pack, "T" * 120, "2026-11-26T16:00:00Z", {"visibility": "Public", "category": "Education",
                                                                     "made_for_kids": "No", "language": "English"})
        self.assertEqual(len(got["snippet"]["title"]), 100)
        self.assertEqual(len(got["snippet"]["description"]), 5000)
        self.assertLessEqual(sum(len(t) + (2 if " " in t else 0) + 1 for t in got["snippet"]["tags"]), 500)
        self.assertEqual(got["snippet"]["categoryId"], "27")
        self.assertEqual(got["status"], {"privacyStatus": "private", "publishAt": "2026-11-26T16:00:00Z",
                                         "selfDeclaredMadeForKids": False, "license": "youtube", "embeddable": True,
                                         "containsSyntheticMedia": False})
        unlisted = self.yt.body({}, "T", "2026-11-26T16:00:00Z", {"visibility": "Unlisted"})["status"]
        self.assertEqual((unlisted["privacyStatus"], "publishAt" in unlisted), ("unlisted", False))

    def test_a_dropped_upload_goes_on_from_the_last_byte_youtube_has(self):
        video = Path(tempfile.mkdtemp()) / "f.mp4"
        video.write_bytes(b"x" * 100)
        puts = []

        def put(url, data=None, headers=None, timeout=None):
            puts.append(headers["Content-Range"])
            if len(puts) == 1:
                import requests
                raise requests.ConnectionError("reset")
            if headers["Content-Range"] == "bytes */100":
                return types.SimpleNamespace(status_code=308, headers={"Range": "bytes=0-39"})
            return types.SimpleNamespace(status_code=200, headers={}, json=lambda: {"id": "vid1"})
        start = types.SimpleNamespace(ok=True, headers={"Location": "https://up/1"})
        with mock.patch.object(self.yt.drive, "token", return_value="t"), mock.patch("requests.post", return_value=start), \
                mock.patch("requests.put", side_effect=put), mock.patch("time.sleep"):
            self.assertEqual(self.yt.send(video, {"snippet": {}}, True), "vid1")
        self.assertEqual(puts, ["bytes 0-99/100", "bytes */100", "bytes 40-99/100"])


class ResearchOnce(unittest.TestCase):
    def test_a_daily_video_researched_before_keeps_its_script_and_sources(self):
        home = Path(tempfile.mkdtemp())
        run = {"id": "siege", "title": "Siege", "slug": "siege", "stage": "research", "nonce": "n", "day": 5}
        k = kernel(run, home)
        sys.path.insert(0, str(ROOT / "cloud"))
        import drive
        said = []

        def get(direction):
            (home / "output" / "siege").mkdir(parents=True)
            (home / "output" / "siege" / "script.md").write_text("# Siege")
        with mock.patch.object(drive, "find", return_value="file1"), mock.patch.object(k, "state", get), \
                mock.patch.object(k, "hook", lambda event, **kw: said.append(event)), mock.patch.object(k, "work") as work:
            k.video("research")
        work.assert_not_called()
        self.assertEqual(said, ["done"])
        smoke = kernel({**run, "day": 0}, Path(tempfile.mkdtemp()))
        with mock.patch.object(drive, "find", return_value="file1") as found, mock.patch.object(smoke, "work"), \
                mock.patch.object(smoke, "state"), mock.patch.object(smoke, "hook"):
            smoke.video("research")
        found.assert_not_called()  # a smoke or test film is researched every time: it is there to test that
