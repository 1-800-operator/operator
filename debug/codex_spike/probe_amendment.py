"""
Phase-0 probe: capture the approved_execpolicy_amendment dict shape AND verify
that sending it suppresses re-elicitation on a second identical command in the
same thread.

Strategy:
  1. Send first prompt → "echo first > /tmp/amendment_test.txt"
  2. On elicitation, send dict-form decision using the available_decisions hint
     pulled from the codex/event's exec_approval_request. Spike's
     observed shape: ["approved", {"approved_execpolicy_amendment":
     {"proposed_execpolicy_amendment": [...argv...]}}, "abort"].
  3. Send a second prompt in the same thread → "run that exact same echo
     command again". Watch: does Codex elicit again, or auto-allow?
  4. Send a third prompt with a SLIGHTLY different argv → does the amendment
     scope only to the exact argv (won't help) or to a pattern (will help)?
"""
import json
import subprocess
import threading
import time
from pathlib import Path

LOG = Path("/Users/jojo/Desktop/operator/debug/codex_spike/probe_amendment.log")

events = []
lock = threading.Lock()
pending_responses = {}
exec_approval_envelopes = []  # full codex/event exec_approval_request bodies


def log_event(label, payload):
    line = f"[{time.strftime('%H:%M:%S')}] {label}: {json.dumps(payload)[:1500]}"
    with lock:
        events.append(line)
        print(line, flush=True)


def stdout_reader(stream, on_request):
    for raw in stream:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "id" in obj and "method" in obj:
            log_event("SERVER_REQUEST", obj)
            on_request(obj)
        elif "id" in obj and ("result" in obj or "error" in obj):
            log_event("RESPONSE", obj)
            pending_responses[obj["id"]] = obj
        elif obj.get("method") == "codex/event":
            msg = obj.get("params", {}).get("msg", {})
            if msg.get("type") == "exec_approval_request":
                exec_approval_envelopes.append(msg)
                log_event("EXEC_APPROVAL_REQ", msg)
            elif msg.get("type") == "session_configured":
                log_event("SESSION", {"id": msg.get("session_id")})


def main():
    proc = subprocess.Popen(
        ["codex", "mcp-server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    elicitation_count = 0

    def on_request(obj):
        nonlocal elicitation_count
        rid = obj["id"]
        method = obj.get("method", "")
        params = obj.get("params", {})
        if "elicitation" in method:
            elicitation_count += 1
            # First elicitation: send the amendment-form decision pulled from
            # the most-recent exec_approval_request. Second/third: send single
            # "approved" so we don't compound the test.
            if elicitation_count == 1 and exec_approval_envelopes:
                ear = exec_approval_envelopes[-1]
                # Per spike: available_decisions = ["approved",
                #   {"approved_execpolicy_amendment": {"proposed_execpolicy_amendment": [...]}},
                #   "abort"].
                amendment_proposal = ear.get("proposed_execpolicy_amendment") or ear.get("command")
                decision = {"approved_execpolicy_amendment": {
                    "proposed_execpolicy_amendment": amendment_proposal
                }}
                log_event("CLIENT_AMENDMENT_DECISION", {"decision": decision})
                proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": rid,
                    "result": {"decision": decision}
                }) + "\n")
            else:
                log_event("CLIENT_PLAIN_APPROVE", {"id": rid, "n": elicitation_count})
                proc.stdin.write(json.dumps({
                    "jsonrpc": "2.0", "id": rid,
                    "result": {"decision": "approved"}
                }) + "\n")
            proc.stdin.flush()

    threading.Thread(target=stdout_reader, args=(proc.stdout, on_request), daemon=True).start()
    threading.Thread(target=lambda: [None for _ in proc.stderr], daemon=True).start()

    def send(obj):
        log_event("CLIENT_SEND", obj)
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05",
                     "capabilities": {"elicitation": {}},
                     "clientInfo": {"name": "operator-spike", "version": "0.1"}}})
    while 1 not in pending_responses:
        time.sleep(0.1)
    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    time.sleep(0.5)

    PROMPTS = [
        "Run `echo amendment-test-one > /tmp/amendment_test.txt` and confirm.",
        "Now do the EXACT same command again: `echo amendment-test-one > /tmp/amendment_test.txt`. Same argv.",
        "Now run a slightly different one: `echo amendment-test-TWO > /tmp/amendment_test.txt`.",
    ]

    thread_id = None
    for next_id, prompt in enumerate(PROMPTS, start=10):
        if thread_id is None:
            args = {"prompt": prompt, "approval-policy": "untrusted",
                    "sandbox": "read-only", "cwd": "/tmp"}
            send({"jsonrpc": "2.0", "id": next_id, "method": "tools/call",
                  "params": {"name": "codex", "arguments": args}})
        else:
            send({"jsonrpc": "2.0", "id": next_id, "method": "tools/call",
                  "params": {"name": "codex-reply",
                             "arguments": {"threadId": thread_id, "prompt": prompt}}})
        deadline = time.time() + 90
        while time.time() < deadline:
            time.sleep(0.5)
            if next_id in pending_responses:
                resp = pending_responses[next_id]
                if "result" in resp:
                    sc = resp["result"].get("structuredContent", {})
                    if sc.get("threadId"):
                        thread_id = sc["threadId"]
                break

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    LOG.write_text("\n".join(events) + "\n")
    print(f"\nTotal elicitations seen: {elicitation_count}")
    print(f"Full log: {LOG}")


if __name__ == "__main__":
    main()
