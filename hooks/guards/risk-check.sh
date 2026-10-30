#!/bin/sh
# PreToolUse hook on Bash (settings.json, next to vault-guard): the risk check (Hermes idea 2).
# Rules, LLM fallback and the risk-ok CLI: risk_check.py next to this file; tests: test_risk_check.py.
# FAILS OPEN (unlike vault-guard): a safety net, not a gate. This wrapper always exits 0. A crash, a missing script
# or a hang past 12 s allows the call and leaves an ERROR line in ~/.local/state/risk-check.log.
# settings.json sets "onFailure": "block" on this hook: if this wrapper itself can't start, is killed or passes
# the 15 s hook timeout, Claude Code blocks the Bash call. Keep the wrapper exiting 0 (or 2) on every path.
# Fast path: a payload without any trigger word (most commands) is allowed by one grep, without starting python.
# The grep must stay a superset of risk_check.py's PREFILTER, on JSON-escaped text (hence the \\[bfnrt] boundary).
# The reduced rule set risk_check_h11.py runs instead of risk_check.py only while the switch file
# risk-check.h11 next to this wrapper holds the line "reduced-v1" (and the reduced script is readable); absent,
# unreadable or any other content: the full rule set. Off: delete the switch file (instant, no restart).
# The switch sits next to the hooks on purpose: whoever can write it can already edit the hook itself.
in=$(cat) || exit 0
printf '%s' "$in" | grep -qE '(^|[^[:alnum:]_.-]|\\[bfnrt])(unraid-do|unraid-ssh|ssh|scp|rsync|sftp|rm|unlink|find|shred|truncate|crontab|curl|wget|docker|docker-compose|tg|gh|git|vault|proxy|risk-ok|mail|mailx|sendmail|msmtp|mutt|swaks|mkfs|mke2fs|mkswap|wipefs|parted|fdisk|cfdisk|sgdisk|gdisk|sfdisk|blkdiscard|dd|zfs|zpool|btrfs|xfs_repair|cryptsetup|hdparm)([^[:alnum:]_-]|$)|\.secrets|\.env|/dev/(sd|nvme|md|hd|vd|xvd|mapper|disk|loop|dm-|mmcblk|nbd)' || exit 0
py=risk_check.py
sw=${RISK_CHECK_H11_SWITCH:-${0%/*}/risk-check.h11}
if [ -r "$sw" ] && [ -r "${0%/*}/risk_check_h11.py" ]; then
  v=; read -r v <"$sw" 2>/dev/null
  [ "$v" = reduced-v1 ] && py=risk_check_h11.py
fi
out=$(printf '%s' "$in" | timeout -k 1 "${RISK_CHECK_TIMEOUT:-12}" /usr/bin/python3 -I "${0%/*}/$py" 2>/dev/null)
rc=$?
if [ "$rc" -eq 0 ]; then
  [ -n "$out" ] && printf '%s\n' "$out"
else
  printf '%s ERROR wrapper: %s exit %s; call allowed (fail open)\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$py" "$rc" \
    >>"${RISK_CHECK_LOG:-$HOME/.local/state/risk-check.log}" 2>/dev/null
fi
exit 0
