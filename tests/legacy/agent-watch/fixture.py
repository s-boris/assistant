#!/usr/local/bin/python3.13 -I
"""Synthetic subagent transcripts for agent-watch tests. Every message text carries SECRET_MARK so the tests can
prove that no transcript content ever reaches output, notes, texts or state.

  fixture.py agent PROJECT SESSION ID KIND MTIME [--start T] [--desc D] [--parent P] [--partial] [--big N]
                                                 [--trailing-attachment] [--no-meta] [--bash-cmd CMD]
        KIND: tool_use | tool_result | thinking | final | interrupted ; MTIME/T: epoch seconds
        tool_use is a Read call (content hidden) unless --bash-cmd makes it a Bash call (with an id) running CMD
  fixture.py grow PROJECT SESSION ID KIND MTIME   append one record of KIND and set the mtime
  fixture.py notif PROJECT SESSION ID STATUS TS [--delivered]   parent-session task notification
  fixture.py filler PROJECT N MTIME               N old finished agents in session "old"
"""

import json
import os
import sys
from datetime import UTC, datetime

SECRET_MARK = "SECRET-CONTENT-XYZZY"


def iso(t):
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def rec(kind, aid, t, big=0, cmd=None):
    base = {"agentId": aid, "isSidechain": True, "sessionId": "S", "timestamp": iso(t), "uuid": f"u{t}"}
    pad = ("x" * big) if big else ""
    if kind == "prompt":
        return {**base, "type": "user", "message": {"role": "user", "content": f"do the task {SECRET_MARK}"}}
    if kind == "tool_use":
        # default: a non-Bash call (content never shown); cmd given: a Bash call with that command (permission-stuck
        # notes show its first 80 chars, so tests pass commands without SECRET_MARK)
        block = (
            {"type": "tool_use", "id": f"toolu_{int(t)}", "name": "Bash", "input": {"command": cmd}}
            if cmd is not None
            else {"type": "tool_use", "name": "Read", "input": {"file_path": f"/x/{SECRET_MARK}{pad}"}}
        )
        return {
            **base,
            "type": "assistant",
            "message": {"role": "assistant", "stop_reason": "tool_use", "content": [block]},
        }
    if kind == "tool_result":
        return {
            **base,
            "type": "user",
            "message": {"role": "user", "content": [{"type": "tool_result", "content": f"{SECRET_MARK} output {pad}"}]},
            "toolUseResult": {"stdout": SECRET_MARK},
        }
    if kind == "thinking":
        return {
            **base,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "stop_reason": None,
                "content": [{"type": "thinking", "thinking": f"{SECRET_MARK}{pad}"}],
            },
        }
    if kind == "final":
        return {
            **base,
            "type": "assistant",
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": f"Report: {SECRET_MARK}{pad}"}],
            },
        }
    if kind == "interrupted":
        return {
            **base,
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}],
            },
        }
    if kind == "attachment":
        return {**base, "type": "attachment", "attachment": {"type": "total_tokens_reminder", "x": SECRET_MARK}}
    raise SystemExit(f"unknown kind {kind}")


def paths(project, session, aid):
    d = os.path.join(project, session, "subagents")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"agent-{aid}.jsonl"), os.path.join(d, f"agent-{aid}.meta.json")


def opt(args, name, default=None, flag=False):
    if name not in args:
        return default
    if flag:
        return True
    return args[args.index(name) + 1]


def main(a):
    cmd = a[0]
    if cmd == "agent":
        project, session, aid, kind, mtime = a[1], a[2], a[3], a[4], float(a[5])
        start = float(opt(a, "--start", mtime - 300))
        jp, mp = paths(project, session, aid)
        recs = [rec("prompt", aid, start), rec("tool_use", aid, start + 5), rec("tool_result", aid, start + 6)]
        last = rec(kind, aid, mtime, int(opt(a, "--big", 0)), opt(a, "--bash-cmd"))
        if opt(a, "--trailing-attachment", flag=True):
            recs += [last, rec("attachment", aid, mtime)]
        else:
            recs.append(last)
        with open(jp, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
            if opt(a, "--partial", flag=True):
                f.write(json.dumps(rec("final", aid, mtime))[:40])  # a record still being written
        if not opt(a, "--no-meta", flag=True):
            meta = {
                "agentType": "general-purpose",
                "description": opt(a, "--desc", f"Task {aid[:6]}"),
                "spawnDepth": 1,
                "requestShape": "background",
            }
            if opt(a, "--parent"):
                meta.update(parentAgentId=opt(a, "--parent"), spawnDepth=2)
            with open(mp, "w") as f:
                json.dump(meta, f)
            os.utime(mp, (start, start))
        os.utime(jp, (mtime, mtime))
    elif cmd == "grow":
        project, session, aid, kind, mtime = a[1], a[2], a[3], a[4], float(a[5])
        jp, _ = paths(project, session, aid)
        with open(jp, "a") as f:
            f.write(json.dumps(rec(kind, aid, mtime)) + "\n")
        os.utime(jp, (mtime, mtime))
    elif cmd == "notif":
        project, session, aid, status, ts = a[1], a[2], a[3], a[4], float(a[5])
        os.makedirs(project, exist_ok=True)
        content = (
            f"<task-notification>\n<task-id>{aid}</task-id>\n<tool-use-id>toolu_x</tool-use-id>\n"
            f"<output-file>/tmp/x</output-file>\n<status>{status}</status>\n"
            f'<summary>Agent "x" {status} {SECRET_MARK}</summary>\n</task-notification>'
        )
        with open(os.path.join(project, session + ".jsonl"), "a") as f:
            if opt(a, "--delivered", flag=True):
                f.write(
                    json.dumps({"type": "user", "timestamp": iso(ts), "message": {"role": "user", "content": content}})
                    + "\n"
                )
            else:
                f.write(
                    json.dumps(
                        {
                            "type": "queue-operation",
                            "operation": "enqueue",
                            "timestamp": iso(ts),
                            "sessionId": session,
                            "content": content,
                        }
                    )
                    + "\n"
                )
                f.write(
                    json.dumps(
                        {
                            "type": "queue-operation",
                            "operation": "remove",
                            "timestamp": iso(ts + 1),
                            "sessionId": session,
                            "content": content,
                        }
                    )
                    + "\n"
                )
    elif cmd == "filler":
        project, n, mtime = a[1], int(a[2]), float(a[3])
        for i in range(n):
            aid = f"f{i:016x}"
            jp, mp = paths(project, "old", aid)
            with open(jp, "w") as f:
                f.write(json.dumps(rec("prompt", aid, mtime)) + "\n" + json.dumps(rec("final", aid, mtime)) + "\n")
            with open(mp, "w") as f:
                json.dump({"description": f"old {i}"}, f)
            os.utime(jp, (mtime, mtime))
            os.utime(mp, (mtime, mtime))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
