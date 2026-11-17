"""The director: the strongest model on Kaggle plans the whole edit of a video in one benchmark run.

For every chapter it answers three prompts from writer.py: EDIT (proof cards, maps, documents on chosen words),
BEATS (captions, mood of the music, place sound, turning points, story sounds) and DIRECT (camera move and pace of
every picture, transitions, light leaks, caption animation, sound design hits, particles, the chapter opening and the
music drop before a reveal). The answers land in parts/edit01.json, beats01.json and direct01.json, the same files
the Gemini key writes, so whatever Kaggle could not answer is planned by Gemini afterwards.

The models are tried in order (DIRECTOR_MODELS); the first that answers plans the video. Benchmark runs use no GPU
hours. DIRECTOR=0 skips Kaggle and leaves the plan to the Gemini key.
"""
import ast
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import writer

MODELS = [m.strip() for m in os.getenv("DIRECTOR_MODELS", "claude-opus-5-5-default,gpt-6.1-sol,gemini-4-argon").split(",") if m.strip()]
TASK = "history-studio-director"

TASK_CODE = '''import base64
import contextvars
import json
import os
from concurrent.futures import ThreadPoolExecutor

import kaggle_benchmarks as kbench

JOBS = json.loads(base64.b64decode("{b64}").decode())


# Kaggle reserves the cost of the longest possible answer before each call, so the answer length is capped
CAP = {cap}


def ask(llm, key, prompt):
    for kwargs in ({{"reasoning": "high", "extra_api_params": {{"max_tokens": CAP}}}},
                   {{"extra_api_params": {{"max_completion_tokens": CAP}}}}, {{}}):
        try:
            with kbench.chats.new(key, orphan=True):
                return llm.prompt(prompt, **kwargs)
        except Exception as err:
            print("failed", key, str(err)[:300])
            if "quota" in str(err):
                return None
    return None


@kbench.task(name="{task}", description="Edit plan of one History Studio video")
def direct(llm) -> dict:
    model = os.getenv("LLM_DEFAULT", "")
    print("model:", model)
    if "3.8-flash" in model:  # the run kaggle starts by itself on push, with its default model
        return {{"answers": {{}}}}
    with ThreadPoolExecutor(2) as ex:
        runs = {{k: ex.submit(contextvars.copy_context().run, ask, llm, k, p) for k, p in JOBS.items()}}
        answers = {{k: r.result() for k, r in runs.items() if r.result()}}
    for k, text in answers.items():
        print("ANS" + "_START " + k + "\\n" + text + "\\nANS" + "_END")
    return {{"answers": answers}}


direct.run(kbench.llm)
'''


def kaggle(jobs, work):
    """jobs: {key: prompt}. Each model gets what the ones before it left unanswered (a model whose Kaggle quota
    runs out mid way answers only part). Returns {key: (answer text, model)}. The run logs stay in
    work/../director_log.txt."""
    exe = Path(sys.executable).with_name("kaggle")
    k = lambda *a, t=4000: subprocess.run([str(exe) if exe.exists() else "kaggle", "benchmarks", "tasks", *a],
                                          capture_output=True, text=True, timeout=t)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    log, done = work.parent / "director_log.txt", {}
    log.write_text("")
    for model in MODELS:
        left = {key: p for key, p in jobs.items() if key not in done}
        if not left:
            break
        code = TASK_CODE.format(b64=base64.b64encode(json.dumps(left).encode()).decode(), task=TASK,
                                cap=int(os.getenv("DIRECTOR_TOKENS", "16000")))
        (work / "task.py").write_text(code)
        r = k("push", TASK, "-f", str(work / "task.py"), "--wait", "900")
        if r.returncode:
            writer.say(f"director: kaggle push failed: {(r.stderr or r.stdout).strip()[-200:]}")
            break
        writer.say(f"director: {model} is planning the edit ({len(left)} prompts, one Kaggle run)")
        out = work / model
        r = k("run", TASK, "-m", model, "--wait", "3600")
        if not r.returncode:
            r = k("download", TASK, "-m", model, "-o", str(out), "-f")
        got = answers(out) if not r.returncode else {}
        trace = k("logs", TASK, "-m", model, t=300)
        with log.open("a") as f:
            f.write(f"== {model}\n" + "\n".join(x[:300] for x in trace.stdout.splitlines() if "failed" in x or "model:" in x) + "\n")
        done.update({key: (text, model) for key, text in got.items()})
        writer.say(f"director: {model} answered {len(got)} of {len(left)}"
                   + ("" if got else f" ({(r.stderr or r.stdout).strip()[-150:]})"))
    return done


def answers(out):
    """{key: text} from the downloaded run files: the task's returned dict, else the printed markers."""
    got = {}

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("answers"), dict):
                got.update({k: v for k, v in node["answers"].items() if isinstance(v, str)})
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            got.update({k: v for k, v in re.findall(r"ANS_START (\S+)\n(.*?)\nANS_END", node, re.S) if k not in got})
            if "answers" in node and node.lstrip()[:1] in "{[":
                for parse in (json.loads, ast.literal_eval):
                    try:
                        return walk(parse(node))
                    except (ValueError, SyntaxError):
                        pass

    for f in sorted(Path(out).rglob("*")) if Path(out).exists() else []:
        if f.is_file() and f.stat().st_size < 50_000_000:
            text = f.read_text(encoding="utf-8", errors="ignore")
            try:
                walk(json.loads(text))
            except ValueError:
                walk(text)
    return got


def plan(folder, jobs):
    """jobs: {file name: (prompt, list key)}. Writes every answered file into parts/ and returns the model used."""
    if os.getenv("DIRECTOR", "1") != "1" or not jobs:
        return None
    try:
        got = kaggle({name: p for name, (p, _) in jobs.items()}, folder / ".kaggle_director")
    except (Exception, SystemExit) as err:
        writer.say(f"director: Kaggle not used ({str(err)[:100]})")
        return None
    used = None
    for name, (text, model) in got.items():
        try:
            obj = writer.objects(text)
        except ValueError:
            continue
        key = jobs[name][1]
        data = obj if key == "direct" else obj.get(key) or obj.get("items") or []
        if data:
            (folder / "parts" / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            used = model
    shutil.rmtree(folder / ".kaggle_director", ignore_errors=True)
    if got:
        models = sorted({m for _, m in got.values()})
        (folder / "parts" / "director.txt").write_text(", ".join(models))
    return used
