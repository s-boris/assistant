"""Placeholder fixture: a shared helper that writes the caller's text verbatim (allowlisted in the fixture list)."""
import os


def put_note(inbox_dir, name, text):
    with open(os.path.join(inbox_dir, name + ".md"), "w") as f:
        f.write(text)
