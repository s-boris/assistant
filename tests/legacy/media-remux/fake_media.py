"""Stand-in for ~/bin/media in the media-remux tests: GETs from $FAKE_DIR/routes.json (paths logged to gets.log),
writes logged to posts.jsonl."""

import json
import os
import urllib.parse

ZRH = None  # media-remux falls back to UTC: MEDIA_REMUX_NOW is read as UTC in the tests


class ApiError(Exception):
    pass


def _dir():
    return os.environ["FAKE_DIR"]


def api(svc, method, path, body=None, timeout=None):
    if method != "GET":
        with open(os.path.join(_dir(), "posts.jsonl"), "a") as f:
            f.write(json.dumps({"svc": svc, "method": method, "path": path, "body": body}) + "\n")
        return {}
    with open(os.path.join(_dir(), "gets.log"), "a") as f:
        f.write(path + "\n")
    with open(os.path.join(_dir(), "routes.json")) as f:
        routes = json.load(f)
    for key in (path, path.split("?")[0]):
        if key in routes:
            return routes[key]
    raise ApiError(f"{svc}: no route for GET {path}")


def get(svc, path, timeout=None):
    return api(svc, "GET", path, timeout=timeout)


def q(s):
    return urllib.parse.quote(str(s), safe="")
