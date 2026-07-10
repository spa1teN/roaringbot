# RoaringBot - Discord Bot

## Overview
RoaringBot is a Discord bot (Python, discord.py ≥ 2.6) for the BIG Bears community:
e-sports match monitoring for BIG via wannspieltbig.de, birthday reminders, a
Kassenbuch (finance) report, and moderation features. State lives in Postgres,
live status is exposed to the server dashboard via `data/status.json`
(see [DATA_INTERFACE.md](DATA_INTERFACE.md)).

Runs as two Docker containers (`docker compose up -d --build`):
`roaringbot` (the bot) and `roaringbot-db` (Postgres 16, schema auto-applied
from `db/schema.sql` on first start).

## Cogs

### 🎮 E-Sports (`cogs/esports.py`)
Polls `https://wannspieltbig.de/api/match_upcoming/` (all pages) every
`ESPORTS_POLL_INTERVAL_MINUTES` (production: 1 minute).

**API time handling (load-bearing, verified against the live API):**
- `first_map_at` carries German wall-clock digits regardless of its offset
  suffix — the offset is discarded and the digits re-interpreted as
  Europe/Berlin.
- `last_map_end` carries a genuine UTC (`Z`) timestamp and is trusted as-is;
  values earlier than start+15min are discarded (then event end falls back to
  start + 1h × best-of).
- Matches with missing `tournament`/`lineup_a` are skipped with an error log;
  missing `lineup_b` renders as "TBA". `lineup_b.team_logo_url` is kept for the
  reminder rendering.

**Discord events:** one scheduled event per new, non-cancelled, future match
(`known_match_ids` in Postgres prevents duplicates across restarts). Voice
events when the API's `block_voice_channel` is "VC 1"/"VC 2" and
`ESPORTS_VC1`/`ESPORTS_VC2` are set, otherwise external ("wannspieltbig.de").
Event start = kickoff − 5 min (clamped to now+30s); auto-start when due
(3 failures → delete + recreate), auto-end at `last_map_end` or 4 h after
kickoff as fallback. Reschedules edit the event (or end + recreate if it is
already active and moved > 1 h into the future). Cancelled → event deleted;
match disappeared from the API → treated as finished (end/delete + cleanup).
A missing event is only recreated after 2 consecutive NotFound polls; stale
`event_to_match` entries are reconciled against live guild events on startup.

**30-minute reminders:** fired in a 29–30 min window before kickoff (never
late, at most 1 min early). With `ESPORTS_FORUM_CHANNEL_ID` set (production),
each match gets a forum thread ("BIG vs X – Counter-Strike") with the reminder
message; the game-specific role ping (`PING_CS`/`PING_LOL`/`PING_TM`) goes as
a separate message into the thread. Fallback without forum channel: posted to
the summary channel (ping inside the message). Rendering: **Components V2**
(`build_reminder_view`) — media gallery with both team logos (`big.png`
attachment + `team_b_logo_url` if present), heading with live countdown, link
buttons Join Event / wannspieltbig.de / HLTV (HLTV only for CS). Reschedules
edit the message; after match end, channel reminders are deleted and threads
untracked (Discord archives them by inactivity).

**Weekly summary:** a single continuously-updated message in the summary
channel covering Monday–Sunday (Europe/Berlin), refreshed on every poll; on
week change the old message is deleted and a new one posted. Rendering:
**Components V2** (`build_weekly_view`) — header section with the
square-padded club logo (`big_square.png`), one block per day with
event-linked match lines. A pre-CV2 embed message keeps being edited in the
legacy embed format until the next regular re-post switches formats (the
legacy embed branch in `_update_existing_summary` can be deleted once the
first CV2 weekly message exists).

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

### 🛡️ Moderation (`cogs/moderation.py`)
Per-guild config lives in Postgres, managed via `/mod_dashboard` (interactive
buttons, admin only):
- **Member log** via a per-guild channel webhook: classic embeds for
  join/leave/kick/ban/timeout/unban (kick vs. leave disambiguated via audit
  log within 10 s; ban reason via `fetch_ban`, moderators via audit log).
  Join embeds carry a "Profil" **link button** (allowed on plain webhooks —
  only interactive components would need an application webhook). Sent with
  `pb.png` as avatar, fallback without.
- **Auto join role** per guild; assignment result is shown in the join embed.
- **Honeypot**: every 60 s, members holding the configured honeypot role in
  the **hardcoded** guild `624700952636817448` are banned ("Autobann").
- `/clear <1-100>`: bulk delete (fails for messages > 14 days, admin only).

Caveat: leave-duration is in-memory — only joins observed during the current
process lifetime produce a "time on server" value.

### 🎂 Birthday (`cogs/birthday.py`)
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

### 💰 Finance / Kassenbuch (`cogs/finance.py`)
Functional port of the old BearsFinanz cron script. Own service account
(`config/kassenbuch_credentials.json`), own spreadsheet/worksheet. Daily
check at 06:00 UTC, posts **only on the 1st of the month**: an embed report
for the previous month (every transaction as a line, opening/closing balance,
monthly balance) with a link button to the sheet. Every 6 h it refreshes the
dashboard status (current balance + transactions of the last 90 days).
No slash commands. Amount parsing expects German currency format with a cents
part ("1.234,56 €").

## Message rendering (Components V2 vs. embeds)
CV2 (`discord.ui.LayoutView`, requires discord.py ≥ 2.6; CV2 messages cannot
carry `content`/`embeds`, and cannot be edited back to embeds):
- CS score tracker, match reminder, weekly summary, birthday post.

Classic embeds (kept intentionally):
- member log (webhook), `/mod_dashboard` + setup confirmations, `/clear`
  confirmation, Kassenbuch monthly report.

Assets: `big.png` (original club logo, reminder gallery), `big_square.png`
(square-padded variant so thumbnails don't crop the paw), `pb.png` (webhook
avatar).

## Slash commands
- `/mod_dashboard` — moderation configuration (admin)
- `/clear <amount>` — delete 1–100 messages (admin)

(The former `/wannspieltbig_*` commands were removed 2026-07-10; CS tracking
is automatic-only now.)

## Persistence (Postgres, `db/`)
`asyncpg` pool + repositories (`db/repositories/`), schema in
`db/schema.sql` (auto-applied by the postgres container on first start):
- moderation per-guild config
- e-sports state: event/reminder/thread↔match maps, known/monitored match
  ids, weekly summary message id, active CS trackers
- birthday sent-dedup

`scripts/migrate_data.py` was the one-time migration from the old JSON files.

## Logging & status
- **No Discord webhook logging.** `ErrorTrackerHandler` in `bot.py` feeds the
  dashboard instead: every INFO+ record counts into
  `bot.counters.log_messages`, ERROR+ into `log_errors`, WARNING+ into the
  rolling `bot.error_log`.
- `core/status_reporter.py` writes a full snapshot to `data/status.json`
  every 15 s (atomic replace); rolling event logs are restored from the
  previous snapshot on startup. This file is the **only** interface external
  tools (the dashboard) should consume — see [DATA_INTERFACE.md](DATA_INTERFACE.md).
- File logs in `logs/` with daily rotation, 30 days retention; each cog gets
  its own file (`esports.log`, `birthday.log`, …) plus the main
  `roaringbot.log`.

## Environment configuration (`.env`, read by `core/config.py`)
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

## Project structure
```
RoaringBot/
├── bot.py                    # Bot class, logging, status wiring, cog loading
├── CLAUDE.md / DATA_INTERFACE.md
├── docker-compose.yml        # bot + roaringbot-db (Postgres 16)
├── Dockerfile
├── big.png / big_square.png / pb.png
├── cogs/
│   ├── birthday.py           # daily birthday post (CV2)
│   ├── esports.py            # match monitoring, events, reminders, weekly
│   │                         #   summary, CS tracking (CV2 builders live here)
│   ├── finance.py            # Kassenbuch monthly report + status
│   └── moderation.py         # member log, join role, honeypot, /clear
├── core/
│   ├── config.py             # env-based config accessors
│   ├── status_reporter.py    # data/status.json writer (dashboard interface)
│   ├── cache_manager.py, http_client.py, validation.py,
│   ├── colors.py, timezone_util.py, mod_views.py
├── db/
│   ├── connection.py         # asyncpg pool singleton
│   ├── schema.sql
│   └── repositories/         # moderation, esports, birthday, guild
├── scripts/migrate_data.py   # one-time JSON→Postgres migration
├── config/                   # credentials (gitignored)
├── data/                     # status.json + cache (gitignored)
└── logs/                     # rotated logs (gitignored)
```

## Development notes
- Deploy: `docker compose up -d --build` (slash commands re-sync on startup,
  so removed commands disappear automatically).
- The dashboard (`/root/dashboard`) consumes `data/status.json` read-only;
  when adding status fields, document them in DATA_INTERFACE.md.
- HTTP client (`core/http_client.py`): pooling, DNS cache, retry with
  backoff; API errors are counted per section in the status snapshot.
- PyNaCl warning at startup is expected (no voice support needed).

## Recent changes
- **2026-07-10**: CS score tracker, match reminder, weekly summary and
  birthday post migrated to **Components V2** (weekly summary switches
  lazily with the first new-week post). Reminder gallery shows both team
  logos; `big_square.png` added. `/wannspieltbig_*` commands and the dead
  `CSGameTracker.get_reminder_embed` removed; `LOG_WEBHOOK_URL` dropped.
  Member-log join embeds got a "Profil" link button. Dashboard's
  `next_matches` is no longer week-capped (always the next three).
- **2026-07-08/09** (Sonnet session): Postgres persistence (`db/`,
  `roaringbot-db`) replacing the JSON files; structured status reporting for
  the dashboard (`core/status_reporter.py`, DATA_INTERFACE.md) replacing
  Discord webhook logging; Kassenbuch cog ported from the BearsFinanz cron
  script; reminder forum threads; event-lifecycle hardening (NotFound
  double-check, reschedule-while-active, startup reconciliation); fixed
  `first_map_at`/`last_map_end` timezone semantics.
- **2026-02**: birthday cog added; map system removed.
