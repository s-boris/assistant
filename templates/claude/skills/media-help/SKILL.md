---
name: media-help
description: Ready answers for the owner when friends who use their media server (Emby + Seerr requests) have trouble. Use when the OWNER asks "X can't play Y", "how does X request a movie", "why is it buffering / stuttering for X", "subtitles missing", "wrong language / no audio in my language", "why isn't Y there yet" (for a friend), "X can't log in / which app", "is Emby down", or "what do I tell X". Read-only checks, then a short reply text the owner forwards. Never contact friends, never raise this unasked. The owner's own requests and download status → skill media-requests.
---
# Media help for friends (the owner asks, I check, they forward)

Only when the owner asks. Nothing goes to anyone but the owner; they forward the text themselves. Requests and download status themselves: skill media-requests. This skill = diagnosis + a forwardable reply.

## Checks (all read-only, pick what fits)
| Question | Run | Read it as |
|---|---|---|
| "Why isn't Y there?" | `media doctor "Y"` (skill media-requests) | one verdict (not out yet, no release, downloading + ETA, re-grabbed, mismatch, …), routine fixes already done, a ready line to forward; `media status "Y"` for the plain Seerr state |
| Nothing arrives at all | `media queue`, `media health` | `!!` stuck imports, indexer or download-client problems |
| Released long ago, still missing | `media missing` | no usable release yet; don't re-request |
| "Is Emby down?" | `media health`; with the unraid feature, skill server-health for the containers | container not running or restarted recently |
| Buffering right now / who is streaming | `emby now` (GET only) | per stream: LAN or remote, source, play method, transcode reasons as plain advice |
| Many remote friends buffer | `emby library` (GET-only report) | 4K titles without a 1080p version, image-only subtitles, remote bitrate limits: a proposal for the owner, never applied unasked |

Reading a play method like `Transcode (v:h264 a:direct)` (v/a = what got converted, `direct` = untouched):
- `ContainerBitrateExceedsLimit`: the app's quality setting or the user's remote limit is below the file's bitrate. Usually fine; buffering then means upload or CPU limits → template 3.
- `AudioCodecNotSupported` (TrueHD/DTS on TV or phone apps): audio transcode, can stutter → template 4.
- `SubtitleCodecNotSupported` / PGS subtitles: image subtitles get burned in = a full video transcode → template 5.
- `VideoResolutionNotSupported` / HEVC 4K on an old device or browser → template 4.
- DirectPlay but still buffering = the network on their side → template 3.

## Privacy
- Friends' viewing is their data: no user ids, devices, IPs or watch history in what I tell the owner; only what played how.
- No friend names, server addresses, links or passwords in replies or notes: use the placeholders `<emby-link>`, `<seerr-link>`, `<username>`; the owner fills them in.
- Server fixes (bitrate limits, transcoding settings, a 1080p version, subtitles) are the owner's call: say what would help, change nothing.

## Reply templates (the owner forwards; keep them this short)
Send the diagnosis in 1–2 lines, then the matching template in the friends' language (default: the profile's `locale.lang`) and English. Fill in what the checks found (title, ETA, track names); leave the placeholders.

1. **How to request:** Request movies and shows here: <seerr-link> → sign in with your Emby login → search → "Request". Once it's downloaded it shows up in Emby by itself. 🎬
2. **Not there yet:** "<title>" is requested ✅ Downloading now, about <ETA> left. / Not out digitally yet; it arrives automatically once it's released.
3. **Buffering:** Lower the quality in the Emby app (playback → quality → e.g. 8 or 4 Mbps) and it stops buffering. On home Wi-Fi, "Original"/max often works best. 📶
4. **Won't play:** Try the AC3/EAC3 audio track (not TrueHD/DTS), or use the Emby app instead of the browser. Still failing? Tell me the title and the device.
5. **Subtitles:** Pick subtitles in the player via the speech-bubble icon. Prefer an "SRT" track; "PGS" ones make it buffer. <If none: "<title>" has no <language> subtitles yet; they're often added later.>
6. **Language:** Switch the language under Audio (🔊) while playing. "<title>" has: <languages>. Set your default language in your profile → settings → playback.
7. **App / login:** Get the "Emby" app (phone, tablet, TV) or use the browser: <emby-link>. User: <username>. Forgot the password → tell me and I'll reset it.
8. **Server down:** The server is taking a short break (maintenance) 🔧 Try again in <time>.

Template 7's reset is the owner's action in the Emby admin; I can't do it.
