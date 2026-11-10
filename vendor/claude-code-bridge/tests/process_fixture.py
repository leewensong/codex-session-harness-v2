"""Hermetic subprocess fixture: never invokes Claude or any model."""
import json
import sys
import time

mode, sid, *args = sys.argv[1:]
sys.stdin.buffer.read()
if mode == "slow":
    time.sleep(30)
elif mode == "flood":
    sys.stderr.write("private-tool-log" * 100000)
elif mode == "invalid":
    print("not-json")
    sys.exit(0)
elif mode == "oversized":
    sys.stdout.write("x" * (4 * 1024 * 1024 + 10) + "\n")
    sys.exit(0)
if "stream-json" in args:
    print(json.dumps({"type": "stream_event", "session_id": sid, "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "DO_NOT_DISPLAY"}}}), flush=True)
    print(json.dumps({"type": "stream_event", "session_id": sid, "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}}), flush=True)
print(json.dumps({"type": "result", "session_id": sid, "subtype": "success", "result": "hello", "permission_denials": []}))
