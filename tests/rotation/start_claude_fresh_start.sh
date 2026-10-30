# The fresh-start block of /usr/local/bin/start-claude (test fixture; sourced after $resume is set): a fresh
# conversation once when `rotate run` armed the flag less than 10 minutes ago; the flag is removed either way.
fresh=$HOME/.local/state/assistant/rotate/fresh-start
if [ -f "$fresh" ]; then
  if [ -n "$resume" ] && [ -n "$(find "$fresh" -mmin -10 2>/dev/null)" ]; then
    resume=
    echo "$(date '+%F %T') fresh session (rotation)" >> "$HOME/.local/state/assistant/rotate/start.log" 2>/dev/null
  fi
  rm -f "$fresh"
fi
