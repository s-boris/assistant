"""hub: code that runs inside the claude-hub stack, never in ~/bin.

  loader.py    the release loader (hub + ci containers). The owner's install paste installs it read-only into the
               hub's appdata; the brain can't change it (a new loader = a paste).
  hubd.py      the hub's API process (hubd's docstring lists its routes).
  ciworker.py  the ci container's process (the CI runner).
  hubfeed.py   the read-only status feed: parts registry + states, gateway, approvals, tg, traces; SSE listener.

`assistant deploy --target hub` copies hubd.py and ciworker.py into a release (~/.local/share/claude-hub/releases/<id>,
release.json with a sha256 per file) and renders the kit (compose, .env, loader, pastes) from a git archive of main.
Each file is self-contained (stdlib only): the containers run plain python:3.13-slim.
"""
