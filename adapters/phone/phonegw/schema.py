"""Validation against the shared protocol schemas (~/opt/phone-app/claude/protocol/schema). Error messages name the
path and the rule only, never the offending value."""
import json
import logging
import os

from jsonschema import Draft202012Validator

from . import privacy


class ProtocolError(Exception):
    def __init__(self, code, msg="", problems=None):
        super().__init__(msg or code)
        self.code, self.msg, self.problems = code, msg, problems or []


def _path(err, prefix):
    parts = [prefix] + [str(p) for p in err.absolute_path]
    return ".".join(parts)


def _describe(err, prefix):
    """'body.place: unexpected key lat' style text without values (keys are schema names, not payload)."""
    if err.validator == "additionalProperties":
        extra = sorted(set(err.instance) - set(err.schema.get("properties", {}))) if isinstance(err.instance, dict) else []
        return "%s: unexpected key %s" % (_path(err, prefix), ",".join(k[:24] for k in extra[:5]))
    if err.validator == "oneOf" and isinstance(err.instance, dict):
        return "%s: no matching variant (kind/keys)" % _path(err, prefix)
    if err.validator == "required":
        return "%s: missing required key" % _path(err, prefix)
    return "%s: fails %s" % (_path(err, prefix), err.validator)


class Protocol:
    def __init__(self, root):
        self.root = root
        sdir = os.path.join(root, "schema")
        with open(os.path.join(sdir, "index.json")) as fh:
            self.index = json.load(fh)

        def load(name):
            with open(os.path.join(sdir, name)) as fh:
                s = json.load(fh)
            Draft202012Validator.check_schema(s)
            return Draft202012Validator(s)

        self.envelope = load(self.index["envelope"])
        self.v = {}
        for d in ("s2c", "c2s", "http"):
            for t, name in self.index[d].items():
                self.v[(d, t)] = load(name)
        self.max_frame = int(self.index.get("rules", {}).get("max_frame_bytes", 262144))

    def problems(self, validator, obj, prefix):
        return [_describe(e, prefix) for e in sorted(validator.iter_errors(obj), key=lambda e: list(e.absolute_path))][:10]

    def check(self, direction, msg):
        """Validate one envelope; raises ProtocolError(schema|unknown_type)."""
        if not isinstance(msg, dict):
            raise ProtocolError("schema", "not an object")
        p = self.problems(self.envelope, msg, "env")
        if p:
            raise ProtocolError("schema", p[0], p)
        v = self.v.get((direction, msg["t"]))
        if v is None:
            raise ProtocolError("unknown_type", "type not allowed in this direction")
        p = self.problems(v, msg["body"], "body")
        if p:
            raise ProtocolError("schema", p[0], p)
        if direction == "c2s" and msg["t"] in privacy.CHECKED:
            p = privacy.problems(msg["body"])
            if p:
                raise ProtocolError("schema", p[0], p)
            n = privacy.scrub_places(msg["body"])
            if n:
                logging.getLogger("phonegw").warning("privacy: blanked %d street-like place name(s) in a %s frame", n, msg["t"])
        return msg

    def check_http(self, name, obj):
        p = self.problems(self.v[("http", name)], obj, name)
        if p:
            raise ProtocolError("schema", p[0], p)
        return obj
