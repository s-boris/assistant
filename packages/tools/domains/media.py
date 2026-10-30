"""Media: typed tools over `media` (~/bin/media: Seerr + Radarr/Sonarr + Emby, keys read by the CLI itself from its
own secrets file, never by a tool): search, status, the download queue, recent imports, what airs, what is missing,
health, trending and similar titles, mood picks from his library, continue watching, a request diagnosis, and a
Seerr request.

Boundaries: no delete, blocklist, cleanup or unmonitor tools (his rule: space = more disks). media.request is the one
write: a Seerr request (Seerr auto-approves his user, then Radarr/Sonarr grab it), refused by the CLI for titles on
Seerr's blocklist or already requested. media.doctor only diagnoses (always --dry-run): its routine fixes stay on the
CLI. Play on the TV, tonight/group picks and the franchise poll stay Telegram flows (skill media-requests).
Env: TOOLS_MEDIA_BIN.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from tools.clirun import bin_path, first_line, no_dash, run_json
from tools.core import REGISTRY, Domain, Format, ToolError, ToolInput, ToolOutput, tool

NOTES = (
    "`media` (CLI, managed; skills media-requests + media-help): the same reads by hand plus what is not a tool "
    "on purpose: play on the TV (`media play`, a Telegram button flow), tonight/group/franchise picks with "
    "buttons and polls, wrapped, posters, hogs/unwatched (never pitched: no media deletion), doctor's routine "
    "fixes (`media doctor` without --dry-run, weekly cron), ready-watch (cron, texts him when his requests "
    "land). Keys live in the CLI's secrets file and never reach a tool or its output."
)

REGISTRY.add_domain(
    Domain(
        name="media",
        title="Media",
        summary="His media server (Seerr, Radarr/Sonarr, Emby): search, status, queue, recent imports, airing,"
        " missing, health, trending and similar titles, mood picks, continue watching, request diagnosis, and"
        " Seerr requests. No deletes.",
        feature="gateway.media",
        notes=NOTES,
    )
)

Text = Annotated[str, AfterValidator(no_dash)]
Kind = Literal["movie", "tv"]


def _bin() -> str:
    return bin_path("TOOLS_MEDIA_BIN", "media")


def _fail(rc: int, out: str, err: str, what: str) -> ToolError:
    msg = first_line(err, out, default=f"{what}: exit {rc}").removeprefix("media: ")
    if rc == 2 or "usage:" in msg:
        return ToolError("invalid_args", msg, f"`media {what.split()[-1]} --help` by hand")
    low = msg.lower()
    if any(w in low for w in ("timed out", "timeout", "connection", "unreachable", "refused", "http 5")):
        return ToolError("unavailable", msg, "Seerr or an arr is down: media.health")
    if "blocklist" in low:
        return ToolError("conflict", msg, "on Seerr's blocklist: not requested (his call to unblock it in Seerr)")
    return ToolError("failed", msg, f"run `{what}` by hand to see why")


def media_json(argv: list[str], what: str, ok: tuple[int, ...] = (0,), timeout: float = 60.0) -> Any:
    return run_json(_bin(), argv, what, ok=ok, timeout=timeout, fail=_fail)[1]


def _lines(rows: list[dict[str, Any]], fmt_row, empty: str) -> str:
    return "\n".join(fmt_row(r) for r in rows) if rows else empty


# ------------------------------------------------------------------ models


class SearchIn(ToolInput):
    query: Text = Field(min_length=1, max_length=200, description="title words")
    type: Kind | None = Field(default=None, description="movie | tv (default both)")
    n: int = Field(8, ge=1, le=20)


class TitleIn(ToolInput):
    """A title by name (best matches first) or by TMDB id + type."""

    title: Text | None = Field(default=None, min_length=1, max_length=200, description="title words")
    tmdb: int | None = Field(default=None, ge=1, le=100_000_000, description="TMDB id (needs type)")
    type: Kind | None = None

    @model_validator(mode="after")
    def _one(self) -> TitleIn:
        if not self.title and self.tmdb is None:
            raise ValueError("give title, or tmdb + type")
        if self.tmdb is not None and not self.type:
            raise ValueError("tmdb needs type (movie | tv)")
        return self

    def argv(self) -> list[str]:
        a = ["--tmdb", str(self.tmdb)] if self.tmdb is not None else []
        a += ["--type", self.type] if self.type else []
        return a + ([self.title] if self.title and self.tmdb is None else [])


class StatusIn(TitleIn):
    n: int = Field(1, ge=1, le=5, description="how many search matches to show")


class EmptyIn(ToolInput):
    pass


class RecentIn(ToolInput):
    n: int = Field(15, ge=1, le=50)


class AiringIn(ToolInput):
    days: int = Field(7, ge=1, le=21)
    cinema: bool = Field(default=False, description="also cinema releases")


class MissingIn(ToolInput):
    n: int = Field(10, ge=1, le=50, description="rows per section")


class TrendingIn(ToolInput):
    type: Kind | None = None
    n: int = Field(10, ge=1, le=20)
    new: bool = Field(default=False, description="only titles not on the server or requested")


class SimilarIn(TitleIn):
    n: int = Field(10, ge=1, le=20)
    new: bool = Field(default=False, description="only titles not on the server or requested")


class VibeIn(ToolInput):
    text: Text = Field(
        min_length=1, max_length=200, description="a mood, words, 'like <film>', a time ('90min', 'under 2h')"
    )
    n: int = Field(4, ge=1, le=8)
    series: bool = Field(default=False, description="series instead of movies")


class ContinueIn(ToolInput):
    n: int = Field(4, ge=1, le=8)


class DoctorIn(ToolInput):
    title: Text | None = Field(
        default=None, min_length=1, max_length=200, description="one title (default: open requests)"
    )
    type: Kind | None = None
    mine: bool = Field(default=False, description="only his own requests (without title)")
    days: int = Field(180, ge=0, le=3650, description="requests of the last N days (0 = all; without title)")
    probe: bool = Field(default=False, description="also run interactive indexer searches (slower)")
    n: int = Field(15, ge=1, le=50)


class RequestIn(ToolInput):
    tmdb: int = Field(ge=1, le=100_000_000, description="TMDB id (media.search shows it)")
    type: Kind
    seasons: list[int] | None = Field(
        None, min_length=1, max_length=60, description="tv only: season numbers (default: all open seasons)"
    )
    dry_run: bool = Field(default=False, description="show what would be requested, send nothing")

    @model_validator(mode="after")
    def _seasons(self) -> RequestIn:
        if self.seasons and self.type != "tv":
            raise ValueError("seasons are for type tv")
        if self.seasons and any(not 0 < s < 1000 for s in self.seasons):
            raise ValueError("season numbers 1..999")
        return self


class Hit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    year: str | int | None = None
    type: str | None = None
    tmdb: int | None = None
    status: str | None = None
    rating: float | None = None


def _hit_line(h: dict[str, Any]) -> str:
    r = f" · {h['rating']}" if h.get("rating") else ""
    return f"{h.get('title')} ({h.get('year') or '?'}) [{h.get('type')}] tmdb:{h.get('tmdb')} — {h.get('status')}{r}"


class HitsOut(ToolOutput):
    count: int
    results: list[Hit]
    like: Hit | None = Field(default=None, description="similar: the title the picks are like")

    def render(self, fmt: Format = "concise") -> str:
        head = f"like {self.like.title} ({self.like.year})\n" if self.like else ""
        return head + _lines([r.model_dump(exclude_none=True) for r in self.results], _hit_line, "no results")


def _dl_line(d: dict[str, Any]) -> str:
    if "series" in d:
        eps = d.get("episodes") or []
        e = f"E{eps[0]:02d}" + (f"-E{eps[-1]:02d} ({len(eps)} eps)" if len(eps) > 1 else "") if eps else ""
        name = f"{d['series']} S{(d.get('season') or 0):02d}{e}"
    else:
        name = f"{d.get('title')} ({d.get('year') or '?'})"
    issue = f" !! {d['issue']}" if d.get("issue") else ""
    return f"{name} — {d.get('progress')}, {d.get('status')}, {d.get('eta')}{issue}"


class StatusOut(ToolOutput):
    count: int
    titles: list[dict[str, Any]] = Field(
        description="title, year, type, tmdb, status, requests (id, status, seasons, by, created), seasons (tv),"
        " emby_url, downloading, note"
    )

    def render(self, fmt: Format = "concise") -> str:
        if not self.titles:
            return "no results"
        out = []
        for t in self.titles:
            out.append(_hit_line(t))
            for r in t.get("requests") or []:
                s = ",".join(map(str, r.get("seasons") or []))
                out.append(f"  request #{r.get('id')}: {r.get('status')}{' S' + s if s else ''} by {r.get('by')}")
            if t.get("seasons"):
                out.append("  seasons: " + ", ".join(f"S{k}: {v}" for k, v in t["seasons"].items()))
            out += [f"  downloading: {_dl_line(d)}" for d in t.get("downloading") or []]
            if t.get("note"):
                out.append(f"  {t['note']}")
            if t.get("emby_url"):
                out.append(f"  Emby: {t['emby_url']}")
        return "\n".join(out)


class QueueOut(ToolOutput):
    movies: list[dict[str, Any]]
    tv: list[dict[str, Any]] = Field(description="one row per download (a season pack = one row with its episodes)")

    def render(self, fmt: Format = "concise") -> str:
        if not self.movies and not self.tv:
            return "nothing downloading"
        out = [f"Movies ({len(self.movies)}):"] + [f"  {_dl_line(d)}" for d in self.movies] if self.movies else []
        out += [f"TV ({len(self.tv)}):"] + [f"  {_dl_line(d)}" for d in self.tv] if self.tv else []
        return "\n".join(out)


class RecentOut(ToolOutput):
    items: list[dict[str, Any]] = Field(
        description="newest first: at (his local time), type, title, year | season + episodes"
    )

    def render(self, fmt: Format = "concise") -> str:
        def row(r: dict[str, Any]) -> str:
            if r.get("type") == "tv":
                eps = ",".join(f"E{e:02d}" for e in r.get("episodes") or [])
                return f"{r.get('at')}  tv     {r.get('title')} S{(r.get('season') or 0):02d} {eps}"
            return f"{r.get('at')}  movie  {r.get('title')} ({r.get('year') or '?'})"

        return _lines(self.items, row, "nothing imported recently")


class AiringOut(ToolOutput):
    start: str
    end: str
    episodes: list[dict[str, Any]] = Field(description="series, season, episode, title, air_local, has_file")
    movies: list[dict[str, Any]] = Field(description="date, kind (digital/physical/cinema), title, year, has_file")
    warnings: list[Any] = Field(default_factory=list)

    def render(self, fmt: Format = "concise") -> str:
        out = [
            f"{len(self.episodes)} episode(s), {len(self.movies)} movie release(s) {self.start[:10]} … {self.end[:10]}"
        ]
        for e in self.episodes:
            done = " ✓" if e.get("has_file") else ""
            out.append(
                f"  {str(e.get('air_local'))[:16].replace('T', ' ')} {e.get('series')} "
                f"S{e.get('season', 0):02d}E{e.get('episode', 0):02d}{done}"
            )
        for m in self.movies:
            out.append(f"  {m.get('date')} {m.get('kind')}: {m.get('title')} ({m.get('year')})")
        return "\n".join(out + [f"warning: {w}" for w in self.warnings])


class MissingOut(ToolOutput):
    episodes_total: int
    totals: dict[str, int] = Field(description="shows, movies_released, movies_not_out: full counts (lists show top n)")
    shows: list[dict[str, Any]] = Field(description="series, episodes missing, seasons, last_aired")
    movies_released: list[dict[str, Any]]
    movies_not_out: list[dict[str, Any]]
    warnings: list[Any] = Field(default_factory=list)

    def render(self, fmt: Format = "concise") -> str:
        t = self.totals
        out = [f"{self.episodes_total} episode(s) missing across {t.get('shows')} show(s); top {len(self.shows)}:"]
        out += [f"  {s.get('series')}: {s.get('episodes')} eps {s.get('seasons')}" for s in self.shows]
        out.append(f"released movies without a file: {t.get('movies_released')}; top {len(self.movies_released)}:")
        out += [f"  {m.get('title')} ({m.get('year')})" for m in self.movies_released]
        out.append(f"announced, not out yet: {t.get('movies_not_out')}")
        return "\n".join(out + [f"warning: {w}" for w in self.warnings])


class HealthOut(ToolOutput):
    ok: bool
    problems: list[dict[str, Any]] = Field(description="service, type, source, message")
    notes: list[Any] = Field(default_factory=list)
    checked: list[Any] = Field(default_factory=list)

    def render(self, fmt: Format = "concise") -> str:
        if self.ok and not self.problems:
            return "media stack healthy (" + ", ".join(map(str, self.checked)) + ")"
        lines = [f"{p.get('service')} {p.get('type')}: {p.get('message')}" for p in self.problems]
        return "\n".join(lines + [str(n) for n in self.notes])


class PicksOut(ToolOutput):
    DETAIL: ClassVar[tuple[str, ...]] = ("message", "buttons")
    label: str | None = None
    kind: str | None = None
    picks: list[dict[str, Any]] = Field(description="emby_id, title, year, rating, runtime_min, why, link, seen")
    warnings: list[Any] = Field(default_factory=list)
    text: str | None = Field(default=None, description="the plain answer text with links")
    message: str | None = None
    buttons: list[Any] | None = None

    def render(self, fmt: Format = "concise") -> str:
        return self.text or _lines(self.picks, lambda p: f"{p.get('title')} ({p.get('year')})", "no picks")


class ContinueOut(ToolOutput):
    DETAIL: ClassVar[tuple[str, ...]] = ("message", "buttons")
    items: list[dict[str, Any]] = Field(
        description="id, kind, title, episode, pos_min, runtime_min, state, last_played"
    )
    total: int | None = None
    play_enabled: bool | None = None
    warnings: list[Any] = Field(default_factory=list)
    message: str | None = None
    buttons: list[Any] | None = None

    def render(self, fmt: Format = "concise") -> str:
        def row(i: dict[str, Any]) -> str:
            ep = f" {i.get('episode')}" if i.get("episode") else ""
            at = f" at {i.get('pos_min')}/{i.get('runtime_min')} min" if i.get("pos_min") else ""
            return f"{i.get('title')}{ep} ({i.get('state')}){at}"

        return _lines(self.items, row, "nothing to continue")


class DoctorOut(ToolOutput):
    DETAIL: ClassVar[tuple[str, ...]] = ("message",)
    items: list[dict[str, Any]] = Field(
        description="title, year, type, tmdb, request, requested_by, code, verdict, action, needs_owner, de, en"
    )
    dry_run: bool = True
    skipped: dict[str, Any] = Field(default_factory=dict)
    warnings: list[Any] = Field(default_factory=list)
    text: str | None = None
    message: str | None = None

    def render(self, fmt: Format = "concise") -> str:
        return self.text or _lines(self.items, lambda i: f"{i.get('title')}: {i.get('verdict')}", "nothing open")


class RequestOut(ToolOutput):
    result: Literal["requested", "not_requested", "dry_run"]
    title: str
    type: str
    tmdb: int
    request_id: int | None = None
    request_status: str | None = None
    seasons: list[int] | None = None
    skipped: dict[str, str] | None = Field(default=None, description="tv: seasons already handled (season -> state)")
    reason: str | None = None

    def render(self, fmt: Format = "concise") -> str:
        s = f" seasons {','.join(map(str, self.seasons))}" if self.seasons else ""
        if self.result == "requested":
            return f"Requested {self.title}{s} (request #{self.request_id}, {self.request_status})"
        if self.result == "dry_run":
            return f"dry run: would request {self.title}{s}"
        return f"Not requested: {self.title} — {self.reason}"


# ------------------------------------------------------------------ tools


@tool(
    "media.search",
    effect="read",
    rate="60/min",
    idempotent=True,
    timeout_s=60,
    examples=({"query": "Dune"}, {"query": "Severance", "type": "tv", "n": 3}),
)
def search(a: SearchIn) -> HitsOut:
    """Search Seerr (TMDB) for a title: year, type, TMDB id and whether it is on the server or requested.
    Use first for "add/get X" (the TMDB id feeds media.request) and "is X on Emby"."""
    argv = ["search", "--json", "-n", str(a.n)] + (["--type", a.type] if a.type else []) + [a.query]
    rows = media_json(argv, "media search")
    hits = [Hit.model_validate(r) for r in rows if isinstance(r, dict)]
    return HitsOut(count=len(hits), results=hits)


@tool(
    "media.status",
    effect="read",
    labels_out=("private",),
    rate="60/min",
    idempotent=True,
    timeout_s=60,
    examples=({"title": "Dune"}, {"tmdb": 1399, "type": "tv"}),
)
def status(a: StatusIn) -> StatusOut:
    """Where a title stands: on Emby, requested (by whom), per-season state, live download progress.
    For "is X downloaded yet", "how far is X". Requesters are his friends' display names: never relay them."""
    rows = media_json(["status", "--json", "-n", str(a.n), *a.argv()], "media status")
    rows = [r for r in rows if isinstance(r, dict)]
    return StatusOut(count=len(rows), titles=rows)


@tool("media.queue", effect="read", rate="60/min", idempotent=True, timeout_s=60, examples=({},))
def queue(a: EmptyIn) -> QueueOut:
    """What Radarr and Sonarr are downloading: progress, ETA and import problems (importPending, blocked)."""
    d = media_json(["queue", "--json"], "media queue")
    return QueueOut(movies=d.get("movies") or [], tv=d.get("tv") or [])


@tool("media.recent", effect="read", rate="60/min", idempotent=True, timeout_s=60, examples=({}, {"n": 5}))
def recent(a: RecentIn) -> RecentOut:
    """What arrived on the server lately (Radarr/Sonarr imports, newest first; episodes of a day grouped)."""
    rows = media_json(["recent", "--json", "-n", str(a.n)], "media recent")
    return RecentOut(items=[r for r in rows if isinstance(r, dict)])


@tool(
    "media.airing",
    effect="read",
    rate="30/min",
    idempotent=True,
    timeout_s=60,
    examples=({}, {"days": 3, "cinema": True}),
)
def airing(a: AiringIn) -> AiringOut:
    """Episodes and movie releases coming up for what the arrs monitor (his local time; ✓ = on disk)."""
    argv = ["airing", "--json", "--days", str(a.days)] + (["--cinema"] if a.cinema else [])
    d = media_json(argv, "media airing", ok=(0, 3))
    return AiringOut(
        start=str(d.get("start") or ""),
        end=str(d.get("end") or ""),
        episodes=d.get("episodes") or [],
        movies=d.get("movies") or [],
        warnings=d.get("warnings") or [],
    )


@tool("media.missing", effect="read", rate="30/min", idempotent=True, timeout_s=90, examples=({}, {"n": 5}))
def missing(a: MissingIn) -> MissingOut:
    """Monitored episodes and movies without a file (what's missing or stuck).
    Shows with the most missing episodes, released movies the arrs could not find, announced ones not out yet."""
    d = media_json(["missing", "--json", "-n", str(a.n)], "media missing", ok=(0, 3))
    lists = {k: d.get(k) or [] for k in ("shows", "movies_released", "movies_not_out")}
    return MissingOut(
        episodes_total=int(d.get("episodes_total") or 0),
        totals={k: len(v) for k, v in lists.items()},
        shows=(d.get("shows") or [])[: a.n],
        movies_released=(d.get("movies_released") or [])[: a.n],
        movies_not_out=(d.get("movies_not_out") or [])[: a.n],
        warnings=d.get("warnings") or [],
    )


@tool("media.health", effect="read", rate="30/min", idempotent=True, timeout_s=60, examples=({},))
def health(a: EmptyIn) -> HealthOut:
    """Radarr/Sonarr health checks and Seerr status: indexer failures, download client, disk space warnings."""
    d = media_json(["health", "--json"], "media health", ok=(0, 2))
    return HealthOut(
        ok=bool(d.get("ok")),
        problems=d.get("problems") or [],
        notes=d.get("notes") or [],
        checked=d.get("checked") or [],
    )


@tool(
    "media.trending",
    effect="read",
    rate="30/min",
    idempotent=True,
    timeout_s=60,
    examples=({}, {"type": "tv", "new": True, "n": 5}),
)
def trending(a: TrendingIn) -> HitsOut:
    """Trending on TMDB (via Seerr) with his server's status per title; new=true leaves out what he has."""
    argv = (
        ["trending", "--json", "-n", str(a.n)] + (["--type", a.type] if a.type else []) + (["--new"] if a.new else [])
    )
    rows = media_json(argv, "media trending")
    hits = [Hit.model_validate(r) for r in rows if isinstance(r, dict)]
    return HitsOut(count=len(hits), results=hits)


@tool(
    "media.similar",
    effect="read",
    rate="30/min",
    idempotent=True,
    timeout_s=60,
    examples=({"title": "Arrival"}, {"tmdb": 438631, "type": "movie", "new": True}),
)
def similar(a: SimilarIn) -> HitsOut:
    """Recommendations like a title (Seerr/TMDB), with his server's status; new=true = only what he lacks.
    For library picks by mood or 'like <film>' from what is already on the server, use media.vibe."""
    argv = ["similar", "--json", "-n", str(a.n)] + (["--new"] if a.new else []) + a.argv()
    d = media_json(argv, "media similar")
    rows = d.get("results") if isinstance(d, dict) else d
    hits = [Hit.model_validate(r) for r in rows or [] if isinstance(r, dict)]
    like = Hit.model_validate(d["like"]) if isinstance(d, dict) and isinstance(d.get("like"), dict) else None
    return HitsOut(count=len(hits), results=hits, like=like)


@tool(
    "media.vibe",
    effect="read",
    labels_out=("private",),
    rate="30/min",
    timeout_s=90,
    examples=(
        {"text": "cozy rainy sunday"},
        {"text": "like Arrival, under 2h"},
        {"text": "dark crime", "series": True},
    ),
)
def vibe(a: VibeIn) -> PicksOut:
    """Picks from his own library for a mood, words or 'like <film>' (local embedding index; his unseen first).
    For "/vibe", "a movie for a cozy rainy sunday", "something mind-bending like Arrival"."""
    argv = ["vibe", "--json", "-n", str(a.n)] + (["--series"] if a.series else []) + [a.text]
    d = media_json(argv, "media vibe", ok=(0, 3))
    return PicksOut(
        label=d.get("label"),
        kind=d.get("kind"),
        picks=d.get("picks") or [],
        warnings=d.get("warnings") or [],
        text=d.get("text"),
        message=d.get("message"),
        buttons=d.get("buttons"),
    )


@tool("media.continue", effect="read", labels_out=("private",), rate="30/min", timeout_s=60, examples=({},))
def continue_(a: ContinueIn) -> ContinueOut:
    """His Emby 'continue watching': resume points and next-up episodes (his user only)."""
    d = media_json(["continue", "--json", "-n", str(a.n)], "media continue", ok=(0, 3))
    return ContinueOut(
        items=d.get("items") or [],
        total=d.get("total"),
        play_enabled=d.get("play_enabled"),
        warnings=d.get("warnings") or [],
        message=d.get("message"),
        buttons=d.get("buttons"),
    )


@tool(
    "media.doctor",
    effect="read",
    labels_out=("private",),
    rate="10/min",
    timeout_s=180,
    examples=({"title": "Dune: Part Three"}, {"mine": True, "days": 60}),
)
def doctor(a: DoctorIn) -> DoctorOut:
    """Why a Seerr request has not reached Emby yet, with a German + English line he can forward.
    Verdicts: not released, no release found, stuck import ... Diagnosis only (dry run): the routine fixes stay on
    the CLI's weekly cron."""
    argv = ["doctor", "--json", "--dry-run", "-n", str(a.n)] + ([] if a.probe else ["--no-probe"])
    argv += ["--type", a.type] if a.type else []
    if a.title:
        argv.append(a.title)
    else:
        argv += ["--all", "--days", str(a.days)] + (["--mine"] if a.mine else [])
    d = media_json(argv, "media doctor", ok=(0, 3), timeout=170)
    return DoctorOut(
        items=d.get("items") or [],
        dry_run=bool(d.get("dry_run", True)),
        skipped=d.get("skipped") or {},
        warnings=d.get("warnings") or [],
        text=d.get("text"),
        message=d.get("message"),
    )


@tool(
    "media.request",
    effect="home",
    rate="30/h",
    idempotent=True,
    timeout_s=60,
    examples=({"tmdb": 438631, "type": "movie"}, {"tmdb": 1399, "type": "tv", "seasons": [1, 2], "dry_run": True}),
)
def request(a: RequestIn) -> RequestOut:
    """Request a movie or show in Seerr (his user: auto-approved, then Radarr/Sonarr grab it).
    Get the TMDB id from media.search first. Already requested / on the server / on the blocklist: not requested
    again (result says why). tv: all open seasons unless seasons lists some."""
    argv = ["request", str(a.tmdb), "--type", a.type, "--json"]
    if a.seasons:
        argv += ["--seasons", ",".join(map(str, a.seasons))]
    if a.dry_run:
        argv.append("--dry-run")
    d = media_json(argv, "media request")
    return RequestOut.model_validate({k: v for k, v in d.items() if k in RequestOut.model_fields})
