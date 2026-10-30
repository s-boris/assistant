#!/bin/bash
# launch.sh CASE_DIR TZ SECS -- CMD [ARGS...]: start one eval worker as the unprivileged eval user (via sudo -u).
# stdin line 1 = the model API access token (never on argv or disk), the rest = the worker's stdin (the prompt).
# The environment is rebuilt from scratch: sandbox HOME/CLAUDE_CONFIG_DIR, PATH with the stub bin first,
# the network guard preloaded, no session/bridge/messaging variables from the caller. CMD runs under timeout.
set -u
D=${1:?case dir}; Z=${2:-UTC}; T=${3:-300}; shift 3; [ "${1:-}" = -- ] && shift
H="$D/home"
[ -d "$H" ] || { echo "launch.sh: no sandbox home $H" >&2; exit 97; }
# the guard library: EVALS_NETGUARD_SO when it survives (no sudo: tests), else next to the installed lib
ng_so=${EVALS_NETGUARD_SO:-$(dirname "$(readlink -f "$0")")/../../../.local/share/evals/netguard-eval.so}
export -n ng_so 2>/dev/null
IFS= read -r tok || tok=
for v in $(compgen -e); do unset "$v" 2>/dev/null; done
export HOME="$H" USER=evalbot LOGNAME=evalbot SHELL=/bin/bash LANG=C.UTF-8 LC_ALL=C.UTF-8 TERM=dumb TZ="$Z"
export PATH="$H/bin:/usr/local/bin:/usr/bin:/bin"
export CLAUDE_CONFIG_DIR="$H/.claude" TMPDIR="$H/tmp"
export LD_PRELOAD="$ng_so"
unset ng_so
[ -r "$LD_PRELOAD" ] || { echo "launch.sh: network guard missing ($LD_PRELOAD)" >&2; exit 98; }
export NETGUARD_LOG="$H/.eval/net.log"
export DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1 DISABLE_ERROR_REPORTING=1 DISABLE_BUG_COMMAND=1
export ENABLE_CLAUDEAI_MCP_SERVERS=false USAGE=off EVALS_SANDBOX=1
[ -n "$tok" ] && export CLAUDE_CODE_OAUTH_TOKEN="$tok"
unset tok
mkdir -p "$TMPDIR"
cd "$H/workspace" 2>/dev/null || cd "$H" || exit 97
umask 022
exec timeout -k 15 "$T" "$@"
