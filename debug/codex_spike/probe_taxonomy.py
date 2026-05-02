"""
Phase-0 probe: enumerate codex's parsed_cmd.type taxonomy.

Spawns codex mcp-server, runs several command-shape prompts under
approval-policy=untrusted, and records every parsed_cmd entry the
elicitations carry.

Auto-approves everything via {"decision": "approved"} so probes complete
without hanging. Logs each observed (type, cmd) pair.

Output: debug/codex_spike/probe_taxonomy.log + summary at end.
"""
import json
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

LOG = Path("/Users/jojo/Desktop/operator/debug/codex_spike/probe_taxonomy.log")
SUMMARY = Path("/Users/jojo/Desktop/operator/debug/codex_spike/probe_taxonomy.summary.md")

PROMPTS = [
    ("read_one", "Read /tmp/spike_target.txt and tell me what's in it."),
    ("grep", "Run `grep payload /tmp/spike_target.txt` and report the matching lines."),
    ("ls", "Run `ls /tmp | head -3` and list the names."),
    ("find", "Run `find /tmp -maxdepth 1 -name 'spike_*' -type f` and list the matches."),
    ("write_simple", "Run `echo abc > /tmp/codex_taxonomy_write.txt` and confirm."),
    ("compound", "Run `echo abc > /tmp/codex_taxonomy_write2.txt && cat /tmp/codex_taxonomy_write2.txt` and report."),
    ("python", "Run `python3 -c 'print(1+1)'` and report the output."),
    ("network", "Run `curl -sI https://example.com | head -1` and report the status line."),
    ("destructive", "Run `rm /tmp/codex_taxonomy_write.txt` and confirm."),
]

events = []
observed_types: dict[str, list[str]] = defaultdict(list)  # type -> list of cmds
lock = threading.Lock()


def log_event(label: str, payload):
    line = f"[{time.strftime('%H:%M:%S')}] {label}: {json.dumps(payload)[:1500]}"
    with lock:
        events.append(line)


def stdout_reader(stream, ready_evt: threading.Event, on_request, on_response):
    for raw in stream:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            log_event("RAW", {"line": raw[:200]})
            continue
        if "id" in obj and "method" in obj:
            log_event("SERVER_REQUEST", obj)
            on_request(obj)
        elif "id" in obj and ("result" in obj or "error" in obj):
            log_event("RESPONSE", obj)
            on_response(obj)
        elif obj.get("method") == "codex/event":
            msg = obj.get("params", {}).get("msg", {})
            mtype = msg.get("type")
            if mtype == "exec_approval_request":
                parsed = msg.get("parsed_cmd", [])
                cmd = " ".join(msg.get("command", [])[:200])
                with lock:
                    for entry in parsed:
                        observed_types[entry.get("type", "?")].append(entry.get("cmd", cmd)[:120])
                log_event("EXEC_APPROVAL_REQ", {"parsed": parsed, "cmd": cmd[:120]})
            elif mtype == "session_configured":
                ready_evt.set()
                log_event("SESSION_CONFIGURED", {})
            elif mtype in ("exec_command_begin", "exec_command_end"):
                pass  # too noisy
            else:
                log_event("EVENT", {"type": mtype})
        else:
            log_event("OTHER", obj)


def stderr_reader(stream):
    for raw in stream:
        raw = raw.strip()
        if raw:
            log_event("STDERR", {"line": raw[:200]})


def main():
    proc = subprocess.Popen(
        ["codex", "mcp-server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    pending_responses = {}
    response_evt = threading.Event()

    def on_request(obj):
        # Auto-approve all elicitations.
        rid = obj["id"]
        method = obj.get("method", "")
        if "elicitation" in method:
            decision = {"decision": "approved"}
            send({"jsonrpc": "2.0", "id": rid, "result": decision})
        else:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "method not supported"}})

    def on_response(obj):
        rid = obj.get("id")
        pending_responses[rid] = obj
        response_evt.set()

    ready = threading.Event()
    t1 = threading.Thread(target=stdout_reader, args=(proc.stdout, ready, on_request, on_response), daemon=True)
    t1.start()
    t2 = threading.Thread(target=stderr_reader, args=(proc.stderr,), daemon=True)
    t2.start()

    def send(obj):
        line = json.dumps(obj)
        log_event("CLIENT_SEND", obj)
        proc.stdin.write(line + "\n")
        proc.stdin.flush()

    # Init.
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05",
                     "capabilities": {"elicitation": {}},
                     "clientInfo": {"name": "operator-spike", "version": "0.1"}}})
    response_evt.clear()
    while 1 not in pending_responses:
        time.sleep(0.1)
    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    time.sleep(0.5)

    thread_id = None
    for next_id, (label, prompt) in enumerate(PROMPTS, start=10):
        if thread_id is None:
            args = {"prompt": prompt, "approval-policy": "untrusted",
                    "sandbox": "read-only", "cwd": "/tmp"}
            send({"jsonrpc": "2.0", "id": next_id, "method": "tools/call",
                  "params": {"name": "codex", "arguments": args}})
        else:
            send({"jsonrpc": "2.0", "id": next_id, "method": "tools/call",
                  "params": {"name": "codex-reply",
                             "arguments": {"threadId": thread_id, "prompt": prompt}}})
        # Wait up to 75s per prompt.
        deadline = time.time() + 75
        while time.time() < deadline:
            time.sleep(0.5)
            if next_id in pending_responses:
                resp = pending_responses[next_id]
                if "result" in resp:
                    sc = resp["result"].get("structuredContent", {})
                    if sc.get("threadId"):
                        thread_id = sc["threadId"]
                break
        else:
            log_event("PROMPT_TIMEOUT", {"label": label})

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    LOG.write_text("\n".join(events) + "\n")

    summary_lines = ["# parsed_cmd.type taxonomy probe results\n"]
    for t, cmds in sorted(observed_types.items()):
        summary_lines.append(f"## `{t}` ({len(cmds)} occurrences)")
        for c in cmds[:5]:
            summary_lines.append(f"- `{c}`")
        if len(cmds) > 5:
            summary_lines.append(f"- ... and {len(cmds) - 5} more")
        summary_lines.append("")
    if not observed_types:
        summary_lines.append("(no parsed_cmd entries observed — every command was auto-allowed)\n")
    SUMMARY.write_text("\n".join(summary_lines))
    print("\n".join(summary_lines))
    print(f"\nFull log at: {LOG}")


if __name__ == "__main__":
    main()
