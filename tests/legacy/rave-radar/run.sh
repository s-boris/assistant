#!/usr/bin/env bash
# Offline tests for ~/bin/rave-radar: date parsing, artist matching, robots rules, refused hosts, html/wp/jsonld/lineup
# parsing, region filter, presale rules, dedupe, cron note, mute, failure streaks, artists file (0600); releases
# (Deezer mapping, silent seed, new-release note, radio-episode skip, pinning, crate, API errors); preshow (MixesDB
# tracklists, most-played, IDs, Deezer lookups, widening, fallback, crate ids, Wednesday-before cron).
# Synthetic fixtures; RAVE_RADAR_FIXTURES serves every URL, so nothing touches the network. Exit 0 = all pass.
# Test a copy before installing: RAVE_RADAR_BIN=/path/to/copy ./run.sh
cd "$(dirname "$0")" || exit 2
python3 -I -W ignore::ResourceWarning test_rave_radar.py "$@"; a=$?
python3 -I -W ignore::ResourceWarning test_releases.py "$@"; b=$?
python3 -I -W ignore::ResourceWarning test_preshow.py "$@"; c=$?
exit $((a | b | c))
