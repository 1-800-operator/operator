"""
Spike 2: Can two processes safely append to one JSONL via O_APPEND?

Load-bearing for Option B (worker subprocess writes captions directly to
the same JSONL the main process writes meta/end events to).

POSIX: O_APPEND atomically increments the file offset to end-of-file before
each write. The write itself is atomic *up to PIPE_BUF (≥512 bytes per
POSIX, 4096 on macOS/Linux for pipes — but for regular files the guarantee
is less crisp).* Real-world: a single write(2) syscall on a regular file
on macOS HFS+/APFS is atomic if the buffer fits in the page cache slot,
which is the common case for sub-PIPE_BUF writes.

Our captions are short (<500 bytes typical). But let's empirically verify
under contention that we never get interleaving.

Method: two child processes each write N lines (each line a distinct,
parseable JSON) to the same file using mode "a" + write + flush. Single
write(2) per line because Python's BufferedWriter coalesces. Both write as
fast as possible.

After: parse every line as JSON. Any line that fails to parse = bytes from
two processes interleaved within a single write. Verdict: pass/fail.
"""
import json
import multiprocessing as mp
import os
import tempfile
import time


def writer(path: str, tag: str, n: int) -> None:
    # Python opens with O_APPEND under mode="a". One write per line, no
    # buffering between lines (newline-terminated, flushed).
    with open(path, "a", buffering=1) as f:  # line-buffered → flush each \n
        for i in range(n):
            # Vary line length to stress different write sizes:
            #   ~200B normal caption
            #   ~1000B long caption with metadata
            #   ~3000B pathological worst case
            for sz in (200, 1000, 3000):
                payload = "x" * sz
                line = json.dumps({"writer": tag, "seq": i, "size": sz, "payload": payload})
                f.write(line + "\n")


def main():
    tmp = tempfile.mktemp(suffix=".jsonl")
    n_per = 300
    print(f"Two processes appending {n_per * 3} lines each to {tmp}")

    t0 = time.perf_counter()
    p1 = mp.Process(target=writer, args=(tmp, "A", n_per))
    p2 = mp.Process(target=writer, args=(tmp, "B", n_per))
    p1.start()
    p2.start()
    p1.join()
    p2.join()
    elapsed = time.perf_counter() - t0
    print(f"Wrote in {elapsed:.2f}s")

    parse_failures = 0
    line_count = 0
    counts = {"A": 0, "B": 0}
    with open(tmp) as f:
        for line in f:
            line_count += 1
            try:
                obj = json.loads(line)
                counts[obj["writer"]] += 1
            except json.JSONDecodeError:
                parse_failures += 1
                if parse_failures <= 3:
                    print(f"INTERLEAVED LINE #{line_count}: {line[:120]!r}...")

    expected = n_per * 3 * 2
    print(f"\nExpected lines: {expected}, got: {line_count}")
    print(f"Counts: {counts}")
    print(f"Parse failures (interleaved writes): {parse_failures}")
    if parse_failures == 0 and line_count == expected:
        print("\nPASS — O_APPEND is atomic on this filesystem for our payload sizes")
    else:
        print("\nFAIL — concurrent append produced interleaved or lost data")
    os.unlink(tmp)


if __name__ == "__main__":
    main()
