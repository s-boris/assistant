#!/usr/bin/env bash
# Offline tests for ~/bin/vidmontage (clips + photos + music -> one montage with ffmpeg; built for the owner's wedding
# video). Exit 0 = all pass. VIDMONTAGE_BIN=<path> tests another copy. vidmontage only writes its -o file and a temp
# dir, so no refusal guard is needed; TMPDIR and HOME are temp dirs, and VIDMONTAGE_FONT points at a fixture font.
# Part 1, ffmpeg/ffprobe PATH shims (python): fixture "videos" are text files "dur=5.0 audio=1"; the ffmpeg shim logs
# every argv as JSON, writes "dur=… audio=1" into each normalized part (photo: its -t) and fails on request; ffprobe
# answers duration/audio from those files. Checks the exact commands and filter graph: normalize (video with/without
# audio, photo), xfade offsets, fade clamp, single part, music mix, --music-only (bug: acrossfade output left
# unconnected), title escaping + missing font, --size/--fade/--photo-secs validation, --dir filtering, missing inputs or
# output folder fail before any work, ffmpeg failures (temp dir removed).
# Part 2 (skipped with VIDMONTAGE_NO_REAL=1 or without ffmpeg): the real, local ffmpeg on tiny generated clips, so
# the filter graphs are proven valid, not just well-formed (each run capped at 30 s).
set -uo pipefail
V=${VIDMONTAGE_BIN:-$(cd "$(dirname "$0")/../../../scripts" && pwd)/vidmontage}
T=$(mktemp -d "${TMPDIR:-/tmp}/vidmontage-tests.XXXXXX")
trap 'rm -rf "$T"' EXIT
REAL_PATH=$PATH
export T FAKE=$T/fake HOME=$T/home TMPDIR=$T/tmp VIDMONTAGE_FONT=$T/font.ttf
mkdir -p "$T/shims" "$FAKE" "$HOME" "$TMPDIR" "$T/in" "$T/out"
touch "$VIDMONTAGE_FONT"
cat > "$T/shims/ffmpeg" <<'EOF'
#!/usr/bin/env python3
import json, os, sys
fake = os.environ["FAKE"]; a = sys.argv[1:]
n = sum(1 for _ in open(f"{fake}/ffmpeg.log")) + 1 if os.path.exists(f"{fake}/ffmpeg.log") else 1
open(f"{fake}/ffmpeg.log", "a").write(json.dumps(a) + "\n")
if os.path.exists(f"{fake}/ffmpeg-fail-{n}"):
    print("Error while filtering: Invalid argument (fake)", file=sys.stderr); sys.exit(1)
out = a[-1]
if "-filter_complex" in a:
    open(out, "w").write("final t=" + a[a.index("-t") + 1] + "\n")
else:
    src = a[a.index("-i") + 1]
    if "-loop" in a:
        dur = a[a.index("-t") + 1]
    else:
        dur = dict(kv.split("=") for kv in open(src).read().split())["dur"]
    open(out, "w").write(f"dur={dur} audio=1\n")
EOF
cat > "$T/shims/ffprobe" <<'EOF'
#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]; path = a[-1]
open(os.environ["FAKE"] + "/ffprobe.log", "a").write(json.dumps(a) + "\n")
try:
    d = dict(kv.split("=") for kv in open(path).read().split())
except (OSError, ValueError):
    print(f"{path}: Invalid data found when processing input", file=sys.stderr); sys.exit(1)
if "format=duration" in a:
    print(json.dumps({"format": {"duration": d["dur"]}}))
elif "a" in a and "-select_streams" in a:
    print("0" if d.get("audio") == "1" else "")
EOF
chmod +x "$T/shims/ffmpeg" "$T/shims/ffprobe"

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
no() { fail=$((fail + 1)); echo "FAIL - $1"; [ -n "${2:-}" ] && printf '       got: %s\n' "$2" | head -30; }
eq() { if [ "$2" = "$3" ]; then ok "$1"; else no "$1" "$2 (want: $3)"; fi; }
has() { if [[ "$2" == *"$3"* ]]; then ok "$1"; else no "$1" "$2"; fi; }
hasnt() { if [[ "$2" == *"$3"* ]]; then no "$1" "$2"; else ok "$1"; fi; }
run() { (cd "$T/out" && PATH=$T/shims:$REAL_PATH "$V" "$@") > "$T/stdout" 2> "$T/stderr"; rc=$?; out=$(cat "$T/stdout"); err=$(cat "$T/stderr"); }
reset() { rm -f "$FAKE"/* "$T/out"/*; }
calls() { [ -s "$FAKE/ffmpeg.log" ] && wc -l < "$FAKE/ffmpeg.log" || echo 0; }
# argv of ffmpeg call N, args joined with spaces; the value after FLAG in call N
argv() { python3 -c 'import json,sys; print(" ".join(json.loads(open(sys.argv[1]).readlines()[int(sys.argv[2])-1])))' "$FAKE/ffmpeg.log" "$1"; }
val() { python3 -c 'import json,sys; a=json.loads(open(sys.argv[1]).readlines()[int(sys.argv[2])-1]); print(a[a.index(sys.argv[3])+1])' "$FAKE/ffmpeg.log" "$1" "$2"; }
probes() { grep -c "$1" "$FAKE/ffprobe.log" 2>/dev/null || echo 0; }
vid() { echo "dur=$2 audio=$3" > "$T/in/$1"; }
VF='scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p'
ENC='-c:v libx264 -preset veryfast -crf 20 -c:a aac -ar 48000 -ac 2'
vid a.mp4 5.0 1; vid b.mov 3.0 0; vid c.mp4 6.0 1; vid short.mp4 1.0 1; echo jpeg > "$T/in/p.jpg"; vid song.mp3 200 1

echo "# two videos (with and without audio), default fade"
reset; run -o "$T/out/m.mp4" "$T/in/a.mp4" "$T/in/b.mov"
eq "rc 0" "$rc" "0"
eq "3 ffmpeg calls (2 parts + final)" "$(calls)" "3"
p1=$(python3 -c 'import json,sys; print(json.loads(open(sys.argv[1]).readline())[-1])' "$FAKE/ffmpeg.log")
eq "video with audio: own audio" "$(argv 1)" "-y -i $T/in/a.mp4 -vf $VF -map 0:v:0 -map 0:a:0 $ENC $p1"
[[ $p1 == $TMPDIR/vidmontage-*/p000.mp4 ]] && ok "parts in a vidmontage-* temp dir under \$TMPDIR" || no "part path" "$p1"
has "video without audio: silent track, -shortest" "$(argv 2)" "-y -i $T/in/b.mov -f lavfi -i anullsrc=r=48000:cl=stereo -vf $VF -map 0:v:0 -map 1:a -shortest $ENC "
eq "has_audio probed per video" "$(probes select_streams)" "2"
eq "duration probed per part" "$(probes format=duration)" "2"
eq "filter graph" "$(val 3 -filter_complex)" "[0:v][1:v]xfade=transition=fade:duration=0.8:offset=4.200[v1];[0:a][1:a]acrossfade=d=0.8[a1];[a1]loudnorm=I=-16:TP=-1.5[aout];[v1]null[vout]"
eq "-t total = 5 + 3 - 0.8" "$(val 3 -t)" "7.200"
has "final encode" "$(argv 3)" "-map [vout] -map [aout] -c:v libx264 -preset medium -crf 20 -pix_fmt yuv420p -c:a aac -b:a 192k -movflags +faststart -t 7.200 $T/out/m.mp4"
eq "output written" "$(cat "$T/out/m.mp4")" "final t=7.200"
has "progress 1" "$err" "[1/2] a.mp4 5.0s"
has "progress 2" "$err" "[2/2] b.mov 3.0s"
has "summary" "$err" "$T/out/m.mp4: 2 parts, 7.2s"
eq "stdout empty" "$out" ""
eq "temp dir removed" "$(ls -A "$TMPDIR")" ""

echo "# photo: a still of --photo-secs with a silent track, never probed for audio"
reset; run -o "$T/out/m.mp4" --photo-secs 2.5 "$T/in/p.jpg" "$T/in/a.mp4"
eq "rc 0" "$rc" "0"
has "photo command" "$(argv 1)" "-y -loop 1 -t 2.5 -i $T/in/p.jpg -f lavfi -t 2.5 -i anullsrc=r=48000:cl=stereo -vf $VF -map 0:v -map 1:a $ENC "
eq "only the video probed for audio" "$(probes select_streams)" "1"
has "photo progress" "$err" "[1/2] p.jpg 2.5s"
reset; cp "$T/in/p.jpg" "$T/in/P.HEIC"; run -o "$T/out/m.mp4" "$T/in/P.HEIC"
has "extension case-insensitive (HEIC = photo, default 4 s)" "$(argv 1)" "-loop 1 -t 4.0 -i $T/in/P.HEIC"
rm "$T/in/P.HEIC"

echo "# three parts: cumulative xfade offsets"
reset; run -o "$T/out/m.mp4" --fade 1 "$T/in/b.mov" "$T/in/a.mp4" "$T/in/c.mp4"
eq "filter graph" "$(val 4 -filter_complex)" "[0:v][1:v]xfade=transition=fade:duration=1.0:offset=2.000[v1];[0:a][1:a]acrossfade=d=1.0[a1];[v1][2:v]xfade=transition=fade:duration=1.0:offset=6.000[v2];[a1][2:a]acrossfade=d=1.0[a2];[a2]loudnorm=I=-16:TP=-1.5[aout];[v2]null[vout]"
eq "-t = 14 - 2" "$(val 4 -t)" "12.000"

echo "# fade clamped to half the shortest part; --fade 0; single part"
reset; run -o "$T/out/m.mp4" "$T/in/a.mp4" "$T/in/short.mp4"
has "fade 0.5" "$(val 3 -filter_complex)" "xfade=transition=fade:duration=0.5:offset=4.500[v1]"
reset; run -o "$T/out/m.mp4" --fade 0 "$T/in/a.mp4" "$T/in/b.mov"
eq "--fade 0: rc 0, -t = 8" "$rc|$(val 3 -t)" "0|8.000"
eq "--fade 0: concat, not a 0 s xfade (which drops all but the first part)" "$(val 3 -filter_complex)" "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[vc][ac];[ac]loudnorm=I=-16:TP=-1.5[aout];[vc]null[vout]"
reset; run -o "$T/out/m.mp4" --fade 0 --music "$T/in/song.mp3" --music-only "$T/in/a.mp4" "$T/in/b.mov" "$T/in/c.mp4"
eq "--fade 0 --music-only: video-only concat" "$(val 4 -filter_complex)" "[0:v][1:v][2:v]concat=n=3:v=1:a=0[vc];[3:a]atrim=0:14.000,afade=t=out:st=11.000:d=3,volume=1.0[m];[m]loudnorm=I=-16:TP=-1.5[aout];[vc]null[vout]"
reset; run -o "$T/out/m.mp4" "$T/in/a.mp4"
eq "single part graph" "$(val 2 -filter_complex)" "[0:a]loudnorm=I=-16:TP=-1.5[aout];[0:v]null[vout]"
eq "single part -t" "$(val 2 -t)" "5.000"
reset; run -o "$T/out/m.mp4" --photo-secs 2 "$T/in/p.jpg" "$T/in/b.mov"
eq "nothing audible (photo + mute clip, no music): no loudnorm, whose NaN on silence ffmpeg 7's AAC refuses" \
  "$(val 3 -filter_complex)" "[0:v][1:v]xfade=transition=fade:duration=0.8:offset=1.200[v1];[0:a][1:a]acrossfade=d=0.8[a1];[a1]anull[aout];[v1]null[vout]"

echo "# --music: looped, trimmed, faded, mixed under the clip audio"
reset; run -o "$T/out/m.mp4" --music "$T/in/song.mp3" "$T/in/a.mp4" "$T/in/b.mov"
eq "rc 0" "$rc" "0"
has "music input looped, last" "$(argv 3)" "-stream_loop -1 -i $T/in/song.mp3 -filter_complex"
eq "graph" "$(val 3 -filter_complex)" "[0:v][1:v]xfade=transition=fade:duration=0.8:offset=4.200[v1];[0:a][1:a]acrossfade=d=0.8[a1];[2:a]atrim=0:7.200,afade=t=out:st=4.200:d=3,volume=0.35[m];[a1][m]amix=inputs=2:duration=first:normalize=0[am];[am]loudnorm=I=-16:TP=-1.5[aout];[v1]null[vout]"
reset; run -o "$T/out/m.mp4" --music "$T/in/song.mp3" "$T/in/short.mp4"
has "total < 3 s: fade starts at 0" "$(val 2 -filter_complex)" "afade=t=out:st=0.000:d=3"

echo "# --music-only: music replaces the clip audio; no dangling acrossfade (was: ffmpeg 'unconnected output')"
reset; run -o "$T/out/m.mp4" --music "$T/in/song.mp3" --music-only "$T/in/a.mp4" "$T/in/b.mov" "$T/in/c.mp4"
eq "rc 0" "$rc" "0"
eq "graph" "$(val 4 -filter_complex)" "[0:v][1:v]xfade=transition=fade:duration=0.8:offset=4.200[v1];[v1][2:v]xfade=transition=fade:duration=0.8:offset=6.400[v2];[3:a]atrim=0:12.400,afade=t=out:st=9.400:d=3,volume=1.0[m];[m]loudnorm=I=-16:TP=-1.5[aout];[v2]null[vout]"
reset; run -o "$T/out/m.mp4" --music-only "$T/in/a.mp4" "$T/in/b.mov"
has "--music-only without --music: clip audio kept" "$(val 3 -filter_complex)" "[0:a][1:a]acrossfade=d=0.8[a1];[a1]loudnorm"

echo "# --title: drawtext on the first 4 s, escaped"
reset; run -o "$T/out/m.mp4" --title "PersonG & Anna: it's 100\\" "$T/in/a.mp4" "$T/in/b.mov"
eq "graph" "$(val 3 -filter_complex)" "[0:v][1:v]xfade=transition=fade:duration=0.8:offset=4.200[v1];[0:a][1:a]acrossfade=d=0.8[a1];[v1]drawtext=fontfile=$VIDMONTAGE_FONT:text='PersonG & Anna\\: it’s 100\\\\':fontcolor=white:fontsize=h/14:box=1:boxcolor=black@0.4:boxborderw=20:x=(w-tw)/2:y=h*0.8:enable='lt(t,4)'[vt];[a1]loudnorm=I=-16:TP=-1.5[aout];[vt]null[vout]"
reset; VIDMONTAGE_FONT=$T/nofont.ttf run -o "$T/out/m.mp4" --title "Hi" "$T/in/a.mp4"
eq "font missing: rc 0, no drawtext" "$rc|$(val 2 -filter_complex)" "0|[0:a]loudnorm=I=-16:TP=-1.5[aout];[0:v]null[vout]"
has "font missing: warned" "$err" "warning: font $T/nofont.ttf missing, --title skipped"

echo "# --size"
reset; run -o "$T/out/m.mp4" --size 1280x720 "$T/in/a.mp4"
has "scale/pad to --size" "$(argv 1)" "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:"
for bad in big 1920 1921x1080 0x0 -2x4 axb; do
  reset; run -o "$T/out/m.mp4" --size "$bad" "$T/in/a.mp4"
  eq "--size $bad: rc 2, no ffmpeg" "$rc|$(calls)" "2|0"
done
has "says why" "$err" "bad size"

echo "# --fade / --photo-secs validation"
reset; run -o "$T/out/m.mp4" --fade -1 "$T/in/a.mp4"; eq "--fade -1: rc 2, no ffmpeg" "$rc|$(calls)" "2|0"
reset; run -o "$T/out/m.mp4" --photo-secs 0 "$T/in/p.jpg"; eq "--photo-secs 0: rc 2" "$rc|$(calls)" "2|0"
reset; run -o "$T/out/m.mp4" --fade nan "$T/in/a.mp4"; eq "--fade nan: rc 2" "$rc|$(calls)" "2|0"

echo "# --dir: sorted, photos/videos only, hidden files and folders skipped; FILE args first"
mkdir -p "$T/d/sub" "$T/d/old.mov"; vid 02.mp4 2.0 1; mv "$T/in/02.mp4" "$T/d/"; vid 01.MOV 3.0 0; mv "$T/in/01.MOV" "$T/d/"
echo jpeg > "$T/d/03.jpg"; echo x > "$T/d/.hidden.mp4"; echo notes > "$T/d/notes.txt"; echo '{}' > "$T/d/meta.json"
reset; run -o "$T/out/m.mp4" "$T/in/a.mp4" --dir "$T/d"
eq "rc 0" "$rc" "0"
eq "order: FILE, then the folder sorted" "$(for i in 1 2 3 4; do val $i -i; done | tr '\n' ' ')" "$T/in/a.mp4 $T/d/01.MOV $T/d/02.mp4 $T/d/03.jpg "
has "skips notes.txt (said on stderr)" "$err" "skipped (not a photo/video): $T/d/notes.txt"
has "skips meta.json" "$err" "skipped (not a photo/video): $T/d/meta.json"
has "skips the subfolder" "$err" "skipped (not a photo/video): $T/d/sub"
has "skips a folder named like a video" "$err" "skipped (not a photo/video): $T/d/old.mov"
hasnt "hidden file silently ignored" "$err" ".hidden"
mkdir -p "$T/e"; echo x > "$T/e/readme.txt"
reset; run -o "$T/out/m.mp4" --dir "$T/e"; eq "folder without media: rc 1, no input files" "$rc|$err|$(calls)" "1|skipped (not a photo/video): $T/e/readme.txt
no input files|0"

echo "# --underwater: every clip/photo goes through uwfix first (shim copies input → -o and logs argv)"
cat > "$T/shims/uwfix" <<'UWEOF'
#!/usr/bin/env python3
import json, os, shutil, sys
a = sys.argv[1:]
open(os.environ["FAKE"] + "/uwfix.log", "a").write(json.dumps(a) + "\n")
if os.path.exists(os.environ["FAKE"] + "/uwfix-fail"):
    print("uwfix: no video/image stream (fake)", file=sys.stderr); sys.exit(1)
shutil.copy(a[0], a[a.index("-o") + 1])
UWEOF
chmod +x "$T/shims/uwfix"
echo heic > "$T/in/h.heic"
reset; rm -f "$FAKE/uwfix.log"; VIDMONTAGE_UWFIX=$T/shims/uwfix run -o "$T/out/u.mp4" --underwater --uw-strength 0.6 "$T/in/a.mp4" "$T/in/p.jpg" "$T/in/h.heic"
eq "underwater: rc 0" "$rc" "0"
eq "underwater: uwfix per supported input" "$(wc -l < "$FAKE/uwfix.log")" "2"
uw1=$(python3 -c 'import json,sys; print(" ".join(json.loads(open(sys.argv[1]).readlines()[0])))' "$FAKE/uwfix.log")
[[ $uw1 == "$T/in/a.mp4 -o $TMPDIR/vidmontage-"*"/uw000.mp4 --quiet --strength 0.6" ]] && ok "underwater: uwfix argv (video → .mp4)" || no "uwfix argv" "$uw1"
uw2=$(python3 -c 'import json,sys; print(" ".join(json.loads(open(sys.argv[1]).readlines()[1])))' "$FAKE/uwfix.log")
[[ $uw2 == "$T/in/p.jpg -o $TMPDIR/vidmontage-"*"/uw001.jpg --quiet --strength 0.6" ]] && ok "underwater: photo stays a photo (.jpg)" || no "uwfix photo argv" "$uw2"
has "underwater: corrected file is normalized" "$(argv 1)" "/uw000.mp4 -vf $VF"
has "underwater: corrected photo loops as a still" "$(argv 2)" "-loop 1 -t 4.0 -i $TMPDIR/vidmontage-"
has "underwater: heic skipped with a note" "$err" "underwater fix skipped (unsupported format): $T/in/h.heic"
eq "underwater: temp dir removed" "$(ls -A "$TMPDIR")" ""
reset; touch "$FAKE/uwfix-fail"; VIDMONTAGE_UWFIX=$T/shims/uwfix run -o "$T/out/u.mp4" --underwater "$T/in/a.mp4"
eq "underwater: uwfix failure exits 1" "$rc" "1"
has "underwater: failure names the file" "$err" "uwfix failed on $T/in/a.mp4"
eq "underwater: no ffmpeg after a uwfix failure" "$(calls)" "0"
rm -f "$FAKE/uwfix-fail"
reset; VIDMONTAGE_UWFIX=$T/nope run -o "$T/out/u.mp4" --underwater "$T/in/a.mp4"
has "underwater: missing uwfix fails before any work" "$err" "--underwater needs uwfix"
eq "underwater: missing uwfix → no ffmpeg" "$(calls)" "0"
reset; run -o "$T/out/u.mp4" --underwater --uw-strength 1.5 "$T/in/a.mp4"
has "underwater: strength > 1 refused" "$err" "--uw-strength must be 0–1"

echo "# fail before any work"
reset; run -o "$T/out/m.mp4"; eq "no inputs: rc 1" "$rc|$err" "1|no input files"
reset; run "$T/in/a.mp4"; eq "-o missing: rc 2 (usage)" "$rc|$(calls)" "2|0"
reset; run -o "$T/out/m.mp4" "$T/in/a.mp4" "$T/in/gone.mp4"; eq "missing input: rc 1, no ffmpeg" "$rc|$err|$(calls)" "1|no such file: $T/in/gone.mp4|0"
reset; run -o "$T/out/m.mp4" --music "$T/in/gone.mp3" "$T/in/a.mp4"; eq "missing music: rc 1, no ffmpeg" "$rc|$err|$(calls)" "1|no such file: $T/in/gone.mp3|0"
reset; run -o "$T/nodir/m.mp4" "$T/in/a.mp4"; eq "missing output folder: rc 1, no ffmpeg" "$rc|$err|$(calls)" "1|output folder does not exist: $T/nodir|0"
reset; run -o m.mp4 "$T/in/a.mp4"; eq "relative -o in the cwd works" "$rc|$(cat "$T/out/m.mp4" 2>/dev/null)" "0|final t=5.000"

echo "# ffmpeg fails"
reset; touch "$FAKE/ffmpeg-fail-2"; run -o "$T/out/m.mp4" "$T/in/a.mp4" "$T/in/b.mov" "$T/in/c.mp4"
eq "2nd part fails: rc 1, stops there" "$rc|$(calls)" "1|2"
has "error names the command" "$err" "ffmpeg failed: ffmpeg -y -i $T/in/b.mov -f lavfi"
has "error has ffmpeg's stderr" "$err" "Invalid argument (fake)"
eq "no output" "$([ -e "$T/out/m.mp4" ] && echo yes || echo no)" "no"
eq "temp dir removed" "$(ls -A "$TMPDIR")" ""
reset; touch "$FAKE/ffmpeg-fail-3"; run -o "$T/out/m.mp4" "$T/in/a.mp4" "$T/in/b.mov"
eq "final render fails: rc 1" "$rc" "1"
eq "temp dir removed" "$(ls -A "$TMPDIR")" ""
reset; echo garbage > "$T/in/bad.mp4"; run -o "$T/out/m.mp4" "$T/in/bad.mp4"
eq "unreadable video: rc 1 (ffprobe fails)" "$rc" "1"
has "says ffmpeg failed" "$err" "ffmpeg failed: ffprobe"

echo "# real ffmpeg (local) on tiny clips: the graphs are valid"
if [ "${VIDMONTAGE_NO_REAL:-0}" = 1 ] || ! PATH=$REAL_PATH command -v ffmpeg >/dev/null; then
  echo "skipped (VIDMONTAGE_NO_REAL=1 or no ffmpeg)"
else
  R=$T/real; mkdir -p "$R"
  rf() { PATH=$REAL_PATH ffmpeg -v error -y "$@" >/dev/null 2>&1; }
  rf -f lavfi -i testsrc=s=64x36:d=1.5:r=30 -f lavfi -i sine=d=1.5 -shortest "$R/a.mp4"
  rf -f lavfi -i testsrc2=s=64x36:d=1.5:r=30 "$R/b.mp4"
  rf -f lavfi -i sine=f=300:d=2 "$R/m.wav"
  rf -f lavfi -i color=red:s=64x36 -frames:v 1 "$R/p.png"
  # timeout signals the whole process group, so a hung ffmpeg (e.g. an endless anullsrc) cannot stall the suite
  rr() { (cd "$R" && PATH=$REAL_PATH timeout 30 "$V" --size 64x36 --photo-secs 1 "$@") > "$T/stdout" 2> "$T/stderr"; rc=$?; err=$(cat "$T/stderr"); }
  dur() { PATH=$REAL_PATH ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null | cut -c1-3; }
  rr -o o1.mp4 a.mp4 b.mp4; eq "2 clips: rc 0, ~2.2 s" "$rc|$(dur "$R/o1.mp4")" "0|2.2"
  rr -o o2.mp4 --music m.wav --music-only a.mp4 b.mp4 p.png; eq "--music-only, 3 parts: rc 0 (was: unconnected output)" "$rc" "0"
  [ "$rc" = 0 ] || printf '       %s\n' "$(echo "$err" | tail -3)"
  rr -o o3.mp4 --music m.wav --title "PersonG & Anna: it's 100% \\o/" a.mp4 b.mp4; eq "--music + --title with : ' % \\: rc 0" "$rc" "0"
  rr -o o4.mp4 --fade 0 p.png a.mp4; eq "--fade 0 + photo: rc 0, 2.5 s (was 1.5: xfade d=0 cut it)" "$rc|$(dur "$R/o4.mp4")" "0|2.5"
  rr -o o5.mp4 --fade 0 --music m.wav --music-only a.mp4 b.mp4; eq "--fade 0 --music-only: rc 0, 3.0 s" "$rc|$(dur "$R/o5.mp4")" "0|3.0"
  eq "temp dirs removed" "$(ls -A "$TMPDIR")" ""
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
