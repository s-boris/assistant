"""Stand-in for ~/bin/arr-unblock in the media-remux tests (verify_radarr + import_now)."""

import json
import os


class _Dec:
    def __init__(self, problems):
        self.problems, self.files = problems, [{"path": "/x.mkv"}]


def verify_radarr(rec, movie, preview, parse):
    refuse = os.environ.get("FAKE_UNBLOCK_REFUSE")
    return _Dec(["the file name parses to another movie"] if refuse else [])


def import_now(dec):
    with open(os.path.join(os.environ["FAKE_DIR"], "posts.jsonl"), "a") as f:
        f.write(json.dumps({"method": "POST", "path": "/api/v3/command", "body": {"name": "ManualImport"}}) + "\n")
    return "ok", "imported"
