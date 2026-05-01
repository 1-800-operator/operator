"""
Entry point for the Operator test harness.
Imports shared utilities from utils.py.
"""

from utils import format_duration, clamp, chunk_list


def run_test(duration_seconds: float, chunk_size: int = 10) -> dict:
    """Run a simulated test and return a summary."""
    label = format_duration(duration_seconds)
    clamped = clamp(duration_seconds, 0, 3600)
    items = list(range(int(clamped)))
    chunks = chunk_list(items, chunk_size)
    return {
        "label": label,
        "clamped_duration": clamped,
        "num_chunks": len(chunks),
    }


if __name__ == "__main__":
    result = run_test(125.5)
    print(result)
