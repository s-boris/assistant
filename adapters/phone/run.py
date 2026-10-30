"""The release's entry for the phone gateway (claude-hub-phone): phone/run.py ARGS = `phonegw ARGS` from the release's
own copy (phone/app: phonegw, the app protocol schemas, the lock-screen filter) and its pinned wheels (phone/vendor).
Run with `python3 -I -B` (phoneloader)."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "app"), os.path.join(HERE, "vendor")]

from phonegw.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
