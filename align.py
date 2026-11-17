"""Exact word times. The voice model only says when each sentence starts and ends; inside a sentence the word times
were guessed from word lengths. Here a speech model (wav2vec2, the MMS forced aligner of torchaudio) listens to every
sentence and finds where each word really is, so a card lands on the spoken word. Runs on the CPU.

python align.py <voice.wav> <words.json>     align one file and print how far the guesses were off
"""
import json
import os
import re
import sys
import wave

import numpy as np

RATE = 16000


def clean(word):
    """Letters the aligner knows. Numbers are read out in words ("1518" becomes "fifteen eighteen"), so a word with a
    digit becomes "*", which matches whatever is said there."""
    letters = re.sub(r"[^a-z']", "", word.lower())
    return letters if letters and not re.search(r"\d", word) else "*"


def sentences(words):
    group = []
    for i, (_, _, w) in enumerate(words):
        group.append(i)
        if re.search(r"[.!?…][\"')\]]*$", w):
            yield group
            group = []
    if group:
        yield group


def refine(path, words):
    try:
        import certifi
        import torch
        import torchaudio
    except ImportError:
        return words
    # the python.org Python on a Mac has no certificates of its own, so the model download needs certifi's
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    bundle = torchaudio.pipelines.MMS_FA
    model, tokenizer, aligner = bundle.get_model().eval(), bundle.get_tokenizer(), bundle.get_aligner()
    with wave.open(str(path)) as f:
        rate, pcm = f.getframerate(), np.frombuffer(f.readframes(f.getnframes()), np.int16)
    audio = torchaudio.functional.resample(torch.from_numpy(pcm.astype(np.float32) / 32768), rate, RATE)
    out, done = [tuple(w) for w in words], 0
    for group in sentences(words):
        a = max(0.0, words[group[0]][0] - 0.15)
        seg = audio[int(a * RATE): int((words[group[-1]][1] + 0.15) * RATE)]
        try:
            with torch.inference_mode():
                emission, _ = model(seg[None])
            spans = aligner(emission[0], tokenizer([clean(words[i][2]) for i in group]))
        except Exception:  # a sentence too short or too odd to align keeps its guessed times
            continue
        step = seg.numel() / emission.shape[1] / RATE
        for i, span in zip(group, spans):
            out[i] = (a + span[0].start * step, a + span[-1].end * step, words[i][2])
        done += len(group)
    print(f"  word times: {done} of {len(words)} words aligned", flush=True)
    return out


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__.strip())
    guess = json.loads(open(sys.argv[2], encoding="utf-8").read())
    exact = refine(sys.argv[1], guess)
    off = [abs(x[0] - y[0]) for x, y in zip(guess, exact)]
    print(f"guessed starts were off by {np.mean(off):.2f} s on average, {max(off):.2f} s at most")
