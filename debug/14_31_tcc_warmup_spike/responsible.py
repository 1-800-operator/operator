"""Return the macOS-assigned responsible PID for a given child PID.

Wraps the private `responsibility_get_pid_responsible_for_pid` in libSystem.
This is the same API hammered by Apple's own tools (Activity Monitor's
'Responsible Process' column). Stable since macOS 10.14.

Usage: python responsible.py <pid>
       prints responsible_pid on stdout (or -1 on error)
"""
from __future__ import annotations

import ctypes
import sys


def responsible_pid(target_pid: int) -> int:
    libc = ctypes.CDLL("/usr/lib/libSystem.dylib")
    fn = libc.responsibility_get_pid_responsible_for_pid
    fn.argtypes = [ctypes.c_int]
    fn.restype = ctypes.c_int
    return fn(target_pid)


if __name__ == "__main__":
    print(responsible_pid(int(sys.argv[1])))
