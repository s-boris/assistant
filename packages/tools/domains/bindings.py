"""Binding tools: the local commands a Telegram command or button may run without a brain turn.

Data, not logic. tools.gen writes them to schemas/tools.toml, which tg-adapter's bindings runner
(adapters/tg/src/registry.ts) and `tg-bind` read live. Fields as in that file's header (tools.gen.TOML_HEADER).
"""

from __future__ import annotations

from tools.core import REGISTRY, ArgvTool, Domain

REGISTRY.add_domain(
    Domain(
        name="bindings",
        title="Telegram bindings",
        summary="Commands and buttons tg-adapter runs without a brain turn (schemas/tools.toml).",
        feature="gateway.bindings",
    )
)

P = ("env", "TG_CMD_PICTURES=1")
B = "~/bin/tg-cmd"

for t in (
    ArgvTool(
        "tg-status.show",
        ("~/bin/tg-status", "show", "--plain"),
        "read",
        "The status board (/status)",
        timeout_s=10,
        note=True,
        menu="What I'm working on",
    ),
    ArgvTool("tg-cmd.bills", (B, "bills"), "read", "Open and upcoming bills", menu="Open and upcoming bills"),
    ArgvTool("tg-cmd.queue", (B, "queue"), "read", "What's downloading", menu="What's downloading (Sonarr/Radarr)"),
    ArgvTool(
        "tg-cmd.space",
        (*P, B, "space"),
        "read",
        "Server disk space and forecast",
        output="picture",
        menu="Server disk space and forecast",
    ),
    ArgvTool("tg-cmd.reminders", (B, "reminders"), "read", "Upcoming reminders", menu="Your upcoming reminders"),
    ArgvTool(
        "tg-cmd.list",
        (B, "list"),
        "home",
        "Lists (two-way sync with the HA shopping list)",
        menu="Your lists (shopping, todo, …)",
    ),
    ArgvTool(
        "tg-cmd.shop",
        (B, "shop"),
        "home",
        "Shopping list by shop (two-way sync with the HA shopping list)",
        menu="Shopping list: what to buy where",
    ),
    ArgvTool(
        "tg-cmd.parcels",
        (B, "parcels"),
        "home",
        "Parcels card; 'picked' marks a station pickup",
        text_arg=True,
        menu="📦 Parcels: what's coming, pickups (/parcels picked)",
    ),
    ArgvTool(
        "tg-cmd.gym",
        (*P, B, "gym"),
        "read",
        "Next gym session, last recap",
        output="picture",
        text_arg=True,
        menu="Next gym session from your plan",
    ),
    ArgvTool(
        "tg-cmd.cat",
        (*P, B, "cat"),
        "read",
        "Cat week",
        output="picture",
        menu="Cat week: litter visits, weight, feeder",
    ),
    ArgvTool(
        "tg-cmd.train",
        (*P, B, "train"),
        "read",
        "Next trains with live status",
        output="picture",
        menu="Next trains with live status",
    ),
    ArgvTool(
        "tg-cmd.gohome",
        (B, "gohome"),
        "read",
        "Way home",
        text_arg=True,
        menu="Way home: next 3 connections + the last tonight (/gohome stop)",
    ),
    ArgvTool("tg-cmd.rave", (B, "rave"), "read", "Upcoming raves and events", menu="Upcoming raves and events"),
    ArgvTool(
        "tg-cmd.trip",
        (*P, B, "trip"),
        "read",
        "Next trip card",
        output="picture",
        text_arg=True,
        menu="Next trip: dates, legs, open to-dos",
    ),
    ArgvTool(
        "tg-cmd.warranty",
        (B, "warranty"),
        "read",
        "Warranty lookup",
        text_arg=True,
        menu="Still under warranty? (/warranty tv filters)",
    ),
    ArgvTool(
        "tg-cmd.back",
        (B, "back"),
        "read",
        "Catch-up after time away",
        text_arg=True,
        menu="Catch-up after time away (/back 3 = 3 days)",
    ),
    ArgvTool(
        "tg-cmd.week",
        (*P, B, "week"),
        "read",
        "The week ahead",
        output="picture",
        menu="Your week: calendar, trains, weather, late nights",
    ),
    ArgvTool(
        "tg-cmd.pack",
        (B, "pack"),
        "read",
        "Packing list for the next event",
        text_arg=True,
        menu="Packing list for the next event (/pack rave)",
    ),
    ArgvTool(
        "tg-cmd.vibe",
        (*P, B, "vibe"),
        "read",
        "Library picks for a mood",
        output="picture",
        text_arg=True,
        menu="4 movies from your library for a mood (/vibe cozy rainy sunday; /vibe dark crime series)",
    ),
    ArgvTool(
        "tg-cmd.finish",
        (*P, B, "finish"),
        "read",
        "Started shows, closest to the end first",
        output="picture",
        menu="Shows you started: closest to the end first",
    ),
    ArgvTool(
        "tg-cmd.doctor",
        (B, "doctor"),
        "read",
        "Why a media request isn't there yet (no probe)",
        text_arg=True,
        menu="Why a Seerr request isn't on Emby yet (/doctor dune)",
    ),
    ArgvTool(
        "tg-cmd.party",
        (B, "party"),
        "home",
        "Party lights on/off/status",
        text_arg=True,
        menu="Party lights: WLEDs follow the music (/party off restores)",
    ),
    ArgvTool(
        "tg-cmd.crate",
        (B, "crate"),
        "home",
        "Save the playing track to the crate list",
        menu="➕ Save the track playing on Spotify to your crate",
    ),
    ArgvTool(
        "tg-cmd.usage",
        (B, "usage"),
        "read",
        "Feature usage (30 days)",
        menu="What you use: top, unused, noisy (30 days)",
    ),
    ArgvTool(
        "tg-cmd.sleep",
        (*P, B, "sleep"),
        "read",
        "Sleep and energy",
        output="picture",
        menu="Sleep & energy: last night, sleep debt, trends",
    ),
    ArgvTool("waiting-card", ("~/bin/waiting-card",), "read", "What's waiting on him", menu="What's waiting on you"),
    ArgvTool("night-log", ("~/bin/night-log",), "read", "What I did overnight", menu="What I did overnight"),
    ArgvTool(
        "traces.brief",
        ("~/bin/traces", "brief", "--instant"),
        "read",
        "Cost, reply speed and errors (24 h)",
        menu="Cost, reply speed and errors (24 h)",
    ),
    ArgvTool(
        "talk-button",
        ("~/bin/talk-button",),
        "send_owner",
        "The voice relay rings him once (Talk button)",
        timeout_s=120,
        toast="📞 Calling you…",
    ),
    ArgvTool(
        "remind.press",
        ("~/bin/remind", "press"),
        "home",
        "Reminder buttons: snooze / done (callback data in TELEGRAM_CALLBACK_DATA)",
    ),
    ArgvTool(
        "ha-autopilot-watch.fixed",
        ("~/bin/ha-autopilot-watch", "fixed"),
        "home",
        "Vacuum fault card: Fixed (resumes the daily runs)",
        timeout_s=60,
        toast="🧹 Resuming daily runs…",
    ),
):
    REGISTRY.add_argv(t)
