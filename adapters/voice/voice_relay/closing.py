"""Closing a call well: what he says when he is done, the "anything else?" question, and why a call ended.

classify(text) -> None | "thanks" | "bye" | "no": his utterance is ONLY closing words (EN / DE / Swiss German, at most
  MAX_WORDS words; any other word -> None, so "thanks, and turn off the lights" is a request, not a closing).
  thanks = thanks / done / ok / passt / merci / danke / super ...; bye = a goodbye or "that's all";
  no = "no" / "nothing else" / "nein danke" (a goodbye only after an "anything else?").
line_class(text) -> thanks | bye | no | other | none: one of his call-log lines as a closing class (the call log's
  him{closing}; other = words that aren't only closing words, none = no words at all). A class, never his text.
asks_more(text) -> True when the assistant's turn ENDS with an "anything else?" question (last TAIL chars).
says_bye(text)  -> True when the assistant's turn ends with a goodbye.
end_why(...)    -> the end reason class for the usage event (ids/flags only; no text ever leaves this module).
The session (session.py) owns the timers; this module only classifies and words the [call] prompts."""
import hashlib
import importlib.machinery
import importlib.util
import os
import re

MAX_WORDS = 10
TAIL = 90

THANKS = ("thank you very much", "thank you so much", "thanks a lot", "thanks so much", "thanks very much",
          "many thanks", "thank you", "thanks", "thank", "thx", "cheers", "ta",
          "danke vielmal", "danke viel mal", "merci vielmal", "merci viel mal", "vielen dank", "danke schön",
          "dankeschön", "danke schoen", "danke sehr", "besten dank", "merci beaucoup", "merci", "danke", "dankä",
          "dank dir", "danke dir", "that's done", "that is done", "done", "erledigt", "fertig",
          "ok", "okay", "o k", "okey", "oki", "alright", "all right", "great", "perfect", "perfekt", "super", "cool",
          "nice", "awesome", "excellent", "brilliant", "lovely", "good", "very good", "sehr gut", "passt", "passt so",
          "passt scho", "passt schon", "das passt", "alles klar", "klar", "top", "tiptop", "wunderbar", "toll",
          "genial", "prima", "mega", "bestens", "sounds good", "got it", "understood", "verstanden", "schön", "guet",
          "gut", "läuft", "much appreciated", "appreciate it")
BYE = ("good bye", "goodbye", "bye bye", "bye", "see you later", "see you", "see ya", "talk to you later", "talk later",
       "speak soon", "later", "tschüss", "tschüs", "tschuess", "tschau", "ciao", "adieu", "ade", "uf widerluege",
       "uf wiederluege", "auf wiederhören", "auf wiedersehen", "bis später", "bis spöter", "bis dann", "bis bald",
       "machs gut", "mach's gut", "that's all", "that is all", "that was all", "that's it", "that is it",
       "that's everything", "das wär's", "das wärs", "das war's", "das wars", "das isch alles", "das ist alles",
       "das war alles", "das wär alles", "i'm done", "we're done", "all done", "hang up", "end the call",
       "leg auf", "du chasch uflegge", "du kannst auflegen")
NO = ("no thank you", "no thanks", "nein danke", "nei danke", "nothing else", "nothing more", "nothing", "nope", "nah",
      "no", "nein", "nei", "nö", "nöö", "ne", "nichts mehr", "nichts weiter", "sonst nichts", "nichts", "nüt meh",
      "nüt", "süsch nüt", "suscht nüt", "all good", "i'm good", "i'm fine", "i am good", "that's fine", "not now",
      "im moment nicht", "grad nöd", "nöd")
FILLER = ("and", "so", "well", "oh", "ah", "ahh", "hmm", "hm", "mhm", "um", "uh", "eh", "very", "much", "really",
          "also", "just", "then", "dann", "mal", "vielmal", "sehr", "you", "that's", "that", "it", "is", "was", "all",
          "yes", "yeah", "yep", "jo", "ja", "jep", "jawohl", "de", "denn", "für", "for", "the", "help", "your",
          "dir", "ihnen", "a", "lot", "now", "jetzt", "es", "das", "isch", "ist", "alles", "everything", "fine",
          "too", "auch", "au", "buddy", "mate", "hey", "claude", "gemini", "assistant")


def _norm(text):
    t = str(text or "").lower().replace("’", "'").replace("`", "'")
    t = re.sub(r"[^\w' ]+", " ", t, flags=re.UNICODE)
    return " " + " ".join(t.split()) + " "


def _strip(t, phrases):
    """Remove whole-word occurrences of phrases (longest first); -> (rest, found)."""
    found = False
    for p in sorted(phrases, key=len, reverse=True):
        pat = " " + p + " "
        while pat in t:
            t = t.replace(pat, " ", 1)
            found = True
    return t, found


def classify(text):
    t = _norm(text)
    words = t.split()
    if not words or len(words) > MAX_WORDS:
        return None
    t, bye = _strip(t, BYE)
    t, no = _strip(t, NO)
    t, thanks = _strip(t, THANKS)
    t, _ = _strip(t, FILLER)
    if t.strip():
        return None                      # another word: a request or an answer, not a closing
    return "bye" if bye else "no" if no else "thanks" if thanks else None


def line_class(text):
    if not _norm(text).split():
        return "none"
    return classify(text) or "other"


MORE = re.compile(
    r"(anything else|something else|anything more|anything further|what else|any(thing)? other|"
    r"else i can|more i can|else (can|could) i|"
    r"noch (et)?was|sonst noch|noch etwas|noch ein(en|e)? (wunsch|frage|anliegen)|kann ich (sonst|noch)|"
    r"no(ch)? (öppis|öpis|epis|ebbis|äppis)|süsch|suscht|sust no|no was|weiter(es)? helfen)[^.!]{0,30}\??\s*$")
SAYS_BYE = re.compile(r"(good ?bye|bye|tschüss|tschüs|tschau|ciao|adieu|uf wider ?luege|auf wieder(hören|sehen)|"
                      r"bis (bald|später|spöter|dann)|have a (good|great|nice) [a-z]+|"
                      r"schön(en|e)? (abend|tag|wochenende|morge)|en schöne[a-z]*( [a-z]+)?|machs? gut)"
                      r"[^.!?]{0,20}[.!?]*\s*$")


def _tail(text):
    t = " ".join(str(text or "").lower().replace("’", "'").split())
    return t[-TAIL:]


def asks_more(text):
    return bool(MORE.search(_tail(text)))


def says_bye(text):
    return bool(SAYS_BYE.search(_tail(text)))


# --- [call] prompts (the model treats "[call]" lines as coming from the call itself; voice-agent.md) ---------------

CLOSING_ASK = ("[call] He thanked you or said it's done, and you didn't answer. If nothing is open, reply in two or "
               "three words and ask once, briefly and in his language, whether there is anything else (\"Anything "
               "else?\" / \"Süsch no öppis?\"). If he was answering a question of yours, carry on instead.")
CLOSING_BYE = ("[call] He is done (a goodbye, or nothing else). Say one short goodbye in his language, then call "
               "end_call.")
CLOSING_BYE_PENDING = ("[call] He is done (a goodbye, or nothing else), but an answer he asked for is still on its way: "
                       "say \"I'll send the rest on Telegram\", then one short goodbye, then call end_call.")


# --- end reason (usage event: ids, flags and numbers only) ----------------------------------------------------------

def end_why(reason, by, why=None):
    """reason/by: VoiceSession.close's; why: the session's own end reason when it decided (see session.py).
    -> him | goodbye | nothing_else | closing_silence | idle_silence | pending_timeout | end_call | time_limit |
       error | relay | network | other"""
    r = str(reason or "")
    if r == "assistant_hangup":
        return why or "end_call"
    if r == "app lost":                    # the phone's connection dropped and didn't come back in phonegw's grace
        return "network"
    if r in ("hangup", "page closed", "app closed") or by == "him":
        return "him"
    if r == "time limit":
        return "time_limit"
    if r == "relay stopped":
        return "relay"
    if r == "provider ended" or r.startswith("provider failed") or by == "error":
        return "error"
    return "other"


def call_ref(call_id):
    """The same 8-hex hash ~/bin/lib/usage.py's call-log collector uses: links this event to the call's row."""
    return "call:" + hashlib.sha1(str(call_id or "").encode()).hexdigest()[:8]


_ULOG = []


def usage_log():
    """~/bin/lib/usage.py's log() under a private module name, loaded once (a no-op when missing)."""
    if not _ULOG:
        fn = None
        try:
            path = os.path.join(os.path.expanduser("~"), "bin", "lib", "usage.py")
            loader = importlib.machinery.SourceFileLoader("voice_usage_lib_closing", path)
            spec = importlib.util.spec_from_loader("voice_usage_lib_closing", loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            fn = mod.log
        except Exception:
            fn = None
        _ULOG.append(fn or (lambda *a, **kw: False))
    return _ULOG[0]
