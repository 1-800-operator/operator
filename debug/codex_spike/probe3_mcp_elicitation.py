"""
Probe 3 — does codex mcp-server round-trip approval requests back to the MCP client?

Spawns `codex mcp-server`, runs the standard initialize handshake, then calls
the `codex` tool with:
  - approval-policy=untrusted   (so EVERY shell command needs approval)
  - sandbox=read-only           (so even read-only writes wouldn't bypass)
  - prompt: "list files in /tmp"

We log every stdout line raw (one JSON object per line) and label it as
"response", "request" (server->client), or "notification".

If Codex sends a request back to us with method like
"elicitation/create" or a custom approval method, we print the full envelope
so we can decide whether the existing operator MCP client can route it to chat
for confirmation.

Timeout: 60s. Codex normally finishes in <20s; if we're hung waiting on an
unresponded approval request we'll see exactly that.
"""
import json
import subprocess
import sys
import threading
import time

LOG_PATH = "/Users/jojo/Desktop/operator/debug/codex_spike/probe3_mcp_elicitation.log"

events = []
lock = threading.Lock()


def log(label, payload):
    line = f"[{time.strftime('%H:%M:%S')}] {label}: {json.dumps(payload)}"
    with lock:
        events.append(line)
        print(line, flush=True)


def reader(stream):
    for raw in stream:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            log("RAW", {"line": raw})
            continue
        if "id" in obj and "result" in obj:
            log("RESPONSE", obj)
        elif "id" in obj and "error" in obj:
            log("RESPONSE_ERROR", obj)
        elif "id" in obj and "method" in obj:
            log("SERVER_REQUEST", obj)
        elif "method" in obj:
            log("NOTIFICATION", obj)
        else:
            log("OTHER", obj)


def stderr_reader(stream):
    for raw in stream:
        raw = raw.strip()
        if not raw:
            continue
        log("STDERR", {"line": raw[:300]})


def main():
    proc = subprocess.Popen(
        ["codex", "mcp-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    t1 = threading.Thread(target=reader, args=(proc.stdout,), daemon=True)
    t1.start()
    t2 = threading.Thread(target=stderr_reader, args=(proc.stderr,), daemon=True)
    t2.start()

    def send(obj):
        line = json.dumps(obj)
        log("CLIENT_SEND", obj)
        proc.stdin.write(line + "\n")
        proc.stdin.flush()

    send({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"elicitation": {}},
            "clientInfo": {"name": "operator-spike", "version": "0.1"},
        },
    })
    time.sleep(1)
    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    time.sleep(0.5)

    # The load-bearing call: ask Codex to do something that requires approval.
    # workspace-write sandbox + approval-policy=untrusted means even reads
    # get gated through approval (untrusted = always ask).
    send({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "codex",
            "arguments": {
                # Force a write (not in safe-command allowlist) to trigger approval.
                "prompt": "Run the shell command `echo probe3b > /tmp/codex_probe3b_write.txt` and confirm the write.",
                "approval-policy": "untrusted",
                "sandbox": "read-only",
                "cwd": "/tmp",
            },
        },
    })

    # Wait up to 60s. If we get a server request, log it and also send an
    # "approve" response in case Codex is blocking.
    deadline = time.time() + 60
    answered_ids = set()
    while time.time() < deadline:
        time.sleep(0.5)
        with lock:
            unanswered = [
                e for e in events
                if "SERVER_REQUEST" in e
            ]
        # Auto-approve any server-side request we haven't yet responded to.
        for line in unanswered:
            try:
                payload = json.loads(line.split("SERVER_REQUEST: ", 1)[1])
            except Exception:
                continue
            req_id = payload.get("id")
            if req_id in answered_ids:
                continue
            answered_ids.add(req_id)
            method = payload.get("method", "")
            log("CLIENT_AUTO_APPROVE", {"in_response_to": method, "id": req_id})
            # Send a generic "approved" response. Shape depends on the method;
            # for elicitation/create this needs to match the MCP elicitation
            # response schema. We try a few generic shapes.
            if "elicitation" in method:
                # Correct shape per Codex: ExecApprovalResponse needs `decision`.
                send({"jsonrpc": "2.0", "id": req_id,
                      "result": {"decision": "approved"}})
            else:
                send({"jsonrpc": "2.0", "id": req_id,
                      "result": {"decision": "approved"}})
        # Stop early if the tool/call response arrived.
        with lock:
            done = any('"id": 2' in e and "RESPONSE" in e for e in events)
        if done:
            break

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    with open(LOG_PATH, "w") as f:
        f.write("\n".join(events) + "\n")
    print(f"\nWrote {len(events)} events to {LOG_PATH}", flush=True)


if __name__ == "__main__":
    main()
