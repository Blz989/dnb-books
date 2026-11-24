"""Stand-in for the rclone binary in tests. Remote "fake:<path>" maps to $FAKE_RCLONE_ROOT/<path>.

Supports: copy <file> <remote-dir>, copyto <remote-file> <local-file>, deletefile <remote-file>.
Flags (any "--" argument) are ignored, except that --use-json-log makes output look like real rclone's
JSON log: a {"stats": ...} line per chunk, debug "pacer" lines for rate-limit retries, and a plain
"Failed to copy" line at the end on failure.

Environment:
  FAKE_RCLONE_FAIL      make every call fail with that message
  FAKE_RCLONE_SLOW      seconds to sleep between copy chunks (four chunks per file)
  FAKE_RCLONE_RETRIES   number of rate-limit retries to log during a copy
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path


def local(remote: str) -> Path:
    assert remote.startswith("fake:"), remote
    return Path(os.environ["FAKE_RCLONE_ROOT"]) / remote[len("fake:"):]


def log(level, msg, **extra):
    print(json.dumps({"level": level, "msg": msg, "source": "fake", **extra}), file=sys.stderr, flush=True)


def stats(done, total, elapsed, **extra):
    speed = done / elapsed if elapsed else 0
    eta = int((total - done) / speed) if speed else None
    log("warning", "\nTransferred: ...\n", stats={
        "bytes": done, "totalBytes": total, "speed": speed, "eta": eta, "errors": 0, "retryError": False,
        "elapsedTime": elapsed, "transfers": int(done == total), "totalTransfers": 1, **extra})


def copy_with_progress(src: Path, dst: Path, json_log: bool):
    total = src.stat().st_size
    slow = float(os.environ.get("FAKE_RCLONE_SLOW", "0"))
    retries = int(os.environ.get("FAKE_RCLONE_RETRIES", "0"))
    start = time.monotonic()
    for i in range(1, 5):
        if slow:
            time.sleep(slow)
        if json_log:
            for r in range(retries if i == 2 else 0):
                log("debug", f"pacer: low level retry {r + 1}/10 (error googleapi: Error 403: Rate Limit Exceeded, rateLimitExceeded)")
                log("debug", "pacer: Rate limited, increasing sleep to 2s")
            stats(total * i // 4, total, max(time.monotonic() - start, 0.001))
    shutil.copy(src, dst / src.name)


def main(argv):
    json_log = "--use-json-log" in argv
    args = [a for a in argv if not a.startswith("--")]
    if os.environ.get("FAKE_RCLONE_FAIL"):
        if json_log:
            log("error", f"Attempt 3/3 failed with 1 errors and: {os.environ['FAKE_RCLONE_FAIL']}")
            print(f"2026/10/27 01:15:00 {os.environ['FAKE_RCLONE_FAIL']}", file=sys.stderr)
        else:
            print(f"2026/10/27 01:15:00 ERROR : {os.environ['FAKE_RCLONE_FAIL']}", file=sys.stderr)
        return 1
    command, *args = args
    if command == "copy":
        src, dst = Path(args[0]), local(args[1])
        dst.mkdir(parents=True, exist_ok=True)
        copy_with_progress(src, dst, json_log)
    elif command == "copyto":
        src = local(args[0])
        if not src.exists():
            print(f"2026/10/27 02:15:00 ERROR : {src.name}: object not found", file=sys.stderr)
            return 3
        shutil.copy(src, args[1])
    elif command == "deletefile":
        local(args[0]).unlink(missing_ok=True)
    else:
        print(f"unsupported: {command}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
