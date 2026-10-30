"""The release's entry for the voice relay (claude-hub-voice): voice/run.py ARGS = `voice-relay ARGS` from the
release's own copy (voice/app) and its pinned wheels (voice/vendor). Run with `python3 -I -B` (voiceloader).
voice/run.py tg ARGS = `voice-tg ARGS` (the call account's login, status, harden) from the same copy: the owner's
relogin paste (`hub kit paste voice-relogin`) runs it through `docker exec`, with the container paths in VOICE_TG_*."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "app"), os.path.join(HERE, "vendor")]

if __name__ == "__main__":
    if sys.argv[1:2] == ["tg"]:
        from voice_relay import userbot

        userbot.ENTRY = [os.path.abspath(__file__), "tg"]  # its login waiter starts through this file too
        sys.exit(userbot.main(sys.argv[2:]))
    from voice_relay.server import main

    sys.exit(main(sys.argv[1:]))
