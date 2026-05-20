"""Transcribe the morning debug per-utterance WAVs and classify each by RMS +
the MIC_ECHO_FLOOR_RMS=0.04 floor, so we can SEE on real audio which mic-leg
captions are genuine speech vs faint incidental pickup the floor drops.

Run: <tool-venv-python> debug/14_37_aec_tune/transcribe_validate.py
"""
import glob, os, wave
import numpy as np
from faster_whisper import WhisperModel

FLOOR = 0.04
SR = 16000
base = os.path.expanduser("~/.operator/debug/audio_1779296397")

def load(path):
    w = wave.open(path, "rb"); sw = w.getsampwidth(); raw = w.readframes(w.getnframes()); w.close()
    if sw == 2: a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    else:       a = np.frombuffer(raw, np.float32).copy()
    return a

print("loading faster-whisper-large-v3-turbo-ct2 ...", flush=True)
model = WhisperModel("deepdml/faster-whisper-large-v3-turbo-ct2", device="cpu", compute_type="int8")

def transcribe(a):
    padded = np.concatenate([np.zeros(int(SR * 0.5), np.float32), a])
    segs, _ = model.transcribe(padded, beam_size=5, language="en")
    return "".join(s.text for s in segs).strip()

for leg in ("M", "S"):
    files = sorted(glob.glob(f"{base}/{leg}/*.wav"))
    if leg == "S":
        files = files[::max(1, len(files)//8)]  # sample ~8 of the S leg
    print(f"\n===== {leg} leg ({'all' if leg=='M' else 'sampled'} {len(files)}) =====", flush=True)
    print(f"{'rms':>6}  {'floor':<5}  text", flush=True)
    for f in files:
        a = load(f)
        if a.size == 0: continue
        rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))
        decision = "DROP" if (leg == "M" and rms < FLOOR) else "keep"
        txt = transcribe(a)
        print(f"{rms:6.3f}  {decision:<5}  {txt!r}", flush=True)
print("\ndone", flush=True)
