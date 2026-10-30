---
name: unraid-capacity
description: Answer "how much space is left", "when will the server/array be full", "how fast is it filling", "is disk X full" for the owner's Unraid server ({{network.server_name}}), and handle the weekly capacity-forecast alert notes in ~/inbox. Uses ~/bin/unraid-capacity (read-only daily samples + forecast). Which shows/movies take the space → skill media-requests; which disk to buy → skill deals.
---
# Unraid capacity forecast

Tool: `unraid-capacity` (~/bin). Strictly read-only on the server: one call of the dispatcher's read-only `capacity` verb (`unraid-ssh run capacity`), which reads Unraid's status files and `df` and wakes no disk. You never write to the server; anything that frees space (deleting media, moving data, adding a disk) is the owner's call.

## Answering a question
1. `unraid-capacity forecast --human` reads the stored samples instantly. Add `--live` for to-the-minute numbers (one extra `capacity` call, about a second; nothing saved). Samples are taken once a day by cron (`unraid-capacity record`).
2. Reply in 2–4 lines: free space (TB), usable free above the media share's per-disk minimum free (the output names it), the 30-day growth rate and the projected full date. Add the 90-day date if it differs a lot. Round the numbers ("~9 TB free, filling ~100 GB/day → full around mid-month X").
3. Per-disk questions: the "Disks over 90%" lines. "trend" comes from that disk's own samples (needs 7+ days); "high-water model" simulates Unraid's allocator with the array's rate. Under high-water, near-full disks fill together near the array's date, not one by one.
4. `unraid-capacity forecast` (no flag) prints the full JSON (pools, docker.img, excluded one-off deletes, trend details).

## Caveats worth mentioning when relevant
- Until ~30 days of real samples exist, earlier growth is estimated from the Radarr/Sonarr import/delete history (media only; needs the media feature). `unraid-capacity backfill` re-runs that estimate (Radarr/Sonarr GETs only); it stays anchored to the first real sample.
- One-off big deletes are left out of the array's rate.
- Pools and docker.img move in steps both ways (pulls, prunes, the mover); they count as filling only when usage grew on net over the window and over the last 7 days, and they need 7+ days of samples.
- Many Unraid arrays run most disks above 90% on purpose. Don't bring fullness up unprompted; the weekly alert covers it getting worse.

## Weekly alert (cron, `forecast --alert --notify`)
- Silent unless the array is projected full within 60 days or a pool/docker.img within 14. Then it messages the owner and leaves an inbox note, once per band (array 60/30/14/7 days, pools 14/7/3; it re-arms after the projection recovers well past the threshold).
- For an inbox alert note the owner already has the text. Offer concrete options only if asked, or if it's urgent (≤ 14 days): which media profiles drive growth, old seeding torrents, recycle-bin size, or a bigger or extra disk (spending money: the owner's decision).
- An inbox note saying "samples are stale" means `record` is failing: check `~/.local/state/unraid-capacity/cron.log` and `unraid-capacity record --dry-run`; tell the owner only if you can't fix it.

Files: ~/.local/state/unraid-capacity/ (samples.jsonl, backfill.jsonl + backfill-meta.json, alerts.json, cron.log).
