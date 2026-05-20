"""Deterministic test for _run_audio_tcc_warmup's fresh-probe polling.

The live TCC dialog can't be exercised on demand (macOS throttles repeated
prompts), so this stubs the two I/O boundaries — the disclaimed helper spawn
and the fresh `--probe` read — and asserts the warmup loop:

  1. returns the instant BOTH legs are answered (not after a fixed wait),
  2. keeps polling while either leg is still `not_determined`,
  3. kills the warmup helper on the way out (it would otherwise sit in
     capture mode forever),
  4. stops early if the helper exits on its own (e.g. mic denied).

Background: debug/14_36_tcc_warmup_timing_spike/. The bug this guards against
is the helper's in-process TCCAccessPreflight being per-process stale, so
detection MUST come from fresh probes polled by the parent.

Run: python tests/test_tcc_warmup_polling.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _1_800_operator import __main__ as m
from _1_800_operator.pipeline import _disclaimed_spawn


class FakeHelper:
    """Stand-in for a DisclaimedProcess. Stays 'alive' until terminated."""

    def __init__(self, exit_after_polls=None):
        self.terminated = False
        self.killed = False
        self._polls = 0
        self._exit_after = exit_after_polls  # self-exit after N poll() calls

    def poll(self):
        self._polls += 1
        if self.terminated or self.killed:
            return 0
        if self._exit_after is not None and self._polls >= self._exit_after:
            return 5  # mimic mic-denied exit(5)
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _install_stubs(probe_sequence, helper):
    """Patch the spawn + probe boundaries; return a restore() callable."""
    seq = list(probe_sequence)
    last = seq[-1]

    def fake_probe():
        return seq.pop(0) if seq else last

    orig_spawn = _disclaimed_spawn.spawn_disclaimed
    orig_env = _disclaimed_spawn.minimal_helper_env
    orig_probe = m._probe_helper_tcc

    _disclaimed_spawn.spawn_disclaimed = lambda *a, **k: helper
    _disclaimed_spawn.minimal_helper_env = lambda *a, **k: {}
    m._probe_helper_tcc = fake_probe

    def restore():
        _disclaimed_spawn.spawn_disclaimed = orig_spawn
        _disclaimed_spawn.minimal_helper_env = orig_env
        m._probe_helper_tcc = orig_probe

    return restore


def test_returns_immediately_when_both_granted():
    helper = FakeHelper()
    # not_determined twice (mic lands first), then both ok.
    seq = [
        '{"system_audio":"not_determined","microphone":"not_determined"}',
        '{"system_audio":"not_determined","microphone":"ok"}',
        '{"system_audio":"ok","microphone":"ok"}',
    ]
    restore = _install_stubs(seq, helper)
    try:
        t0 = time.monotonic()
        sa, mic = m._run_audio_tcc_warmup(timeout_s=60.0)
        dt = time.monotonic() - t0
    finally:
        restore()
    assert (sa, mic) == ("ok", "ok"), (sa, mic)
    # 3 polls * 0.5s sleep ≈ 1.5s — must be nowhere near the 60s deadline.
    assert dt < 5, f"took {dt:.1f}s — did not early-bail on grant"
    assert helper.terminated or helper.killed, "warmup helper not killed"
    print(f"  ok: returned ('{sa}','{mic}') in {dt:.2f}s; helper killed")


def test_detects_denial_and_stops():
    helper = FakeHelper()
    seq = [
        '{"system_audio":"not_determined","microphone":"not_determined"}',
        '{"system_audio":"denied","microphone":"ok"}',  # a deny still 'answers'
    ]
    restore = _install_stubs(seq, helper)
    try:
        sa, mic = m._run_audio_tcc_warmup(timeout_s=60.0)
    finally:
        restore()
    assert (sa, mic) == ("denied", "ok"), (sa, mic)
    assert helper.terminated or helper.killed
    print(f"  ok: deny answered → returned ('{sa}','{mic}'); helper killed")


def test_stops_when_helper_exits_on_its_own():
    # Helper exits (poll != None) after a couple of checks; grant never lands.
    helper = FakeHelper(exit_after_polls=2)
    seq = ['{"system_audio":"not_determined","microphone":"not_determined"}']
    restore = _install_stubs(seq, helper)
    try:
        t0 = time.monotonic()
        sa, mic = m._run_audio_tcc_warmup(timeout_s=60.0)
        dt = time.monotonic() - t0
    finally:
        restore()
    assert dt < 5, f"took {dt:.1f}s — did not stop on helper exit"
    print(f"  ok: helper self-exit → stopped in {dt:.2f}s ('{sa}','{mic}')")


def test_unknown_is_retried_not_trusted():
    helper = FakeHelper()
    # transient probe failure ('unknown') must NOT be treated as answered.
    seq = [
        '{"system_audio":"ok","microphone":"unknown"}',
        '{"system_audio":"ok","microphone":"unknown"}',
        '{"system_audio":"ok","microphone":"ok"}',
    ]
    restore = _install_stubs(seq, helper)
    try:
        sa, mic = m._run_audio_tcc_warmup(timeout_s=60.0)
    finally:
        restore()
    assert (sa, mic) == ("ok", "ok"), (sa, mic)
    print(f"  ok: 'unknown' retried until resolved → ('{sa}','{mic}')")


if __name__ == "__main__":
    test_returns_immediately_when_both_granted()
    test_detects_denial_and_stops()
    test_stops_when_helper_exits_on_its_own()
    test_unknown_is_retried_not_trusted()
    print("All _run_audio_tcc_warmup polling tests passed.")
