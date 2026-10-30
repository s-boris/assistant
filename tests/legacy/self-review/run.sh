#!/usr/bin/env bash
# Tests for ~/bin/self-review. Exit 0 = all pass. SR=<path> tests another copy (e.g. before install).
# Builds a temp brain repo (BRAIN_GIT_DIR / BRAIN_WORK_TREE) with fixtures for every check, a fake LLM
# (SELF_REVIEW_LLM) and fake hard rules. All private-looking values are FAKE and generated at run time.
# SELF_REVIEW_LIVE=1 adds one call through the real local LLM (llm-local in the vault; ~10-60 s).
# Prompt tuning on realistic fixtures (real LLM): live.sh tp (planted problems) / live.sh noise (must stay silent).
set -uo pipefail
SR=${SR:-${SELF_REVIEW_BIN:-/home/node/bin/self-review}}   # run-tests.sh passes the tool as SELF_REVIEW_BIN
T=$(mktemp -d "${TMPDIR:-/tmp}/self-review-tests.XXXXXX")
trap "${KEEP:+:} rm -rf $T" EXIT
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; }
has() { if grep -qF -- "$2" "$3"; then ok "$1"; else no "$1"; fi; }
hasnt() { if grep -qF -- "$2" "$3"; then no "$1"; else ok "$1"; fi; }
rand() { LC_ALL=C tr -dc "${2:-A-Za-z0-9}" </dev/urandom | head -c "$1"; }

export BRAIN_GIT_DIR=$T/brain.git BRAIN_WORK_TREE=$T/home SELF_REVIEW_STATE=$T/state SELF_REVIEW_REPORTS=$T/reports \
       SELF_REVIEW_INBOX=$T/inbox SELF_REVIEW_LLM=$T/fake-llm SELF_REVIEW_HARD_RULES=$T/hard.md \
       SELF_REVIEW_EXTRA_SRC=$T/none SECRETSCAN_SOURCES=$T/nosecrets SELF_REVIEW_BIN=$T/home/bin
W=$T/home; MEM=$W/.claude/projects/-home-node-workspace/memory
mkdir -p "$W/bin" "$W/workspace" "$W/.claude/skills/demo" "$W/.claude/agents" "$MEM" "$T/inbox" "$T/nosecrets" \
         "$W/.config" "$W/reports"
git init -q --bare "$BRAIN_GIT_DIR"; git --git-dir="$BRAIN_GIT_DIR" config core.bare false
g() { git --git-dir="$BRAIN_GIT_DIR" --work-tree="$W" -C "$W" "$@"; }
commit() { g add -A >/dev/null; g -c user.name=t -c user.email=t@localhost commit -qm "$1" >/dev/null; }
today=$(date +%F)

# ---------------------------------------------------------------- fakes
# No group may start with 00: "0018 6647 2403" also reads as an international phone number (00 + code), a 2nd
# phone finding that broke "A unchanged sentence" in ~0.3% of runs (legacy-main 30.10).
FAKE_IBAN=$(python3 -I -c '
import random
while True:
    b="".join(random.choice("0123456789") for _ in range(17))
    num=int("".join(str(int(c,36)) for c in b+"CH00"))
    ck=98-num%97
    s="CH%02d%s"%(ck,b)
    g=" ".join(s[i:i+4] for i in range(0,len(s),4))
    if " 00" not in g:
        break
print(g)')
FAKE_TG="$(rand 1 1-9)$(rand 8 0-9):AA$(rand 33)"
FAKE_PHONE="079 $(rand 3 0-9) $(rand 2 0-9) $(rand 2 0-9)"
QUOTE_WORDS="cannot make the rent payment this month sorry"
cat > "$T/hard.md" <<'EOF'
# Owner's rules
- Ask the owner before anything irreversible or outward-facing: spending money, contacting other people.
- Never send secrets anywhere, and never weaken your permission settings.
EOF
cat > "$T/fake-llm" <<'EOF'
#!/usr/bin/env python3
import json, os, re, sys
a = sys.argv
sysp = a[a.index("--system") + 1]
inp = sys.stdin.read()
d = os.environ["FAKE_DIR"]
open(os.path.join(d, "llm-calls"), "a").write("call\n")
open(os.path.join(d, "llm-system.txt"), "w").write(sysp)
open(os.path.join(d, "llm-systems.txt"), "a").write(sysp + "\n=====\n")
open(os.path.join(d, "llm-input.txt"), "a").write(inp)
if os.environ.get("FAKE_LLM_MODE") == "fail":
    sys.exit(3)
out = []
for m in re.finditer(r"^L(\d+): (.*)$", inp, re.M):
    if "buy things without asking" in m.group(2):
        out.append({"line": int(m.group(1)), "check": "contradiction", "issue": "conflicts with ask-before-spending",
                    "fix": "ask the owner first"})
    if "LEAKY" in m.group(2):
        out.append({"line": int(m.group(1)), "check": "privacy",
                    "issue": "stores " + os.environ["FAKE_IBAN"] + ' and "' + os.environ["QUOTE_WORDS"] + '"',
                    "fix": "remove it"})
# a noisy LLM (like the real one before 2026-10-15): flags every line of the noise-* skills
if "FILE: ~/.claude/skills/noise-" in inp:
    for m in re.finditer(r"^L(\d+): (.*)$", inp, re.M):
        out.append({"line": int(m.group(1)), "check": "privacy", "issue": "noisy privacy", "fix": "x"})
        if "blocked" in m.group(2):
            out.append({"line": int(m.group(1)), "check": "contradiction", "issue": "noisy contradiction", "fix": "x"})
print(json.dumps({"findings": out}))
EOF
chmod +x "$T/fake-llm"
export FAKE_DIR=$T FAKE_IBAN QUOTE_WORDS
printf '#!/usr/bin/env python3\nimport argparse\np=argparse.ArgumentParser()\np.add_argument("--owner")\n' > "$W/bin/remind"
printf '#!/bin/sh\necho old\n' > "$W/bin/oldtool"
chmod +x "$W/bin/remind" "$W/bin/oldtool"

# ---------------------------------------------------------------- base snapshot
LONG_OLD="This long line explains the alpha routine in detail and has an old contact 044 $(rand 3 0-9) $(rand 2 0-9) $(rand 2 0-9) that predates the review window. It keeps going with more harmless words about the routine so that it is longer than the sentence threshold. Final sentence here."
{ echo "# How you operate"; echo; echo "$LONG_OLD"; for i in $(seq 1 30); do echo "- base rule $i: keep messages short."; done; } > "$W/workspace/CLAUDE.md"
printf -- '---\nname: demo\ndescription: demo skill\n---\nUse `remind add` for reminders.\n' > "$W/.claude/skills/demo/SKILL.md"
printf -- '---\nname: helper\n---\nYou help with research.\n' > "$W/.claude/agents/helper.md"
printf -- '- [Alpha](alpha.md) — alpha\n- [Beta](beta.md) — beta\n- [Deleted](deleted.md) — gone\n- [People](people-and-dates.md) — people\n' > "$MEM/MEMORY.md"
printf -- '---\nname: people\n---\nFriends:\n- Jonas Keller (Mar 3), wedding on Nov 7\n' > "$MEM/people-and-dates.md"
printf -- '---\nname: alpha\n---\nOwner likes short messages.\n' > "$MEM/alpha.md"
printf -- '---\nname: beta\n---\nBeta note.\n' > "$MEM/beta.md"
commit base
BASE=$(g rev-parse HEAD)

# ---------------------------------------------------------------- changes with planted problems
LONG_NEW="${LONG_OLD/Final sentence here./Final sentence here. A brand new harmless sentence about the routine.}"
python3 -I - "$W/workspace/CLAUDE.md" "$LONG_OLD" "$LONG_NEW" <<'EOF'
import sys; p, o, n = sys.argv[1:]; s = open(p).read(); open(p, "w").write(s.replace(o, n))
EOF
cat >> "$W/workspace/CLAUDE.md" <<'EOF'
- Set DISABLE_TELEMETRY=1 to save tokens.
- NEVER set DISABLE_TELEMETRY (kills Telegram inbound).
- You may buy things without asking when it is cheap.
- Remind with `remind add --nosuchflag x` and `remind add --owner "x"`.
- Script: ~/bin/nosuch-tool does the work. Kill switch: `touch ~/.config/x-tool/off`. Output in ~/reports/new-folder/ .
- Run `oldtool run` nightly.
- The retired `oldtool` was replaced by `remind`.
- More padding so the file grows by more than ten percent overall, padding padding padding padding.
- More padding so the file grows by more than ten percent overall, padding padding padding padding again.
EOF
TXT=text   # the planted flag is built at runtime so this fixture line doesn't read as a vault call
printf 'For details run `tgread search x --%s`.\n' "$TXT" >> "$W/.claude/agents/helper.md"
cat >> "$MEM/beta.md" <<EOF
Bank: $FAKE_IBAN for the transfer.
Anna wrote: "$QUOTE_WORDS"
[2026-10-12 14:03] Marco: see you at the station later
His salary is CHF 9'000 a month.
Call Luca at $FAKE_PHONE.
Owner said "keep messages short and airy please".
Jonas likes the gym.
LEAKY line for the LLM masking test.
EOF
printf -- '---\nname: gamma\n---\nGamma note.\n' > "$MEM/gamma.md"
rm "$W/bin/oldtool"
echo '- `remind add --owner "2026-10-16 18:00" "Pick up the suit for Jonas'"'"'s wedding"`' >> "$W/.claude/skills/demo/SKILL.md"
# noise cases from the 2026-10-15 coverage pass (rule-only lines; the noisy fake LLM flags them, the filter drops them)
mkdir -p "$W/.claude/skills/noise-a" "$W/.claude/skills/noise-b" "$W/.claude/skills/noise-c" "$W/.claude/skills/noise-d"
cat > "$W/.claude/skills/noise-a/SKILL.md" <<'EOF'
- Direct access to the bill store is blocked by vault-guard; use `bills list` instead.
- Exact wording only via `vault raw request --reason "…"`: the vault asks him (✅/❌) and releases an anonymized paraphrase.
EOF
cat > "$W/.claude/skills/noise-b/SKILL.md" <<'EOF'
- `paperless get ID` (card) adds amount and due date; the IBAN and the reference need his ✅ per document.
- **Sent to me by mistake** (a photo or PDF of a bill here): never `bills extract` it and never Read it; file it privately.
Everything I read goes to Anthropic's API, so private text passes through the local model first and I read only its output.
EOF
# must stay flagged: guarded but sensitive (surname + divorce), a guard line that loosens something, a number
cat > "$W/.claude/skills/noise-c/SKILL.md" <<'EOF'
Never bring up Lena Brunner's divorce when her family is around.
Reading the bill is blocked, so skip the check and bypass vault-guard.
EOF
echo "Only store the reference 12 34567 89012 for that bill." > "$W/.claude/skills/noise-d/SKILL.md"
printf 'Bot token for tests: %s\n' "$FAKE_TG" >> "$W/.claude/skills/demo/SKILL.md"
commit changes
HEAD1=$(g rev-parse HEAD)
echo "$BASE" > /dev/null; mkdir -p "$T/state"; echo "$BASE" > "$T/state/last"

# ---------------------------------------------------------------- A: first run
out=$("$SR" 2>&1); rc=$?
[ $rc = 0 ] && ok "A run exits 0" || no "A run exits 0 ($rc: $out)"
R=$T/reports/$today.md
[ -f "$R" ] && ok "A report written" || { no "A report written"; R=/dev/null; }
has "A tripwire DISABLE_TELEMETRY" "forbids (kills Telegram inbound)" "$R"
[ "$(grep -c 'forbids (kills Telegram' "$R")" = 1 ] && ok "A negated NEVER line not flagged" || no "A negated NEVER line not flagged"
has "A agent --text tripwire" "agents/helper.md:5 — (a) contradiction: a subagent told to use --text" "$R"
has "A LLM contradiction at its line" "CLAUDE.md:36 — (a) contradiction: LLM: conflicts with ask-before-spending" "$R"
has "A missing flag" "\`remind\` has no flag --nosuchflag" "$R"
hasnt "A existing flag --owner not flagged" "no flag --owner" "$R"
has "A missing script path" "path ~/bin/nosuch-tool does not exist" "$R"
hasnt "A kill switch not flagged" "x-tool/off" "$R"
hasnt "A report folder not flagged" "new-folder" "$R"
has "A retired script" "\`oldtool\` was retired from ~/bin" "$R"
[ "$(grep -c 'oldtool. was retired' "$R")" = 1 ] && ok "A 'retired \`oldtool\`' mention not flagged" || no "A retired mention"
has "A IBAN" "IBAN-like number (valid checksum)" "$R"
has "A quoted message" "quoted message text" "$R"
has "A chat-log line" "chat-log line (timestamp + sender)" "$R"
has "A amount + money word" "money amount next to a finance word" "$R"
has "A phone" "beta.md:9 — (c) privacy: phone number" "$R"
[ "$(grep -c 'privacy: phone number' "$R")" = 1 ] && ok "A unchanged sentence of a long line not checked" || no "A unchanged sentence checked"
hasnt "A owner's own quote not flagged" "beta.md:10 —" "$R"
has "A secretscan hit (name only)" "secretscan hit: pattern:telegram-bot-token" "$R"
has "A index: unindexed note" "gamma.md:1 — (d) index drift: memory note missing from the MEMORY.md index" "$R"
has "A index: missing file" "index line points to missing note deleted.md" "$R"
has "A growth" "(e) CLAUDE.md growth: CLAUDE.md grew" "$R"
has "A LLM masked IBAN" "LLM: stores [IBAN] and [quote]" "$R"
for v in "$FAKE_IBAN" "$QUOTE_WORDS" "$FAKE_PHONE" "$FAKE_TG" "see you at the station" "9'000"; do
  hasnt "A report never holds planted value (${v:0:6}…)" "$v" "$R"
done
grep -q "Never send secrets anywhere" "$T/llm-system.txt" && ok "A LLM gets the hard rules" || no "A LLM gets the hard rules"
grep -q "A brand new harmless sentence" "$T/llm-input.txt" && ok "A LLM gets the changed sentence" || no "A changed sentence"
grep -q "that predates the review window" "$T/llm-input.txt" && no "A LLM got an unchanged sentence" || ok "A LLM gets only changed sentences"
n=$(ls "$T/inbox" | wc -l); [ "$n" = 1 ] && ok "A one inbox note" || no "A one inbox note ($n)"
N=$(ls "$T"/inbox/self-review-*.md 2>/dev/null | head -1)
has "A note lists findings with file:line" "agents/helper.md:5" "${N:-/dev/null}"
for v in "$FAKE_IBAN" "$QUOTE_WORDS" "$FAKE_TG"; do hasnt "A note never holds planted value (${v:0:6}…)" "$v" "${N:-/dev/null}"; done
[ "$(cat "$T/state/last")" = "$HEAD1" ] && ok "A state last = HEAD" || no "A state last = HEAD"
nopen=$("$SR" open --json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
[ "$nopen" -ge 15 ] && ok "A open findings stored ($nopen)" || no "A open findings stored ($nopen)"

hasnt "A noise: guard/approval lines silent (noise-a)" "skills/noise-a/" "$R"
hasnt "A noise: tier-1 fields, don't-Read, premise silent (noise-b)" "skills/noise-b/" "$R"
has "A noise: 6 LLM findings dropped and counted" "6 findings dropped (rule-only lines)" "$R"
has "A noise filter keeps a guarded line with a surname + sensitive topic" "noise-c/SKILL.md:1 — (c) privacy: LLM: noisy privacy" "$R"
has "A noise filter keeps a guard line that loosens something" "noise-c/SKILL.md:2 — (a) contradiction: LLM: noisy contradiction" "$R"
has "A noise filter keeps a line with a number" "noise-d/SKILL.md:1 — (c) privacy: LLM: noisy privacy" "$R"
dl=$(grep -n "Jonas" "$W/.claude/skills/demo/SKILL.md" | cut -d: -f1)
has "A people-memory name in a skill example" "skills/demo/SKILL.md:$dl — (c) privacy: names a person from the people memory" "$R"
hasnt "A people-memory name in a memory note not flagged" "beta.md:11 — (c) privacy: names a person" "$R"
hasnt "A report never holds the person's name" "Jonas" "$R"
grep -q "NOT a finding" "$T/llm-system.txt" && ok "A prompt has the not-a-finding guidance" || no "A prompt not-a-finding guidance"
grep -q "server name and IP" "$T/llm-system.txt" && grep -q "Krankenkasse" "$T/llm-system.txt" && grep -q "find the mail from X" "$T/llm-system.txt" \
  && ok "A prompt covers owner data, search terms, trigger phrases" || no "A prompt covers owner data, search terms, trigger phrases"
grep -q "skills/noise-a/SKILL.md (skill: generic docs" "$T/llm-input.txt" && ok "A LLM gets the file kind" || no "A LLM gets the file kind"
grep -q "^- You may buy things without asking" "$T/llm-systems.txt" && no "A changed rule line fed back as a RULE" || ok "A changed rule line not fed back as a RULE"

# ---------------------------------------------------------------- B: no changes -> nothing new
"$SR" --cron >/dev/null 2>&1
n=$(ls "$T/inbox" | wc -l); [ "$n" = 1 ] && ok "B no new note without new findings" || no "B no new note ($n)"
has "B report lists earlier open findings" "Still open from earlier" "$T/reports/$today.md"

# ---------------------------------------------------------------- C: fixing a line resolves its finding
sed -i "/^Bank: /d" "$MEM/beta.md"; commit fix-iban
"$SR" --cron >/dev/null 2>&1
"$SR" open > "$T/open.txt"
hasnt "C fixed IBAN line no longer open" "IBAN-like" "$T/open.txt"
has "C other findings still open" "phone number" "$T/open.txt"
n=$(ls "$T/inbox" | wc -l); [ "$n" = 1 ] && ok "C no note for a fix" || no "C no note for a fix ($n)"

# ---------------------------------------------------------------- D: ack
id=$(grep -- "helper.md:5" "$T/open.txt" | head -1 | sed -E 's/^- `([0-9a-f]+)`.*/\1/')
"$SR" ack "$id" >/dev/null
"$SR" open > "$T/open2.txt"
hasnt "D acked finding hidden" "helper.md:5" "$T/open2.txt"

# ---------------------------------------------------------------- E: --dry-run writes nothing
before=$(find "$T/reports" "$T/inbox" "$T/state" -type f -newer "$T/open2.txt" | wc -l)
sleep 1; touch "$T/mark"
"$SR" --since "$BASE" --dry-run > "$T/dry.txt" 2>&1
after=$(find "$T/reports" "$T/inbox" "$T/state" -type f -newer "$T/mark" | wc -l)
[ "$after" = 0 ] && ok "E dry run writes nothing" || no "E dry run wrote $after files"
has "E dry run prints the report" "# Self-review" "$T/dry.txt"

# ---------------------------------------------------------------- F: LLM failure -> skipped and noted
last_before=$(cat "$T/state/last")
FAKE_LLM_MODE=fail "$SR" --since "$BASE" --report-only > "$T/f.txt" 2>&1
RF=$(ls -t "$T"/reports/*.md | head -1)
has "F LLM failure noted" "LLM unavailable" "$RF"
has "F deterministic findings still there" "path ~/bin/nosuch-tool does not exist" "$RF"
[ "$(cat "$T/state/last")" = "$last_before" ] && ok "F report-only keeps state" || no "F report-only keeps state"
n=$(ls "$T/inbox" | wc -l); [ "$n" = 1 ] && ok "F report-only writes no note" || no "F report-only note ($n)"

# ---------------------------------------------------------------- G: budget + --no-llm + date --since
: > "$T/llm-calls"
"$SR" --since "$BASE" --dry-run --max-llm 1 > "$T/g.txt" 2>&1
[ "$(wc -l < "$T/llm-calls")" = 1 ] && ok "G --max-llm caps calls" || no "G --max-llm caps calls"
has "G skipped calls counted" "skipped (budget)" "$T/g.txt"
: > "$T/llm-calls"
"$SR" --since "$BASE" --dry-run --no-llm > /dev/null 2>&1
[ ! -s "$T/llm-calls" ] && ok "G --no-llm makes no calls" || no "G --no-llm"
"$SR" --since "30 days ago" --dry-run --no-llm > "$T/g2.txt" 2>&1
has "G date before the repo start = first snapshot" "Range: ${BASE:0:7}.." "$T/g2.txt"
"$SR" --since "not-a-rev-or-date" --dry-run >/dev/null 2>&1 && no "G bad --since rejected" || ok "G bad --since rejected"

# ---------------------------------------------------------------- K: pinned clock (SELF_REVIEW_NOW)
# Own brain repo with commits at fixed instants; every run pins the clock and runs with TZ=UTC, so a date, ISO week or
# day window taken from UTC, from the container zone or from wall-clock maths fails here.
K=$T/k; KW=$K/home; KM=$KW/.claude/projects/-home-node-workspace/memory
mkdir -p "$KW/workspace" "$KW/bin" "$KM" "$K/reports" "$K/inbox"
git init -q --bare "$K/brain.git"; git --git-dir="$K/brain.git" config core.bare false
kg() { git --git-dir="$K/brain.git" --work-tree="$KW" -C "$KW" "$@"; }
kcommit() { kg add -A >/dev/null; GIT_AUTHOR_DATE=$1 GIT_COMMITTER_DATE=$1 kg -c user.name=t -c user.email=t@localhost commit -qm "$1" >/dev/null; }
kr() {  # kr STATE NOW ARGS...: one run on the K repo with the clock pinned
  local st=$1 now=$2; shift 2
  TZ=UTC SELF_REVIEW_NOW=$now BRAIN_GIT_DIR=$K/brain.git BRAIN_WORK_TREE=$KW SELF_REVIEW_STATE=$K/$st \
    SELF_REVIEW_REPORTS=$K/reports SELF_REVIEW_INBOX=$K/inbox SELF_REVIEW_BIN=$KW/bin "$SR" "$@"
}
kopen() {  # kopen STATE CHECK FIELD: that field of the stored findings of one check, sorted
  python3 -I -c 'import json, sys
d = json.load(open(sys.argv[1]))
print(" ".join(sorted(str(v.get(sys.argv[3])) for v in d.values() if v["check"] == sys.argv[2])))' "$K/$1/open.json" "$2" "$3"
}
klines() { for i in $(seq "$1"); do echo "Fixture line $i keeps the routine short and plain."; done; }
klines 20 > "$KW/workspace/CLAUDE.md"
printf -- '- [Gone](gone.md) — a note that does not exist\n' > "$KM/MEMORY.md"
kcommit 2026-10-19T12:30:00+02:00                                      # c0: Monday before the fall-back night
klines 30 > "$KW/workspace/CLAUDE.md"; kcommit 2026-10-24T10:00:00+02:00  # c1: +50%

# DST: the 7-day window is 7 x 24 h (as git's "7 days ago"); report names use the owner's date and time
kr k1 2026-10-26T12:00:00+01:00 --dry-run --no-llm > "$K/k1.txt" 2>&1
has "K1 7-day window across the fall-back night: c0 is 7 d 30 min old, in" "since 7 days ago" "$K/k1.txt"
kr k1 2026-10-26T11:20:00+01:00 --dry-run --no-llm > "$K/k1b.txt" 2>&1
hasnt "K1 c0 6 d 23 h 50 min old: not yet in the 7-day window" "since 7 days ago" "$K/k1b.txt"
kr k2 2026-10-25T00:30:00+02:00 --report-only --no-llm >/dev/null 2>&1
[ -f "$K/reports/2026-10-25.md" ] && ok "K2 fall-back night 00:30 (22:30Z the day before): report of the owner's day" \
  || no "K2 fall-back night report name ($(ls "$K/reports" | tr '\n' ' '))"
kr k2 2026-10-25T02:30:00+01:00 --report-only --no-llm >/dev/null 2>&1
[ -f "$K/reports/2026-10-25-0230.md" ] && ok "K2 repeated 02:30 (01:30Z): second report of the day named -0230" \
  || no "K2 repeated hour report name ($(ls "$K/reports" | tr '\n' ' '))"
kr k2 2026-03-29T00:30:00+01:00 --report-only --no-llm >/dev/null 2>&1
[ -f "$K/reports/2026-03-29.md" ] && ok "K2 spring-forward night 00:30 (23:30Z the day before): report of the owner's day" \
  || no "K2 spring-forward report name ($(ls "$K/reports" | tr '\n' ' '))"

# week turn: the 7-day growth finding is keyed by the owner's ISO week (raised again from Monday 00:00 local)
klines 40 > "$KW/workspace/CLAUDE.md"; kcommit 2026-11-01T20:00:00+01:00  # c2: +33% (Sunday evening)
kr k3 2026-11-01T22:00:00+01:00 --no-llm >/dev/null 2>&1                 # first run: base = 1 day ago = c1
kr k3 2026-11-01T23:50:00+01:00 --no-llm >/dev/null 2>&1                 # Sunday, week 44: c2 vs 7 days ago (c1)
n=$(kopen k3 growth first | wc -w); [ "$n" = 2 ] && ok "K3 Sunday 23:50: growth vs the last review + vs 7 days ago" \
  || no "K3 Sunday growth findings ($n: $(kopen k3 growth first))"
kr k3 2026-11-02T00:10:00+01:00 --no-llm >/dev/null 2>&1                 # Monday 00:10 local = Sunday 23:10Z
[ "$(kopen k3 growth first)" = "2026-11-01 2026-11-01 2026-11-02" ] \
  && ok "K3 Monday 00:10 (Sunday in UTC): new week, the 7-day growth finding is raised again" \
  || no "K3 week turn ($(kopen k3 growth first))"
kr k3 2026-11-02T00:40:00+01:00 --no-llm >/dev/null 2>&1
[ "$(kopen k3 growth first | wc -w)" = 3 ] && ok "K3 same week: not raised a second time" || no "K3 same week ($(kopen k3 growth first))"
kr k3 2026-11-02T00:10:00+01:00 ack all >/dev/null 2>&1
[ "$(kopen k3 growth acked)" = "2026-11-02 2026-11-02 2026-11-02" ] && ok "K3 ack dated with the owner's day" \
  || no "K3 ack date ($(kopen k3 growth acked))"

# month turn: a finding still present after 30 days (index drift) is raised again on day 31, counted in owner days
kr k5 2026-11-01T00:20:00+01:00 --no-llm >/dev/null 2>&1                 # 31.10 23:20Z
[ "$(kopen k5 index-drift first)" = 2026-11-01 ] && ok "K4 first seen 01.11 00:20 (31.10 in UTC): dated 01.11" \
  || no "K4 first-seen date ($(kopen k5 index-drift first))"
kr k5 2026-12-01T23:50:00+01:00 --no-llm >/dev/null 2>&1
[ "$(kopen k5 index-drift first)" = 2026-11-01 ] && ok "K4 30 days later (01.12 23:50): still the same finding" \
  || no "K4 day 30 ($(kopen k5 index-drift first))"
kr k5 2026-12-02T00:10:00+01:00 --no-llm >/dev/null 2>&1                 # 01.12 23:10Z
[ "$(kopen k5 index-drift first)" = 2026-12-02 ] && ok "K4 day 31 (02.12 00:10, 01.12 in UTC): raised again" \
  || no "K4 day 31 ($(kopen k5 index-drift first))"

kr k6 "soon" --dry-run --no-llm > "$K/k6.txt" 2>&1 && no "K5 unreadable SELF_REVIEW_NOW rejected" \
  || has "K5 unreadable SELF_REVIEW_NOW rejected" "SELF_REVIEW_NOW is not ISO 8601" "$K/k6.txt"

# ISO year in the week key: an ack of week 45 hides only that week, not week 45 a year later; a stored ack under the
# week-only key (gw|<week>, no year) still hides the week it was raised in, and only that one
klines 50 > "$KW/workspace/CLAUDE.md"; kcommit 2027-11-05T10:00:00+01:00  # c3: +25% vs c2, a year after K3
kweekly() {  # kweekly STATE: "<acked or open>:<first>" of the stored 7-day growth findings, sorted
  python3 -I -c 'import json, sys
d = json.load(open(sys.argv[1]))
print(" ".join(sorted(("acked" if v.get("acked") else "open") + ":" + v["first"] for v in d.values()
                      if v["check"] == "growth" and "7 days ago" in v["msg"])))' "$K/$1/open.json"
}
kweek_only() {  # kweek_only STATE WEEK FIRST: a stored, acked 7-day growth finding under the week-only key
  mkdir -p "$K/$1"
  python3 -I -c 'import hashlib, json, sys
fid = hashlib.sha1(f"gw|{sys.argv[2]}".encode()).hexdigest()[:8]
json.dump({fid: {"id": fid, "check": "growth", "file": "workspace/CLAUDE.md", "line": 1, "lh": None,
                 "msg": "CLAUDE.md grew 1 → 2 bytes (+100%) since 7 days ago", "fix": "rewrite",
                 "first": sys.argv[3], "last": sys.argv[3], "acked": sys.argv[3]}}, open(sys.argv[1], "w"))' \
    "$K/$1/open.json" "$2" "$3"
}
kr k3 2027-11-08T00:10:00+01:00 --no-llm --since HEAD >/dev/null 2>&1    # week 45 of 2027; k3 acked week 45 of 2026
[ "$(kweekly k3)" = "open:2027-11-08" ] && ok "K6 same week number a year later: raised, not hidden by last year's ack" \
  || no "K6 same week next year ($(kweekly k3))"
kweek_only k7 45 2027-11-08
kr k7 2027-11-08T00:40:00+01:00 --no-llm >/dev/null 2>&1
[ "$(kweekly k7)" = "acked:2027-11-08" ] && ok "K6 week-only ack of this year's week 45: still hides it (no re-raise)" \
  || no "K6 week-only ack this year ($(kweekly k7))"
want=$(printf 'gw|2027-W45' | sha1sum | cut -c1-8)
has "K6 week-only ack stored under the ISO year + week id after the first save" "\"$want\"" "$K/k7/open.json"
kr k7 2027-11-08T00:50:00+01:00 --no-llm >/dev/null 2>&1
[ "$(kweekly k7)" = "acked:2027-11-08" ] && ok "K6 re-keyed ack: still hides this week on the next run" \
  || no "K6 week-only ack re-keyed ($(kweekly k7))"
kweek_only k8 45 2026-11-02
kr k8 2027-11-08T00:40:00+01:00 --no-llm >/dev/null 2>&1
[ "$(kweekly k8)" = "open:2027-11-08" ] && ok "K6 week-only ack of week 45 a year earlier: does not hide this year's" \
  || no "K6 week-only ack last year ($(kweekly k8))"

# ---------------------------------------------------------------- M: own repo for diff parsing, dedup, open/ack, errors
# Own brain repo + fake LLM with failure modes (MLLM=flag|garbage|exit4|exit5|unavail|weird|far); clock pinned.
M=$T/m; MW=$M/home; MMEM=$MW/.claude/projects/-home-node-workspace/memory
mkdir -p "$MW/workspace" "$MW/bin" "$MW/.claude/skills/s1" "$MW/.claude/skills/synced/x" "$MW/.claude/agents" \
         "$MMEM" "$M/inbox" "$M/reports"
git init -q --bare "$M/brain.git"; git --git-dir="$M/brain.git" config core.bare false
mg() { git --git-dir="$M/brain.git" --work-tree="$MW" -C "$MW" "$@"; }
mcommit() { mg add -A >/dev/null; mg -c user.name=t -c user.email=t@localhost commit -qm "$1" >/dev/null; }
mr() {  # mr STATE NOW ARGS...: one run on the M repo (MB / MLLM_BIN override the brain dir / LLM path)
  local st=$1 now=$2; shift 2
  M_DIR=$M SELF_REVIEW_NOW=$now BRAIN_GIT_DIR=${MB:-$M/brain.git} BRAIN_WORK_TREE=$MW SELF_REVIEW_STATE=$M/$st \
    SELF_REVIEW_REPORTS=$M/reports SELF_REVIEW_INBOX=$M/inbox SELF_REVIEW_BIN=$MW/bin \
    SELF_REVIEW_LLM=${MLLM_BIN:-$M/m-llm} "$SR" "$@"
}
mj() {  # mj STATE FILE-SUBSTRING FIELD: that field of the stored findings in matching files
  python3 -I -c 'import json, sys
d = json.load(open(sys.argv[1]))
print(" ".join(sorted(str(v.get(sys.argv[3])) for v in d.values() if sys.argv[2] in v["file"])))' "$M/$1/open.json" "$2" "$3"
}
notes() { find "$M/inbox" -name 'self-review-*.md' | wc -l; }
calls() { wc -l < "$M/llm-calls" 2>/dev/null || echo 0; }
cat > "$M/m-llm" <<'EOF'
#!/usr/bin/env python3
import json, os, re, sys
d, mode = os.environ["M_DIR"], os.environ.get("MLLM", "flag")
inp = sys.stdin.read()
open(os.path.join(d, "llm-calls"), "a").write("call\n")
open(os.path.join(d, "llm-input.txt"), "a").write(inp)
m = re.match(r"FILE: (\S+)", inp)
open(os.path.join(d, "llm-files"), "a").write((m.group(1) if m else "?") + "\n")
codes = {"unavail": 3, "exit4": 4, "exit5": 5}
if mode in codes:
    sys.exit(codes[mode])
if mode == "garbage":
    print("the model says hello"); sys.exit(0)
lines = [(int(a), b) for a, b in re.findall(r"^L(\d+): (.*)$", inp, re.M)]
if mode == "weird":   # only the first 3 items count: a non-dict, an unknown check, an unparsable line
    out = ["not a dict", {"check": "style", "line": lines[0][0], "issue": "unknown check kind"},
           {"check": "contradiction", "line": "abc", "issue": "weird bad line", "fix": "x"},
           {"check": "contradiction", "line": lines[0][0], "issue": "weird fourth item", "fix": "x"}]
elif mode == "far":
    out = [{"check": "contradiction", "line": 9999, "issue": "far line", "fix": "x"}]
else:
    out = [{"line": n, "check": "contradiction", "issue": "planted conflict", "fix": "drop it"}
           for n, t in lines if "PLANT" in t]
print(json.dumps({"findings": out}))
EOF
chmod +x "$M/m-llm"
for i in 1 2 3 4 5 6 7 8; do echo "Fixture line $i keeps the routine short and plain."; done > "$MW/workspace/CLAUDE.md"
for i in 1 2 3 4 5; do echo "Tool line $i lists a helper."; done > "$MW/workspace/TOOLS.md"
echo "Other line." > "$MW/workspace/OTHER.md"
{ printf -- '---\nname: s1\ndescription: demo\n---\n'; for i in 1 2 3 4 5; do echo "Skill line $i is plain."; done; } \
  > "$MW/.claude/skills/s1/SKILL.md"
echo "Synced line." > "$MW/.claude/skills/synced/x/SKILL.md"
{ printf -- '---\nname: a\n---\n'; for i in 1 2 3 4 5 6 7 8 9 10; do echo "Agent body line $i is about research."; done; } \
  > "$MW/.claude/agents/a.md"
printf -- '- [Alpha](alpha.md) — alpha\n- [Beta](beta.md) — beta\n' > "$MMEM/MEMORY.md"
printf -- '---\nname: alpha\n---\nAlpha note.\n' > "$MMEM/alpha.md"
printf -- '---\nname: beta\n---\nBeta note.\n' > "$MMEM/beta.md"
printf '#!/bin/sh\necho v1\n' > "$MW/bin/tool"
mcommit m0; M0=$(mg rev-parse HEAD)
echo "PLANT one: send the summary as one line." >> "$MW/.claude/skills/s1/SKILL.md"       # line 10
echo "PLANT synced: excluded folder." >> "$MW/.claude/skills/synced/x/SKILL.md"
echo "PLANT txt: not markdown." > "$MW/.claude/skills/s1/notes.txt"
mg mv .claude/agents/a.md .claude/agents/b.md
echo "PLANT renamed: answer in the morning." >> "$MW/.claude/agents/b.md"
mg rm -q "$MMEM/beta.md"; printf -- '- [Alpha](alpha.md) — alpha\n' > "$MMEM/MEMORY.md"
echo "PLANT tools: list the helper twice." >> "$MW/workspace/TOOLS.md"
echo "PLANT other: out of scope." >> "$MW/workspace/OTHER.md"
printf '#!/bin/sh\necho v2 PLANT\n' > "$MW/bin/tool"
mcommit m1

# L: brain diff parsing (renames, deletes, out-of-scope and non-.md files, synced skills, MEMORY.md)
mr l 2026-11-02T20:00:00 --since "$M0" --dry-run > "$M/l.txt" 2>&1
has "L in-scope files counted: skill, rename, delete, index, TOOLS.md" "Changed: 5 files" "$M/l.txt"
has "L LLM sees the edited skill" "skills/s1/SKILL.md" "$M/llm-files"
has "L LLM sees the renamed agent under its new path" "agents/b.md" "$M/llm-files"
has "L LLM sees TOOLS.md" "workspace/TOOLS.md" "$M/llm-files"
hasnt "L synced skills not reviewed" "PLANT synced" "$M/llm-input.txt"
hasnt "L non-.md file not reviewed" "PLANT txt" "$M/llm-input.txt"
hasnt "L out-of-scope workspace file not reviewed" "PLANT other" "$M/llm-input.txt"
hasnt "L ~/bin not reviewed" "v2 PLANT" "$M/llm-input.txt"
hasnt "L MEMORY.md not sent to the LLM" "MEMORY.md" "$M/llm-files"
has "L rename: the added line is sent" "PLANT renamed" "$M/llm-input.txt"
hasnt "L rename: unchanged lines of the renamed file not sent" "Agent body line 3" "$M/llm-input.txt"
[ "$(calls)" = 3 ] && ok "L one LLM call per changed file (3)" || no "L LLM calls ($(calls))"
has "L finding at its line number" "s1/SKILL.md:10 — (a) contradiction" "$M/l.txt"
has "L renamed file's finding under the new path" "agents/b.md:14 — (a) contradiction" "$M/l.txt"
hasnt "L deleted file not reported" "beta.md:" "$M/l.txt"
hasnt "L no traceback" "Traceback" "$M/l.txt"

# M/N: one inbox note per run; the same finding on a later day is deduped (first kept, last/line updated)
out=$(mr m 2026-11-02T20:00:00 --since "$M0" --cron 2>&1); rc=$?
[ "$rc" = 0 ] && [ -z "$out" ] && ok "N --cron run with findings: exit 0, silent" || no "N cron run ($rc: $out)"
[ "$(notes)" = 1 ] && ok "N three new findings in three files: ONE inbox note" || no "N one note ($(notes))"
nf=$(grep -c '^- `' "$M/inbox"/self-review-*.md)
[ "$nf" -ge 3 ] && ok "N the one note lists every new finding ($nf)" || no "N note findings ($nf)"
[ "$(mj m s1/SKILL first)" = 2026-11-02 ] && ok "M first-seen day stored" || no "M first ($(mj m s1/SKILL first))"
{ printf -- '---\nname: s1\ndescription: demo\n---\nNew line A is plain.\nNew line B is plain.\n'
  sed -n '5,$p' "$MW/.claude/skills/s1/SKILL.md"; } > "$M/s1.tmp"; mv "$M/s1.tmp" "$MW/.claude/skills/s1/SKILL.md"
mcommit m2
mr m 2026-11-03T20:00:00 --since "$M0" --cron >/dev/null 2>&1
[ "$(notes)" = 1 ] && ok "M same findings re-found next day: no second note" || no "M re-found note ($(notes))"
[ "$(mj m s1/SKILL first)" = 2026-11-02 ] && ok "M re-found finding keeps its first day" \
  || no "M first kept ($(mj m s1/SKILL first))"
[ "$(mj m s1/SKILL last)" = 2026-11-03 ] && ok "M re-found finding: last = today" || no "M last ($(mj m s1/SKILL last))"
[ "$(mj m s1/SKILL line)" = 12 ] && ok "M moved line: stored line number follows (10 -> 12)" \
  || no "M line ($(mj m s1/SKILL line))"
n_ids=$(python3 -I -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$M/m/open.json")
echo "PLANT three: keep the helper list short." >> "$MW/workspace/TOOLS.md"; mcommit m3
mr m 2026-11-04T20:00:00 --cron >/dev/null 2>&1
[ "$(notes)" = 2 ] && ok "M a new finding on day 3: one more note" || no "M day-3 note ($(notes))"
last_note=$(ls -t "$M/inbox"/self-review-*.md | head -1)
has "M day-3 note lists the new finding" "TOOLS.md:7" "$last_note"
hasnt "M day-3 note does not repeat older findings" "s1/SKILL.md" "$last_note"
[ "$(grep -c '^- `' "$last_note")" = 1 ] && ok "M day-3 note holds exactly 1 finding" \
  || no "M day-3 note findings ($(grep -c '^- `' "$last_note"))"

# P: empty diff -> silence
c0=$(calls); n0=$(notes)
out=$(mr m 2026-11-04T21:00:00 --cron 2>&1); rc=$?
[ "$rc" = 0 ] && [ -z "$out" ] && ok "P no new commits: exit 0, no output" || no "P empty diff ($rc: $out)"
[ "$(notes)" = "$n0" ] && ok "P no new commits: no note" || no "P note ($(notes))"
[ "$(calls)" = "$c0" ] && ok "P no new commits: no LLM call" || no "P LLM calls ($(calls) vs $c0)"
has "P report says nothing changed" "Changed: 0 files (none)" "$M/reports/2026-11-04.md"
echo "Other line two." >> "$MW/workspace/OTHER.md"; printf '#!/bin/sh\necho v3\n' > "$MW/bin/tool"; mcommit m4
out=$(mr m 2026-11-04T22:00:00 --cron 2>&1); rc=$?
[ "$rc" = 0 ] && [ -z "$out" ] && [ "$(notes)" = "$n0" ] && [ "$(calls)" = "$c0" ] \
  && ok "P out-of-scope-only commit: silent, no note, no LLM call" || no "P out-of-scope ($rc: $out; $(notes); $(calls))"
[ "$(cat "$M/m/last")" = "$(mg rev-parse HEAD)" ] && ok "P out-of-scope commit still advances state/last" \
  || no "P last not advanced"

# O: open / ack
out=$(mr o-none 2026-11-04T20:00:00 open 2>&1); rc=$?
[ "$rc" = 0 ] && [ "$out" = "self-review: no open findings" ] && ok "O open without any state" || no "O open fresh ($rc: $out)"
[ "$(mr o-none 2026-11-04T20:00:00 open --json)" = "[]" ] && ok "O open --json without state: []" || no "O open --json fresh"
mr m 2026-11-04T20:00:00 open > "$M/o1.txt"
has "O open lists a finding with its first day" "(since 2026-11-02)" "$M/o1.txt"
has "O open header counts findings" "open finding(s)" "$M/o1.txt"
nj=$(mr m 2026-11-04T20:00:00 open --json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
[ "$nj" -ge 3 ] && ok "O open --json lists the open findings ($nj)" || no "O open --json ($nj)"
out=$(mr m 2026-11-05T09:00:00 ack nosuchid 2>"$M/o-err.txt"); rc=$?
[ "$rc" = 0 ] && [ "$out" = "self-review: acked 0" ] && ok "O ack unknown id: acked 0, exit 0" || no "O ack unknown ($rc: $out)"
has "O ack unknown id named on stderr" "unknown id nosuchid" "$M/o-err.txt"
tid=$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print([k for k,v in d.items() if "TOOLS" in v["file"] and "line" in v and v["line"]==7][0])' "$M/m/open.json")
[ "$(mr m 2026-11-05T09:00:00 ack "$tid")" = "self-review: acked 1" ] && ok "O ack ID" || no "O ack ID"
[ "$(mr m 2026-11-06T09:00:00 ack "$tid")" = "self-review: acked 0" ] && ok "O ack twice: acked 0" || no "O ack twice"
acked=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]["acked"])' "$M/m/open.json" "$tid")
[ "$acked" = 2026-11-05 ] && ok "O second ack keeps the first ack date" || no "O ack date ($acked)"
mr m 2026-11-05T09:00:00 open > "$M/o2.txt"; hasnt "O acked finding hidden from open" "$tid" "$M/o2.txt"
mr m 2026-11-05T09:00:00 --cron >/dev/null 2>&1
[ "$(notes)" = "$n0" ] && ok "O run after ack: no note" || no "O run after ack ($(notes))"
cp "$MW/.claude/skills/s1/SKILL.md" "$M/s1.bak"; grep -v "PLANT one" "$M/s1.bak" > "$MW/.claude/skills/s1/SKILL.md"
mr m 2026-11-05T09:00:00 open > "$M/o3.txt"
hasnt "O line removed in the work tree (uncommitted): no longer open" "s1/SKILL.md" "$M/o3.txt"
cp "$M/s1.bak" "$MW/.claude/skills/s1/SKILL.md"
mr m 2026-11-05T09:00:00 ack all > "$M/o4.txt"
[ "$(mr m 2026-11-05T09:00:00 open)" = "self-review: no open findings" ] && ok "O ack all: nothing open" \
  || no "O ack all ($(cat "$M/o4.txt"))"
mr m 2026-12-08T09:00:00 --cron >/dev/null 2>&1
nacked=$(python3 -I -c 'import json,sys; print(sum(1 for v in json.load(open(sys.argv[1])).values() if v.get("acked")))' "$M/m/open.json")
[ "$nacked" = 0 ] && ok "O acked entries dropped once last seen > 30 days ago" || no "O acked kept ($nacked)"

# Q: error handling
n0=$(notes)
out=$(MB=$M/missing.git mr q1 2026-11-04T20:00:00 --cron 2>&1); rc=$?
[ "$rc" = 2 ] && ok "Q missing brain repo: exit 2" || no "Q missing brain rc ($rc)"
case $out in "self-review: git rev-parse"*) ok "Q missing brain: one-line error";; *) no "Q missing brain msg ($out)";; esac
[ ! -f "$M/q1/last" ] && [ "$(notes)" = "$n0" ] && ok "Q missing brain: no state, no note" || no "Q missing brain wrote"
git init -q --bare "$M/empty.git"
out=$(MB=$M/empty.git mr q2 2026-11-04T20:00:00 --cron 2>&1); rc=$?
[ "$rc" = 2 ] && ! grep -q Traceback <<<"$out" && ok "Q brain without commits: exit 2, no traceback" || no "Q empty brain ($rc: $out)"
mkdir -p "$M/q3"; echo 0123456789abcdef0123456789abcdef01234567 > "$M/q3/last"
mr q3 2026-11-04T20:00:00 --dry-run --no-llm > "$M/q3.txt" 2>&1
has "Q stale state/last (unknown commit): falls back to the first-run window" "first run (1 day)" "$M/q3.txt"
mkdir -p "$M/q4"
out=$(flock "$M/q4/.lock" env M_DIR=$M SELF_REVIEW_NOW=2026-11-04T20:00:00 BRAIN_GIT_DIR=$M/brain.git BRAIN_WORK_TREE=$MW \
  SELF_REVIEW_STATE=$M/q4 SELF_REVIEW_REPORTS=$M/q4r SELF_REVIEW_INBOX=$M/inbox SELF_REVIEW_LLM=$M/m-llm \
  "$SR" --since "$M0" 2>&1); rc=$?
[ "$rc" = 0 ] && [ "$out" = "self-review: another run is active" ] && ok "Q lock held: skips with a message, exit 0" \
  || no "Q lock ($rc: $out)"
[ ! -e "$M/q4r" ] && [ ! -f "$M/q4/last" ] && [ "$(notes)" = "$n0" ] && ok "Q lock held: writes nothing" || no "Q lock wrote"
qmode() {  # qmode MODE EXPECTED-NOTE
  MLLM=$1 mr q5 2026-11-04T20:00:00 --since "$M0" --dry-run > "$M/q5-$1.txt" 2>&1; local rc=$?
  if [ "$rc" = 0 ] && grep -qF -- "$2" "$M/q5-$1.txt" && ! grep -q Traceback "$M/q5-$1.txt"; then
    ok "Q LLM $1: run completes, noted as '$2'"; else no "Q LLM $1 ($rc: $(grep -m1 -e 'LLM check' -e Traceback "$M/q5-$1.txt"))"; fi
}
qmode garbage "invalid JSON"; qmode exit4 "bad response"; qmode exit5 ": timeout"
has "Q LLM failure: failed calls counted" "0 calls ok, 3 failed" "$M/q5-exit4.txt"
MLLM_BIN=$M/no-such-llm mr q5 2026-11-04T20:00:00 --since "$M0" --dry-run > "$M/q5-missing.txt" 2>&1
has "Q LLM binary missing: noted, no crash" "cannot run llm-local (FileNotFoundError)" "$M/q5-missing.txt"
c0=$(calls); MLLM=unavail mr q5 2026-11-04T20:00:00 --since "$M0" --dry-run > "$M/q5-unavail.txt" 2>&1
[ $(( $(calls) - c0 )) = 2 ] && ok "Q LLM unavailable twice: no third call" || no "Q unavailable calls ($(( $(calls) - c0 )))"
has "Q LLM unavailable: noted once for the rest" "local LLM unavailable: the remaining LLM checks were skipped" "$M/q5-unavail.txt"
has "Q LLM unavailable: rest counted as skipped" "2 failed, 1 skipped" "$M/q5-unavail.txt"
MLLM=weird mr q5 2026-11-04T20:00:00 --since "$M0" --dry-run > "$M/q5-weird.txt" 2>&1
has "Q LLM unparsable line number: finding at the chunk's first line" "s1/SKILL.md:5 — (a) contradiction: LLM: weird bad line" "$M/q5-weird.txt"
hasnt "Q LLM unknown check ignored" "unknown check kind" "$M/q5-weird.txt"
hasnt "Q LLM items beyond 3 ignored" "weird fourth item" "$M/q5-weird.txt"
hasnt "Q LLM weird output: no traceback" "Traceback" "$M/q5-weird.txt"
MLLM=far mr q5 2026-11-04T20:00:00 --since "$M0" --dry-run > "$M/q5-far.txt" 2>&1
has "Q LLM out-of-range line: snapped to the nearest changed line" "s1/SKILL.md:12 — (a) contradiction: LLM: far line" "$M/q5-far.txt"
mkdir -p "$M/q6"; echo '{not json' > "$M/q6/open.json"
mr q6 2026-11-04T20:00:00 --since "$M0" --cron --no-llm > "$M/q6.txt" 2>&1; rc=$?
[ "$rc" = 0 ] && python3 -I -c 'import json,sys; assert isinstance(json.load(open(sys.argv[1])), dict)' "$M/q6/open.json" 2>/dev/null \
  && ok "Q corrupt open.json: run starts fresh and rewrites it" || no "Q corrupt open.json ($rc: $(head -c 200 "$M/q6.txt"))"
echo '[1, 2]' > "$M/q6/open.json"
for c in "--cron --no-llm" "open" "ack all"; do
  mr q6 2026-11-04T20:00:00 $c > "$M/q6b.txt" 2>&1; rc=$?
  [ "$rc" = 0 ] && ! grep -q Traceback "$M/q6b.txt" && ok "Q open.json holding a list: '$c' works" \
    || no "Q open.json list, $c ($rc: $(grep -m1 Error "$M/q6b.txt"))"
done

# ---------------------------------------------------------------- S: step (h) setup health (LOOP_HEALTH_FIXTURE seam)
# The pass itself is tested in the repo (tests/loop/test_health.py); here: findings in the report and the note, new
# vs open, recomputed (gone = fixed), the off switch. Own state dirs, no Phoenix writes, never the live loop state.
S=$T/sh; mkdir -p "$S/inbox" "$S/reports"
cat > "$S/fx1.json" <<'EOF'
{"findings": [
 {"key": "land:shared-red:ci", "source": "land", "metric": "blocked_min", "now": 25, "base": 0, "sev": 3,
  "msg": "every land was blocked 25 min in 24 h (ci; 1 episode(s))", "fix": "fix main's red suite"},
 {"key": "phoenix:err:gateway.x", "source": "phoenix", "metric": "error %", "now": 40, "base": 0, "sev": 2,
  "msg": "gateway.x: 4 of 10 spans failed · trace http://phx.invalid/projects/P/traces/abc", "fix": "open the trace"}],
 "readings": {"land": {"line": "9 lands, 3 failed", "secs": 0.1}}, "clusters": [
 {"source": "land", "sig": "land ci", "n": 3, "base": null, "new": true}], "errors": {"slo": "not found"}, "secs": 0.2}
EOF
python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); d["findings"]=d["findings"][:1]; json.dump(d,open(sys.argv[2],"w"))' "$S/fx1.json" "$S/fx2.json"
sr_h() {  # sr_h FIXTURE ARGS...: one run with step (h) on, its own state; never the live loop state, no Phoenix writes
  SELF_REVIEW_HEALTH=${HEALTH_RUN_MODE:-on} LOOP_HEALTH_FIXTURE=$1 LOOP_STATE=$S/loop HEALTH_PHOENIX_WRITES=off SELF_REVIEW_LOOP=off \
    SELF_REVIEW_STATE=$S/state SELF_REVIEW_REPORTS=$S/reports SELF_REVIEW_INBOX=$S/inbox "$SR" "${@:2}"
}
sr_h "$S/fx1.json" --since "$BASE" --cron --no-llm > "$S/r1.txt" 2>&1
R1=$(ls -t "$S/reports"/*.md 2>/dev/null | head -1)
has "S (h) section in the report" "## (h) Setup health" "${R1:-/dev/null}"
has "S (h) finding listed with its source" "health:land — (h) setup health: every land was blocked 25 min" "${R1:-/dev/null}"
has "S (h) Phoenix finding keeps its trace link" "traces/abc" "${R1:-/dev/null}"
has "S (h) unavailable source named" "unavailable: slo (not found)" "${R1:-/dev/null}"
has "S (h) clustered signature listed" "land: land ci ×3 (new)" "${R1:-/dev/null}"
N1=$(ls "$S/inbox" | grep -c '^self-review-')
if [ "$N1" = 1 ] && grep -q "setup's health" "$S/inbox"/self-review-*.md; then ok "S new (h) findings: one inbox note"
else no "S new (h) findings: inbox note ($N1)"; fi
if [ -e "$S/loop/health/latest.json" ]; then ok "S latest.json for the loop review"; else no "S latest.json missing"; fi
if grep -q "Traceback" "$S/r1.txt"; then no "S traceback ($(grep -m1 Error "$S/r1.txt"))"; else ok "S no traceback"; fi
sr_h "$S/fx1.json" --since "$BASE" --cron --no-llm > /dev/null 2>&1
if [ "$(ls "$S/inbox" | grep -c '^self-review-')" = 1 ]; then ok "S same findings again: no second note"
else no "S same findings again: a second note"; fi
sr_h "$S/fx2.json" --since "$BASE" --cron --no-llm > /dev/null 2>&1
SELF_REVIEW_STATE=$S/state "$SR" open > "$S/open.txt" 2>&1
has "S still-present (h) finding open" "every land was blocked 25 min" "$S/open.txt"
hasnt "S gone (h) finding closed (recomputed)" "gateway.x" "$S/open.txt"
HEALTH_RUN_MODE=off sr_h "$S/fx1.json" --since "$BASE" --dry-run --no-llm > "$S/off.txt" 2>&1
hasnt "S SELF_REVIEW_HEALTH=off skips step (h)" "(h) Setup health" "$S/off.txt"

# ---------------------------------------------------------------- H: live local LLM (optional)
if [ "${SELF_REVIEW_LIVE:-0}" = 1 ]; then
  SECONDS=0
  SELF_REVIEW_LLM=/home/node/bin/llm-local "$SR" --since "$BASE" --dry-run --max-llm 1 > "$T/live.txt" 2>&1
  if grep -q "LLM: 1 calls ok" "$T/live.txt"; then ok "H live LLM call ok (${SECONDS}s)"; else
    no "H live LLM call ($(grep -m1 'LLM' "$T/live.txt"))"; fi
  grep -q "LLM:.*ask\|LLM:.*spend\|LLM:.*permission\|LLM:.*owner" "$T/live.txt" && echo "info - live LLM flagged the planted contradiction" \
    || echo "info - live LLM did not flag the planted contradiction"
fi

echo "---"; echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
