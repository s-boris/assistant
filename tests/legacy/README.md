# Tool suites in shell form

**All suites become native pytest; the shell form is temporary.** No new suite goes here: a new tool gets a
pytest file under `tests/`.

Each `tests/legacy/<suite>/` is the old test suite of one managed tool (`run.sh`, sometimes unittest or mjs files
it calls). They are pytest tests now: `tests/test_suites.py` makes one test per manifest suite (id = the suite's
name) and runs it through the CI driver's suite runner (`ops/assistant.py` `_ci_legacy`: the tool's copy staged in
its `*_BIN` seam, the tree's lib modules, a temp HOME and TMPDIR, the network guard, 240 s each, side by side).
`assistant ci` picks the suites a change impacts (`legacy_select`, extras in `impact.toml`) and names them in
`ASSISTANT_CI_SUITES`; one pytest step, one report. By hand:
`ASSISTANT_CI_SUITES=kk,recall .venv-3.13/bin/python -m pytest tests/test_suites.py`.

Every CI run prints how many are left (`N still in shell form`); land rows carry it as `shell_suites`.

## Converting one

1. Write `tests/<area>/test_<tool>.py`: pytest, `tmp_path` HOME, the tool's repo copy (or its `*_BIN` seam), no
   network, no real home. Cover what `run.sh` checks; drop cases that only test shell plumbing.
2. Remove `suite = "<name>"` (and `seam`) from the tool's entry in `ops/deploy/manifest.toml`.
3. Delete `tests/legacy/<name>/` and its lines in `impact.toml`, `slow.txt`, `../runner-skip.txt` (a host-bound
   suite's line becomes the new file's line) and pyproject's ruff excludes.
4. `assistant land`: the count drops by one.

## Order

Value first (how often the tool and its suite changed lately: those are rerun and read most), then size. Next up:
assistant, evals, flowd, remind, codebase-sync, plans-watch, container-watch, sleepin, media (the slowest, 97 s),
usage, agent-watch, watch, browser, botlib, gym, tg-cmd, unraid-health, tg-status, self-review, board-lint, deals,
event-card; then the rest by size. The suites `~/bin/test-all` runs on the live setup without a manifest entry
(codebase-precheck, dash, docs-lint, dst, hooks, inbox-lint, phonegw, voice-mode) convert the same way.

The internal names (`tests/legacy`, `legacy_select`, `assistant legacy-main`, `legacy-main.json`,
`ASSISTANT_CI_LEGACY_*`) go with the last suite; renaming them earlier needs a hub release in the same step (the
hub runner's CI driver reads `tests/legacy`).
