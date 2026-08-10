# RoaringBot — Discord Bot für BIG Bears

Python-Discord-Bot (discord.py ≥ 2.6, CV2-only Messages) für die BIG-eSports-Community:
Match-Tracking (wannspieltbig.de), Geburtstags-Posts, Kassenbuch-Report, Moderation
(Member-Log, Auto-Join-Role, Honeypot, Bot-Trap), User-Feedback-System.

Läuft als Docker-Container (`docker compose up -d --build`) mit Postgres 16
(`roaringbot-db`). Schreibt alle 15 s einen Status-Snapshot nach
`data/status.json` (via `core/status_reporter.py`) und stellt eine
aiohttp-REST-API auf Port 8080 für das Dashboard bereit.

## Architektur

```
cogs/               # Discord-Cogs (ein File pro Feature)
  esports.py        # 2846 Zeilen — der größte und komplexeste Cog
  birthday.py       # Täglicher Geburtstags-Post um 10:00 Berlin-Zeit
  moderation.py     # Member-Log, Join-Role, Honeypot, Bot-Trap, /clear
  finance.py        # Kassenbuch-Report am 1. des Monats
  feedback.py       # /feedback Slash-Command mit CV2-Modal
core/
  config.py         # Env-basierte Config-Accessoren
  api_server.py     # aiohttp REST API (Feedback-Management für Dashboard)
  status_reporter.py # data/status.json-Writer (Dashboard-Schnittstelle)
  mod_views.py      # CV2 LayoutView für /mod_dashboard
  colors.py         # Discord-Farbkonstanten
  http_client.py    # aiohttp-Pool mit Retry/Backoff
  timezone_util.py  # Europe/Berlin-Helfer
  cache_manager.py  # In-Memory-Cache
  validation.py     # Input-Validierung
db/
  schema.sql        # Wird beim ersten Start automatisch angewendet
  connection.py     # asyncpg-Pool-Singleton
  repositories/     # Pro-Feature-Repositories (esports, birthday, feedback, …)
bot.py              # Bot-Klasse, Error-Tracker, Cog-Loading, Status-Wiring
```

## Schlüsselkonzepte & Invarianten

### E-Sports (esports.py)

**Poll-Zyklus:** Alle `ESPORTS_POLL_INTERVAL_MINUTES` (prod: 1) wird
`wannspieltbig.de/api/match_upcoming/` abgefragt. Neue Matches → Discord-Event +
Reminder. Reschedules → Event/Reminder-Update. Verschwundene Matches → Cleanup.

**Zeitzonen — load-bearing:**
- `first_map_at`: Deutsche Wanduhr-Zeit (Offset-Suffix wird verworfen, Digits als
  Europe/Berlin reinterpretiert).
- `last_map_end`: Echte UTC (`Z`), aber unzuverlässig (zu früh) — wird NICHT zum
  Event-Beenden verwendet.

**Discord-Events:**
- Ein Scheduled-Event pro neuem, nicht-cancellten Match.
- Startzeit = Kickoff − 5 min (geclampt auf now+30s). Großzügiges
  `scheduled_end_time` (90 min/Map + 90 min Puffer) — nur für Discord-nötig.
- Voice-Events wenn API `block_voice_channel` "VC 1"/"VC 2" + Env-Vars gesetzt.
- **Ende ausschließlich durch wannspieltbig-Signale**, nie durch Zeitschätzung:
  CS-Livescore-Loop beendet bei echtem Finish; API-verschwundene Matches via
  `_handle_match_finished`.
- Missing event → erst nach 2 konsekutiven NotFound-Polls neu erstellen
  (`event_not_found_count`). **Transiente API-Fehler (503, 522, Timeout) werden
  NICHT als "event gone" gezählt** — nur echte 404er incrementieren den Counter.
  Gleiche 2-Strike-Regel gilt in `_update_discord_event` und
  `_check_event_status_updates`.
- `_reconcile_event_schedule`: Korrigiert Start/Ende jedes Polls (nur wenn > 60 s
  Drift, nie < 2 min vor Start) — Events alter Code-Versionen heilen sich selbst.
- **Duplicate-Event-Prevention**: `_create_discord_event` scannt vor Creation alle
  Guild-Events auf gleichen Namen + Startzeit (±2 h). Scan-Fehler (z. B. 503 beim
  `fetch_scheduled_events`) → **Abbruch** der Creation (kein Silent-Proceed).
  `_dedup_guild_events` läuft einmal pro Poll-Zyklus und löscht echte Duplikate
  (Name + Zeitfenster), behält das in `event_to_match` getrackte Event.

**CS Livescore-Tracking:**
- Startet 4–5 min vor jedem CS-Match automatisch (kein manueller Befehl).
- CV2-Score-Message mit Round-Score, Map-Tabelle, Admin-Buttons.
- Korrekte OT-Regeln (12-12 → first to 16, 15-15 → 19, …).
- Jede Änderung wird via PUT an `wannspieltbig.de/api/matchmap_update/<id>/`
  zurückgeschrieben (Basic Auth: `WSB_User`/`WSB_PW` — exakte Schreibweise!).
- 30-s `live_score_updater`-Loop synct von `/api/match_livescore/` und erkennt
  Finish → Winner-Rendering, Event beendet, Tracker entfernt.
- Tracker überleben Restarts via Postgres; Map-History ist In-Memory (wird in
  ≤ 30 s aus API neu aufgebaut).

**`_livescore_finished_ids` (Set[int]):**
- Match-IDs, die via Livescore sauber beendet wurden.
- `_end_match_event()` setzt `discord_event_id = None`, fügt ID in dieses Set.
- `_match_health_issues()` skipped Matches in diesem Set → leere Issues-Liste.
- `_compute_next_matches()` setzt `cleanly_finished = True` und
  `has_discord_event = True` für diese Matches → Dashboard zeigt grünen
  "Match erfolgreich beendet"-Chip statt falscher roter Fehler-Dots.
- Dieser Guard ist kritisch: Ohne ihn meldet das Dashboard "Discord event fehlt"
  und rote Voice-Event-Dots für sauber beendete Matches.

**Dashboard-Daten (`_compute_next_matches`):**
- `cleanly_finished`: `True` wenn in `_livescore_finished_ids`
- `has_discord_event`: `True` wenn `discord_event_id` gesetzt ODER in
  `_livescore_finished_ids`
- `voice_event_ok`, `reminder_ok`, `tracking_ok`: Timeline-basierte Health-Checks
- `issues`: Von `_match_health_issues()` — immer leer für Livescore-beendete Matches

**30-Min-Reminder:**
- Gefeuert im 29–30-Min-Fenster vor Kickoff. Forum-Thread mit Versus-Image
  (`compose_versus_image` in `core/share_pages.py` — geteilte Funktion für
  Discord und WhatsApp, 2:1, JPEG quality 85, game-spezifische Hintergründe,
  Schlagschatten, CS-Dreieck-Overlays). Game-Role-Ping **30 s verzögert**
  (`REMINDER_PING_DELAY`) in separater Nachricht.
- Opponent-Logo via images.weserv.nl-Proxy (HLTV-CDN blockt Server-IPs); Fallback:
  alte Two-Tile-Gallery.
- **Ping-Card (CS-Large-Role-Workaround)**: CV2-Karte im Summary-Channel mit
  "Match Thread"-Button, umgeht Discords 250-Member-Thread-Ping-Limit.
- **Reschedule/Update**: `_edit_reminder_message` aktualisiert bei Time- oder
  Opponent-Änderung **sowohl** die Thread-Nachricht (`reminder_message_id`) **als
  auch** die Ping-Card (`ping_message_id`) und **benennt den Thread um**, falls
  `team_a`/`team_b` sich geändert haben. Frisches `discord.File` pro Edit
  (BytesIO-Streams sind nach dem ersten Edit verbraucht).
- **Startup-Reconciliation**: `_reconcile_all_reminders` bringt nach dem ersten
  Poll nach Restart alle existierenden Reminder/Ping-Cards/Thread-Titles auf den
  aktuellen API-Stand — fängt Änderungen, die während Downtime/Crash passiert sind.
- Reminder + Ping-Card werden automatisch gelöscht, wenn der Match endet
  (`_check_for_reminder_cleanup`).

**Weekly Summary:** Eine durchgehend editierte Nachricht pro Woche (Mo–So
Europe/Berlin). Wochenwechsel → alte löschen, neue posten. CV2 mit Club-Logo
(`big_square.png`) und Tagesblöcken.

**Event-Cover (4:1):** `_build_event_cover_media` → `compose_versus_image(..., h=400)` —
gleiche geteilte Funktion wie der 2:1-Reminder, 1600×400, JPEG. Design-Elemente
proportional skaliert (sf = 0.5).

### Geburtstage (birthday.py)

- Task läuft um 08:00 **und** 09:00 UTC, handelt aber nur wenn es 10:00 in Berlin
  ist (DST-Gate) → exakt ein Versuch pro Tag.
- Google Sheets "Register"-Worksheet; Spalten per Header lokalisiert.
- Dedup via Postgres (verhindert Doppel-Post bei Restart um 10:00).
- Bekannte Einschränkung: 29. Februar wird in Nicht-Schaltjahren nicht gefeiert.
- Dashboard-Daten: `upcoming_birthdays`, `recent_birthdays` (mit `turning_age`).

### Moderation (moderation.py)

- **Member-Log** via Webhook: CV2-Container mit Markdown-User-Referenzen
  (`[name](https://discord.com/users/{id})`), dynamische `<t:unix:R>`-Timestamps.
  Join/Leave/Kick/Ban/Timeout/Unban mit Audit-Log-Disambiguierung.
- **Honeypot**: `on_member_update` — User bekommt Honeypot-Role → Instant-Ban
  (7d Nachrichten-Löschung), nur in Guild `624700952636817448`.
- **Bot-Trap**: `on_message` — Post im Bot-Trap-Channel → Instant-Ban (7d),
  selbe Guild. Nachricht `1525846311537213600` ist exempt.
- `/mod_dashboard`: CV2 LayoutView (ephemeral, admin-only) → Feature-Toggles mit
  Channel/Role-Pickern. Alle States sind CV2 (kein Edit zurück zu Embeds möglich).
- `/clear <1-100>`: Bulk-Delete (admin-only, schlägt fehl für > 14 Tage alte Messages).

### Feedback (feedback.py + core/api_server.py)

- `/feedback` → CV2-Modal: Subject-Dropdown, Anonymity-Toggle, Nachricht.
- aiohttp-API auf Port 8080: `GET/PATCH /api/feedback/*`, `GET /api/feedback/unread-count`.
- Dashboard (`/root/dashboard`) konsumiert diese API fürs Feedback-Management.

### Finance (finance.py)

- Eigener Service-Account (`kassenbuch_credentials.json`). Täglicher Check um
  06:00 UTC, postet **nur am 1. des Monats** einen CV2-Kassenbericht für den
  Vormonat. Dashboard-Refresh alle 6 h. Deutsche Währungsformat-Parsing ("1.234,56 €").

## Status-Reporting

`core/status_reporter.py` schreibt `data/status.json` (atomic replace) alle 15 s.
Das Dashboard (`/root/dashboard`) mounted das Verzeichnis read-only. `generated_at`
wird vom Dashboard auf Staleness geprüft (> 120 s → bot down).

Die REST-API (`core/api_server.py`) läuft im selben Prozess auf Port 8080 und
wird vom Dashboard für Feedback-CRUD genutzt.

## Development

```bash
cp .env.example .env   # DISCORD_TOKEN, DB_PASSWORD sind Pflicht
docker compose up -d --build
```

- Cogs deaktivieren sich selbst wenn ihre Channel/Spreadsheet-IDs fehlen.
- Slash-Commands syncen beim Startup → gelöschte Commands verschwinden automatisch.
- `discord.py>=2.3.0` in requirements.txt ist ein Floor; CV2 braucht ≥ 2.6 zur Laufzeit.
- PyNaCl-Warning beim Startup ist erwartet (kein Voice-Support nötig).
- Kein Discord-Webhook-Logging. `ErrorTrackerHandler` in `bot.py` feeded das
  Dashboard: INFO+ → `bot.counters.log_messages`, ERROR+ → `log_errors`,
  WARNING+ → rolling `bot.error_log`.

## Datei-Layout (Produktionscode)

```
RoaringBot/
├── bot.py                 # Bot-Klasse, Error-Tracker, Cog-Loading
├── CLAUDE.md
├── DATA_INTERFACE.md      # API- und Status-JSON-Contract
├── Dockerfile / docker-compose.yml
├── resources/             # big.png, big_square.png, pb.png, tba.png,
│                          #   cs/lol/tm-bg.jpg, cs/lol/tm-logo.png
├── cogs/                  # birthday.py, esports.py, feedback.py, finance.py, moderation.py
├── core/                  # api_server.py, config.py, status_reporter.py, colors.py,
│                          #   http_client.py, timezone_util.py, cache_manager.py,
│                          #   validation.py, mod_views.py, share_pages.py
├── db/                    # schema.sql, connection.py, repositories/
├── scripts/               # migrate_data.py (einmalig JSON→Postgres)
├── config/                # Credentials (gitignored)
├── data/                  # status.json + Cache (gitignored)
└── logs/                  # Rotierte Logs, 30d Retention (gitignored)
```

## Wichtige Umgebungsvariablen

Siehe `.env.example`. Kritische Besonderheiten:

- `WSB_User` / `WSB_PW` — exakte Schreibweise! Basic Auth für wannspieltbig-API-PUTs.
- `ESPORTS_VC1` / `ESPORTS_VC2` — Voice-Channel-IDs für "VC 1"/"VC 2"-Events.
- `PING_CS` / `PING_LOL` / `PING_TM` — Game-spezifische Reminder-Ping-Roles.
- `REMINDER_PING_DELAY` — Sekunden Verzögerung zwischen Reminder und Ping (Default 60, hardcoded in `esports.py`).
- `BIRTHDAY_EMOTE_ID` — Nur die ID; der Emote-Name (`tabsSax`) ist hardcoded.
- `ESPORTS_FORUM_CHANNEL_ID` — Fehlt → Reminder fallen zurück auf Summary-Channel.
