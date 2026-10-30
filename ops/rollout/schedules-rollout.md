# Rollout: schedules as data (registry, tags, queues, keepers, sentinel)

What ships with the land (bin target, dark): the registry core/schedules.toml (+ core.schedules, core.jobfail),
`assistant cron tag|check`, `hub wf cron`'s registry check, the workflow tools' registry listing / why / edit and the
registry-wide executor (job.failed / job.recovered), `keepers` and `hub-sentinel` (no cron line runs them yet), the
event kinds job.failed, job.recovered, schedule.changed, hub.down. The per-class DBOS queues (jobs-critical 2,
jobs-fast 4, jobs-slow 2) are hub code: they load with the next `assistant deploy --target hub` (his ✅); until then
schedules made by the new brain side land on H15's hub-jobs (the old hubd ignores `queue`).
The tool worker's default threads go 6 -> 12 at its next fresh start (a land's respawn keeps its count).

## Done at build time
- Census: ~/reports/plans/p3-1-census.md (135 lines, 138 jobs; classes = plan §6.1).
- Tags: `assistant cron tag` (comment-only proof: suffix-only diff, shell top level at each line's end, `sh -n` on
  both versions; backup in ~/.local/state/assistant/crontab-backups/). `assistant cron check` = 0 untracked.

## Switch-over (main session; pure additions first, the old lines stay until step 4)
1. `keepers status` · `hub-sentinel status` · `assistant cron check` (exit 0).
2. `assistant cron add keepers '* * * * *' '~/bin/keepers ensure --cron >/dev/null 2>&1'`
   `assistant cron add hub-sentinel '* * * * *' '~/bin/hub-sentinel check --cron >/dev/null 2>&1'`
   Both run beside the old keeper lines: every ensure is idempotent (its own lock), so the overlap only doubles
   failure notes (cron-fail + job.failed) for a broken keeper.
3. After ~10 min: `keepers status` (6 keepers, last exit 0), `hub-sentinel status` (up).
4. In the registry flip the six keepers to `runs_on = "keepers"` and keepers / hub-sentinel to `runs_on = "cron"`
   (land), then `assistant cron rm tool-worker`, `hub-feed`, `browser-ensure`, `whisper-server`,
   `llm-idle-check`, `cputemp-log` -> 128 lines (131 less the three H15 Pilot 2 watch lines, now hub schedules:
   their registry entries say `runs_on = "hub"`); `assistant cron check` exit 0.
5. Drill (one job.failed in the next beat's agenda): `tool call workflow.job '{"name":"drill-fail","mode":"live"}'`,
   then ack the event and delete ~/.local/state/jobs/drill-fail.streak.json.

## Done (switch-over)
- Steps 1-5 done; the registry (core/schedules.toml) and ~/crontab agree: 128 tagged lines, `assistant cron check`
  0 untracked / 0 missing / 0 changed. Runners: cron 128 lines; `keepers` the six keepers; the hub (H15 Pilot 2,
  live) watch, media-ready, damp-watch, window-watch; drill-fail none.
- Lints: the cron-fail suite's SILENT_OK lists `bin/keepers ensure --cron` and `bin/hub-sentinel check --cron` (each
  writes its own failure notes) and no longer `llm-server-ctl idle-check` (a keeper now); JOBS.md documents keepers,
  hub-sentinel, the job.failed notes and the hub-run watch jobs (drift lint clean).
- The drill's job.failed reached the agenda and was acked; its streak file is gone.

## Switches and rollback
- One keeper: `keepers off NAME --why …` / `keepers on NAME`; all: ~/.config/assistant/keepers/all.off.
- Sentinel: ~/.config/assistant/hub-sentinel-off.
- Rollback: `assistant cron rm keepers` + `hub-sentinel`, then `assistant cron add NAME 'CRON' 'COMMAND'` for each
  keeper from its registry `cron_line` (or restore the newest crontab backup + `crontab ~/crontab`); flip their
  `runs_on` back to "cron" (land) so `assistant cron check` is clean again, and put `llm-server-ctl idle-check`
  back into the cron-fail suite's SILENT_OK.

## The 4 critical jobs and drive-watch on hub schedules (live 28.10, the owner's msg 2841: no shadow)
Jobs: ha-autopilot-watch (every minute), home-ctx (*/5), emby-watch (*/2), agent-watch (*/5) (registry group
`critical`, queue jobs-critical) and drive-watch (3-58/5, class W, its 24.10 hold is over). Registry: `runs_on = "hub"`.
drive-brief stays on cron until unplanned drives have a start signal (event-workflow runbook step 5).
- Hub down: hub-sentinel (cron, every minute) runs the `critical` hub jobs from here while /health has failed for
  >= 3 min, at their own minutes, each slot once (`hub-sentinel run-job`, core.watchjobs.run_live; usage src
  `sentinel`; ~/.local/state/hub-sentinel/jobs.json). Minute-level hub schedules never catch up, so nothing runs
  twice when the hub is back. The Recovered line and `hub-sentinel status` count the runs. Off alone:
  ~/.config/assistant/hub-sentinel-jobs-off. drive-watch has no fallback (a 5-min Waze check; the hub's run resumes).
- Switch, per job, in one minute: `tool call workflow.schedule '{"job": "X", "state": "live", "why": "critical jobs live"}'`
  then `assistant cron rm X`. Check: `tool call workflow.schedules '{}'` shows `ACTIVE live` with a fresh `last`,
  `assistant cron check` exit 0, the job's own log/state keeps its cadence.
- Rollback, per job: `assistant cron add X` from its registry `cron_line`, then `tool call workflow.schedule
  '{"job": "X", "state": "shadow", "why": "critical jobs rollback"}'` in the same minute; flip its `runs_on` back to "cron"
  (land; that also ends its sentinel fallback).
