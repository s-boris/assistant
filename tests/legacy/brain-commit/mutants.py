MUTATIONS = [
    ("sync even when brain fails", 'if [ "$rc" = 0 ] && [ -z', "if [ -z"),
    ("BRAIN_GIT_DIR ignored", '[ -z "${BRAIN_GIT_DIR:-}${BRAIN_WORK_TREE:-}" ]', '[ -z "${BRAIN_WORK_TREE:-}" ]'),
    ("BRAIN_WORK_TREE ignored", '[ -z "${BRAIN_GIT_DIR:-}${BRAIN_WORK_TREE:-}" ]', '[ -z "${BRAIN_GIT_DIR:-}" ]'),
    ("no -x check", ' && [ -x "$d/codebase-sync" ]; then', "; then"),
    ("sync output shown", '"$d/codebase-sync" --bg >/dev/null 2>&1 || true', '"$d/codebase-sync" --bg || true'),
    ("sync without --bg", '"$d/codebase-sync" --bg >/dev/null', '"$d/codebase-sync" >/dev/null'),
    ("exit 0 always", 'exit "$rc"', "exit 0"),
    (
        "exit = sync's rc",
        '"$d/codebase-sync" --bg >/dev/null 2>&1 || true\nfi\nexit "$rc"',
        '"$d/codebase-sync" --bg >/dev/null 2>&1; rc=$?\nfi\nexit "$rc"',
    ),
    ("no readlink -f", 'd=$(dirname "$(readlink -f "$0")")', 'd=$(dirname "$0")'),
    ("args joined", '"$d/brain" commit "$@"', '"$d/brain" commit "$*"'),
    ("brain without commit verb", '"$d/brain" commit "$@"; rc', '"$d/brain" "$@"; rc'),
]
