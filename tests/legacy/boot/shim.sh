#!/bin/sh
# Logging stand-in for every command boot.sh may run that touches the system, the network or my session.
# Behaviour per name (symlink name = $0): BT_MODE=fresh|warm, BT_NET=0 (no network), BT_APT_FAIL=1, BT_BG_SLEEP=N.
n=${0##*/}
printf '%s\n' "$n $*" >> "$BT_LOG"
inside() { case $1 in "$BT_ROOT"/*) return 0;; esac; echo "OUTSIDE $n $1" >> "$BT_VIOL"; return 1; }
case $n in
  sudo)  # runs nothing; apt-get through sudo fails when apt or the network is down; tee swallows its input
    case " $* " in *" apt-get "*)
      [ "${BT_APT_FAIL:-0}" = 1 ] && exit 100
      [ "${BT_NET:-1}" = 0 ] && exit 100;; esac
    case " $* " in *" tee "*) cat >/dev/null;; esac
    exit 0;;
  apt-get|apt|pip|pip3|npm|npx|locale-gen) exit 0;;
  dpkg)
    [ "$1" = --print-architecture ] && { echo amd64; exit 0; }
    [ "$BT_MODE" = warm ]; exit $?;;
  locale)
    echo C.utf8; [ "$BT_MODE" = warm ] && echo de_CH.utf8; exit 0;;
  id)  # users boot.sh creates (evalbot) exist only on a warm boot; never the host's real user table
    [ "$BT_MODE" = warm ]; exit $?;;
  curl)
    [ "${BT_NET:-1}" = 0 ] && exit 6
    out=; prev=; for a; do [ "$prev" = -o ] && out=$a; prev=$a; done
    if [ -n "$out" ]; then inside "$out" && echo fake-deb > "$out"; else echo fake-key; fi; exit 0;;
  python|python3)
    case " $* " in *" -m venv "*)
      echo "start venv" >> "$BT_BG"; sleep "${BT_BG_SLEEP:-0}"
      d=; for a; do d=$a; done
      inside "$d" && mkdir -p "$d/bin" && ln -sf "$BT_SHIMBIN/pip" "$d/bin/pip"
      echo "end venv" >> "$BT_BG";; esac
    exit 0;;
  restart-cause|whisper-server-ctl|browser|flowd)  # the ~/bin helpers boot.sh starts in the background
    echo "start $n" >> "$BT_BG"; sleep "${BT_BG_SLEEP:-0}"; echo "end $n" >> "$BT_BG"; exit 0;;
  claude|restart|crontab|tmux|tg|systemctl|service|docker|ssh-keygen)
    echo "FORBIDDEN $n $*" >> "$BT_VIOL"; exit 1;;
  *) exit 0;;
esac
