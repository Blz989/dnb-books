"""Stand-in for the rclone binary in tests. Remote "fake:<path>" maps to $FAKE_RCLONE_ROOT/<path>.

Supports: copy <file> <remote-dir>, copyto <remote-file> <local-file>, deletefile <remote-file>.
Set FAKE_RCLONE_FAIL to make every call fail with that message on stderr.
"""
import os
import shutil
import sys
from pathlib import Path


def local(remote: str) -> Path:
    assert remote.startswith("fake:"), remote
    return Path(os.environ["FAKE_RCLONE_ROOT"]) / remote[len("fake:"):]


def main(argv):
    if os.environ.get("FAKE_RCLONE_FAIL"):
        print(f"2026/10/27 01:15:00 ERROR : {os.environ['FAKE_RCLONE_FAIL']}", file=sys.stderr)
        return 1
    command, *args = argv
    if command == "copy":
        src, dst = args[0], local(args[1])
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst / Path(src).name)
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
