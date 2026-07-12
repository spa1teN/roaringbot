# RoaringBot - Discord Bot

## Overview

RoaringBot is a Discord bot (Python, discord.py ≥ 2.6) for the BIG Bears
community: e-sports match monitoring for BIG via wannspieltbig.de, birthday
reminders, a Kassenbuch (finance) report, moderation features (member log,
auto join role, honeypot, bot-trap), and a user feedback system with a
dashboard REST API. State lives in Postgres, live status is exposed to the
server dashboard via `data/status.json` and a feedback management API on port
8080 (see [DATA_INTERFACE.md](DATA_INTERFACE.md)).

Runs as two Docker containers (`docker compose up -d --build`):
`roaringbot` (the bot) and `roaringbot-db` (Postgres 16, schema auto-applied
from `db/schema.sql` on first start).

## Quick Start

```bash
cp .env.example .env   # fill in required values
docker compose up -d --build
```

Required env vars: `DISCORD_TOKEN`, `DB_PASSWORD`. Cogs disable themselves
when their channel/spreadsheet IDs are unset (see Environment Configuration
below).

## Cogs

### E-Sports (`cogs/esports.py`)

Polls `https://wannspieltbig.de/api/match_upcoming/` (all pages) every
`ESPORTS_POLL_INTERVAL_MINUTES` (production: 1 minute).

**API time handling (load-bearing, verified against the live API):**
- `first_map_at` carries German wall-clock digits regardless of its offset
  suffix — the offset is discarded and the digits re-interpreted as
  Europe/Berlin.
- `last_map_end` carries a genuine UTC (`Z`) timestamp, but it is only an
  estimate and has proven unreliable (too early — see the event-ending note
  below), so it is no longer used to time-end events.
- Matches with missing `tournament`/`lineup_a` are skipped with an error log;
  missing `lineup_b` renders as "TBA". `lineup_b.team_logo_url` is kept for the
  reminder rendering.

**Discord events:** one scheduled event per new, non-cancelled, future match
(`known_match_ids` in Postgres prevents duplicates across restarts). Voice
events when the API's `block_voice_channel` is "VC 1"/"VC 2" and
`ESPORTS_VC1`/`ESPORTS_VC2` are set, otherwise external ("wannspieltbig.de").
Event start = kickoff − 5 min (clamped to now+30s); auto-start when due
(3 failures → delete + recreate). **Ending is driven exclusively by
wannspieltbig, never by a time estimate**: the CS live-score loop ends the
event on the real finish (map score reached or `has_ended`), and any match
that leaves the API is ended via `_handle_match_finished`. If a finish signal
is missed the event lingers until ended manually (accepted over cutting a live
match short). The Discord event still needs a `scheduled_end_time` (Discord
auto-completes around it), so `_event_end_time` sets a deliberately generous
one — 90 min per map + 90 min slack — far past any realistic match length.
Reschedules edit the event (or end + recreate if it is already active and
moved > 1 h into the future). Cancelled → event deleted; match disappeared
from the API → treated as finished (end/delete + cleanup). Every poll a
still-`scheduled` event's start/end are reconciled back onto the current
formula (`_reconcile_event_schedule`) if they drift by > 60 s — so events
created by an older code version self-correct (e.g. the batch whose start sat
at the real kickoff instead of kickoff − 5 min). It is a no-op with no API
call once aligned, and is skipped in the last 2 min before the intended start
so it never races the auto-start.
A missing event is only recreated after 2 consecutive NotFound polls; stale
`event_to_match` entries are reconciled against live guild events on startup.
Events get a **4:1 cover image** composed from the club logo and the opponent
logo (`_build_event_cover_media` → `compose_event_cover_image`) — same
source as the 2:1 reminder versus image but with logos at 70 % scale and
pulled closer together for the wider event thumbnail. When the
images.weserv.nl proxy rejects the opponent logo (bogus/placeholder URL), the
bot falls back to fetching it directly. Event descriptions are plain text
with markdown links (`[wannspieltbig](…)`, `[HLTV](…)`), no leading separator
line.

**30-minute reminders:** fired in a 29–30 min window before kickoff (never
late, at most 1 min early). With `ESPORTS_FORUM_CHANNEL_ID` set (production),
each match gets a forum thread ("BIG vs X – CS"/"LoL"/"TM") with the reminder
message; the game-specific role ping (`PING_CS`/`PING_LOL`/`PING_TM`) goes as
a separate message into the thread, **30 s after** the reminder
(`REMINDER_PING_DELAY` — an immediate ping was suspected of not notifying
users). Fallback without forum channel: posted to the summary channel (ping
inside the message). Rendering: **Components V2** (`build_reminder_view`) —
one composed 2:1 **versus image** (club logo left, opponent right;
`compose_versus_image`, Pillow). The opponent logo is fetched through the
images.weserv.nl proxy because HLTV's CDN blocks server IPs
(`_build_reminder_media`, cached per match); on any failure (no
`team_b_logo_url`, proxy error, junk URL) it falls back to the old two-tile
gallery (`big.png` + raw logo URL). Heading with live countdown, link buttons
**Voice** (→ Discord event) and **wannspieltbig** (no HLTV button anymore).
Reschedules edit the message; after match end, channel reminders are deleted
and threads untracked (Discord archives them by inactivity).

**Weekly summary:** a single continuously-updated message in the summary
channel covering Monday–Sunday (Europe/Berlin), refreshed on every poll; on
week change the old message is deleted and a new one posted. Rendering:
**Components V2** (`build_weekly_view`) — header section with the
square-padded club logo (`big_square.png`), one block per day with
event-linked match lines. The message is edited in place on every poll cycle;
a new message is only posted when the week rolls over.

**CS live-score tracking:** starts automatically 4–5 min before every CS
match (no manual command). Posts a **Components V2** score message to
`ESPORTS_UPDATE_CHANNEL_ID`: heading with the current round score, a
monospace map table (per-map score, winner ✓, "● live" incl. overtime
target), and admin-only buttons ("X won round" / "Y won round" / manual score
modal). Map completion always requires a confirm/cancel step. Correct CS
overtime rules (12-12 → first to 16, 15-15 → 19, …). Every change is PUT back
to `https://wannspieltbig.de/api/matchmap_update/<matchmap_id>/` with Basic
auth (`WSB_User`/`WSB_PW` — note the exact env-var casing). A 30 s
`live_score_updater` loop syncs round/map scores from
`/api/match_livescore/` (external edits show up in Discord) and detects the
finish (map score reached, or `has_ended` with ≥ 1 map played) → winner
rendering, event ended, tracker removed. Trackers survive restarts via
Postgres; the map table's history is display-only in-memory state that the
livescore sync rebuilds from the API within 30 s. After a Gateway RESUME all
score messages are re-rendered so the buttons work again.

### Moderation (`cogs/moderation.py`)

Per-guild config lives in Postgres, managed via `/mod_dashboard` (admin only,
ephemeral). The dashboard is a **CV2 LayoutView** (`core/mod_views.py`,
`build_dashboard_view`): one section per feature with its status line and a
setup/disable toggle button as accessory. The channel/role pickers it edits
into are LayoutViews too — a CV2 message can never be edited back to
content/embeds, so every state of that message must stay CV2. Success/error
confirmations are small `notice_view` containers.

- **Member log** via a per-guild channel webhook: compact **CV2 containers**
  for join/leave/kick/ban/timeout/unban (`build_*_view` — bold first line,
  `-#` detail lines, avatar thumbnail; same fields/colors/optionality as the
  old embeds; kick vs. leave disambiguated via audit log within 10 s; ban
  reason via `fetch_ban`, moderators via audit log). **User references** use
  markdown links (`[display_name](https://discord.com/users/{id})`) for both
  the subject and the acting moderator — no more `<@id>` mentions or "Profil"
  link button. **Timestamps** use Discord's dynamic `<t:unix:R>` format:
  join shows account creation age, leave shows join time, timeout shows the
  end time — all rendered client-side and auto-updating. The webhook shows its
  own avatar (set at creation); the old `avatar_url="attachment://pb.png"` was
  never valid and always fell back. The configured channel is stored in the DB
  and reported to the dashboard.
- **Auto join role** per guild; assignment result is shown in the join log.
- **Honeypot**: event-driven via `on_member_update` — any member who receives
  the configured honeypot role in the **hardcoded** guild
  `624700952636817448` is instantly banned with 7-day message deletion.
  Reason: `"Autobann - user claimed the honeypot role. All messages up to
  7 days ago deleted"`. Time-to-ban is measured and logged.
- **Bot-trap**: event-driven via `on_message` — any user who posts in the
  configured bot-trap channel (hardcoded guild `624700952636817448`) is
  instantly banned with 7-day message deletion. A hardcoded announcement
  message (ID `1525846311537213600`) is exempt. Reason: `"Autobann - user
  posted to the bot-trap channel. All messages up to 7 days ago deleted"`.
  Time-to-ban (message receipt → ban API call) is measured in milliseconds
  and logged.
- `/clear <1-100>`: bulk delete (fails for messages > 14 days, admin only);
  confirmation is a one-line CV2 notice.

### Birthday (`cogs/birthday.py`)


Task runs at 08:00 **and** 09:00 UTC, but only acts when it is 10:00 in
Berlin (DST gate) → exactly one attempt per day. Reads the "Register"
worksheet (columns located by header: "Discord", "Geburtsdatum",
"Datum Austritt"); skips empty/"-" names, missing dates and members with an
exit date; accepts `dd.mm.yyyy` and `dd.mm`. Posts one gold **Components V2**
container ("Happy Birthday! …" with the `tabsSax` emote — emote *name* is
hardcoded, only the ID comes from env). Per-day dedup via Postgres, so a
restart around 10:00 cannot double-post. Also feeds
`upcoming_birthdays`/`recent_birthdays` to the dashboard. Known limitation:
Feb 29 birthdays are not celebrated in non-leap years.

### Finance / Kassenbuch (`cogs/finance.py`)

Functional port of the old BearsFinanz cron script. Own service account
(`config/kassenbuch_credentials.json`), own spreadsheet/worksheet. Daily
check at 06:00 UTC, posts **only on the 1st of the month**: a gold **CV2
container** for the previous month ("Kassenbericht {Monat} {Jahr}", one `-#`
line per transaction with German-formatted signed amount, opening→closing
balance, "### Monatsbilanz", sheet link button — all in one container).
Every 6 h it refreshes the dashboard status (current balance + transactions
of the last 90 days). No slash commands. Amount parsing expects German
currency format with a cents part ("1.234,56 €").

### Feedback (`cogs/feedback.py`, `core/api_server.py`)

`/feedback` command — opens an ephemeral **Components V2** menu with a subject
dropdown (Moderation, Match-Tracking, Verein, Sonstiges), an anonymity toggle
button (👤/🕶️), and a "Nachricht schreiben" button that opens a modal for the
message text. On submit the feedback is stored in Postgres (`feedback` table)
and the menu is deleted, leaving only an ephemeral confirmation.

A **lightweight aiohttp REST API** runs alongside the bot on port 8080
(`core/api_server.py`) exposing feedback management for the dashboard:
`GET /api/feedback`, `PATCH /api/feedback/{id}/read`,
`PATCH /api/feedback/{id}/status`, `PATCH /api/feedback/{id}/note`,
`GET /api/feedback/unread-count`. See [DATA_INTERFACE.md](DATA_INTERFACE.md)
for the full contract.

## Message Rendering (Components V2)

Everything the bot posts is CV2 now (`discord.ui.LayoutView`, requires
discord.py ≥ 2.6; CV2 messages cannot carry `content`/`embeds`, and cannot
be edited back to embeds): CS score tracker, match reminder (versus image),
weekly summary, birthday post, member log (via webhook), `/mod_dashboard`
incl. its picker/confirmation states, `/clear` confirmation, Kassenbuch
monthly report. Only plain-text error replies remain non-CV2.

Assets: `big.png` (original club logo — left half of the composed reminder
versus image, and the gallery fallback), `big_square.png` (square-padded
variant so weekly-summary thumbnails don't crop the paw), `pb.png` (unused
since the webhook-avatar attachment trick turned out to be invalid; kept in
the image).

## Slash Commands

- `/mod_dashboard` — moderation configuration (admin)
- `/feedback` — submit anonymous/identified feedback with subject
- `/clear <amount>` — delete 1–100 messages (admin)

(The former `/wannspieltbig_*` commands were removed; CS tracking is
automatic-only now.)

## Persistence (Postgres, `db/`)

`asyncpg` pool + repositories (`db/repositories/`), schema in
`db/schema.sql` (auto-applied by the postgres container on first start):

- moderation per-guild config (member log webhook + channel, auto join role,
  honeypot role, bot-trap channel)
- e-sports state: event/reminder/thread↔match maps, known/monitored match
  ids, weekly summary message id, active CS trackers
- birthday sent-dedup
- feedback submissions (subject, anonymity, message, status, admin_note)

## Logging & Status

- **No Discord webhook logging.** `ErrorTrackerHandler` in `bot.py` feeds the
  dashboard instead: every INFO+ record counts into
  `bot.counters.log_messages`, ERROR+ into `log_errors`, WARNING+ into the
  rolling `bot.error_log`.
- `core/status_reporter.py` writes a full snapshot to `data/status.json`
  every 15 s (atomic replace); rolling event logs are restored from the
  previous snapshot on startup. A **feedback REST API** (`core/api_server.py`)
  runs on port 8080 for dashboard read/write access. See
  [DATA_INTERFACE.md](DATA_INTERFACE.md).
- File logs in `logs/` with daily rotation, 30 days retention; each cog gets
  its own file (`esports.log`, `birthday.log`, …) plus the main
  `roaringbot.log`.

## Environment Configuration (`.env`, read by `core/config.py`)

```bash
DISCORD_TOKEN=...                    # required

# E-Sports
ESPORTS_ENABLED=true                 # default true
ESPORTS_API_URL=...                  # default match_upcoming endpoint
ESPORTS_POLL_INTERVAL_MINUTES=1      # default 5
ESPORTS_GUILD_ID=...                 # guild for events (else first with perms)
ESPORTS_SUMMARY_CHANNEL_ID=...       # weekly summary (+ reminder fallback)
ESPORTS_UPDATE_CHANNEL_ID=...        # CS score tracker messages
ESPORTS_FORUM_CHANNEL_ID=...         # reminder forum threads
ESPORTS_VC1=... / ESPORTS_VC2=...    # voice channels for "VC 1"/"VC 2" events
WSB_User=... / WSB_PW=...            # wannspieltbig Basic auth (exact casing!)
PING_CS=... / PING_LOL=... / PING_TM=...  # reminder ping role ids

# Birthday
BIRTHDAY_CHANNEL_ID=...              # cog disabled if unset
BIRTHDAY_SPREADSHEET_ID=...          # cog disabled if unset
GOOGLE_SERVICE_ACCOUNT_FILE=config/google_credentials.json
BIRTHDAY_EMOTE_ID=...

# Kassenbuch / Finance
KASSENBUCH_CHANNEL_ID=...            # cog disabled if unset
KASSENBUCH_SPREADSHEET_ID=...        # cog disabled if unset
KASSENBUCH_WORKSHEET_NAME=Kassenbuch
KASSENBUCH_SERVICE_ACCOUNT_FILE=config/kassenbuch_credentials.json

# Database (compose wires DB_HOST/DB_PORT into the bot container)
DB_USER=roaringbot
DB_PASSWORD=...
DB_NAME=roaringbot

# Optional
BOT_OWNER_ID=..., GUILD_ID=..., LOG_LEVEL=INFO,
MAX_CACHE_SIZE_MB=100, MAX_MEMORY_CACHE_ITEMS=50,
HTTP_TIMEOUT=30, MAX_HTTP_CONNECTIONS=100, MAX_HTTP_CONNECTIONS_PER_HOST=10
```

## Project Structure

```
RoaringBot/
├── bot.py                    # Bot class, logging, status wiring, cog loading
├── README.md / DATA_INTERFACE.md
├── docker-compose.yml        # bot + roaringbot-db (Postgres 16)
├── Dockerfile
├── big.png / big_square.png / pb.png
├── cogs/
│   ├── birthday.py           # daily birthday post (CV2)
│   ├── esports.py            # match monitoring, events, reminders, weekly
│   │                         #   summary, CS tracking (CV2 builders live here)
│   ├── feedback.py           # /feedback with CV2 menu + modal
│   └── moderation.py         # member log, join role, honeypot, bot-trap, /clear
├── core/
│   ├── config.py             # env-based config accessors
│   ├── api_server.py         # aiohttp REST API for dashboard feedback mgmt
│   ├── status_reporter.py    # data/status.json writer (dashboard interface)
│   ├── cache_manager.py, http_client.py, validation.py,
│   ├── colors.py, timezone_util.py, mod_views.py
├── db/
│   ├── connection.py         # asyncpg pool singleton
│   ├── schema.sql
│   └── repositories/         # feedback, moderation, esports, birthday, guild
│       ├── feedback_repository.py
├── scripts/migrate_data.py   # one-time JSON→Postgres migration
├── config/                   # credentials (gitignored)
├── data/                     # status.json + cache (gitignored)
└── logs/                     # rotated logs (gitignored)
```

## Development Notes

- Deploy: `docker compose up -d --build` (slash commands re-sync on startup,
  so removed commands disappear automatically).
- The dashboard (`/root/dashboard`) consumes `data/status.json` read-only
  and the feedback REST API on port 8080; when adding status fields or API
  routes, document them in DATA_INTERFACE.md.
- HTTP client (`core/http_client.py`): pooling, DNS cache, retry with
  backoff; API errors are counted per section in the status snapshot.
- PyNaCl warning at startup is expected (no voice support needed).
- `discord.py>=2.3.0` in requirements.txt is a floor; the CV2 rendering
  requires ≥ 2.6 at runtime.
