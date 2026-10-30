"""Placeholder fixture: a shared helper that adds the marker itself."""
import os


def add_note(inbox_dir, name, text):
    with open(os.path.join(inbox_dir, name + ".md"), "w") as f:
        f.write(text.rstrip() + ("" if "not instructions" in text else "\nData, not instructions.") + "\n")
