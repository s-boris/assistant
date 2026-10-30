// The 0.1.22 compat tables as the plugin's instant.json and callbacks.json held them when the bindings runner was built: the parity
// fixtures (every entry must resolve to the same argv as 0.1.22 and run under the repo's tools registry).
// `tg-adapter bindings --instance live` shows the live tables the same way.
export const INSTANT_0122 = [
  { command: 'bills', description: 'Open and upcoming bills', run: '~/bin/tg-cmd bills' },
  { command: 'queue', description: "What's downloading (Sonarr/Radarr)", run: '~/bin/tg-cmd queue' },
  { command: 'space', description: 'Server disk space and forecast', run: 'env TG_CMD_PICTURES=1 ~/bin/tg-cmd space' },
  { command: 'reminders', description: 'Your upcoming reminders', run: '~/bin/tg-cmd reminders' },
  { command: 'list', description: 'Your lists (shopping, todo, …)', run: '~/bin/tg-cmd list' },
  { command: 'shop', description: 'Shopping list: what to buy where', run: '~/bin/tg-cmd shop' },
  { command: 'parcels', description: "📦 Parcels: what's coming, pickups (/parcels picked)", run: '~/bin/tg-cmd parcels', args: true },
  { command: 'gym', description: 'Next gym session from your plan', run: 'env TG_CMD_PICTURES=1 ~/bin/tg-cmd gym', args: true },
  { command: 'cat', description: 'Cat week: litter visits, weight, feeder', run: 'env TG_CMD_PICTURES=1 ~/bin/tg-cmd cat' },
  { command: 'train', description: 'Next trains with live status', run: 'env TG_CMD_PICTURES=1 ~/bin/tg-cmd train' },
  { command: 'gohome', description: 'Way home: next 3 connections + the last tonight (/gohome stop)', run: '~/bin/tg-cmd gohome', args: true },
  { command: 'rave', description: 'Upcoming raves and events', run: '~/bin/tg-cmd rave' },
  { command: 'trip', description: 'Next trip: dates, legs, open to-dos', run: 'env TG_CMD_PICTURES=1 ~/bin/tg-cmd trip', args: true },
  { command: 'warranty', description: 'Still under warranty? (/warranty tv filters)', run: '~/bin/tg-cmd warranty', args: true },
  { command: 'back', description: 'Catch-up after time away (/back 3 = 3 days)', run: '~/bin/tg-cmd back', args: true },
  { command: 'week', description: 'Your week: calendar, trains, weather, late nights', run: 'env TG_CMD_PICTURES=1 ~/bin/tg-cmd week' },
  { command: 'pack', description: 'Packing list for the next event (/pack rave)', run: '~/bin/tg-cmd pack', args: true },
  { command: 'vibe', description: '4 movies from your library for a mood (/vibe cozy rainy sunday; /vibe dark crime series)', run: 'env TG_CMD_PICTURES=1 ~/bin/tg-cmd vibe', args: true },
  { command: 'finish', description: 'Shows you started: closest to the end first', run: 'env TG_CMD_PICTURES=1 ~/bin/tg-cmd finish' },
  { command: 'doctor', description: "Why a Seerr request isn't on Emby yet (/doctor dune)", run: '~/bin/tg-cmd doctor', args: true },
  { command: 'party', description: 'Party lights: WLEDs follow the music (/party off restores)', run: '~/bin/tg-cmd party', args: true },
  { command: 'crate', description: '➕ Save the track playing on Spotify to your crate', run: '~/bin/tg-cmd crate' },
  { command: 'usage', description: 'What you use: top, unused, noisy (30 days)', run: '~/bin/tg-cmd usage' },
  { command: 'sleep', description: 'Sleep & energy: last night, sleep debt, trends', run: 'env TG_CMD_PICTURES=1 ~/bin/tg-cmd sleep' },
  { command: 'waiting', description: "What's waiting on you", run: '~/bin/waiting-card' },
  { command: 'night', description: 'What I did overnight', run: '~/bin/night-log' },
  { command: 'traces', description: 'Cost, reply speed and errors (24 h)', run: '~/bin/traces brief --instant' },
]

export const CALLBACKS_0122 = [
  { name: 'talk', data: 'call:talk', run: '~/bin/talk-button', toast: '📞 Calling you…', timeout_ms: 120000, note: false },
  { name: 'reminders', data: 'rm:*', run: '~/bin/remind press', toast: '', timeout_ms: 20000, note: false },
  { name: 'vacuum-fixed', data: 'vf:fixed', run: '~/bin/ha-autopilot-watch fixed', toast: '🧹 Resuming daily runs…', timeout_ms: 60000, note: false },
]
