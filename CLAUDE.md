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
  moderation.py     # Member-Log, Join-Role, Honeypot, Bot-Trap, /clear, /spa1timo
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
- Event-Description: `[wannspieltbig](detail_url)` und `🔗 [HLTV](hltv_url)` (CS-only)
  in **einer Zeile** durch ` • ` getrennt — kein Zeilenumbruch zwischen den Links.
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
  `played_map_name` ist **optional** im Payload: Wenn die Site den Map-Namen
  noch nicht gesetzt hat, wird der PUT trotzdem gesendet (nur ohne dieses Feld) —
  sonst würden Score-Writes stillschweigend verworfen und der 30-s-Loop den
  lokalen Stand wieder zurücksetzen.
- Der Voice-Event-Name zeigt den Live-Stand (`_update_event_name_with_score` /
  `get_event_score_name`). Easter Egg: Score `7:1` (in dieser Reihenfolge) wird
  als `bra71l` angezeigt statt `7:1`; alle anderen Scores bleiben normal.
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
- **Match-ID-Reuse**: `_process_match_updates` cleared die ID aus dem Set, wenn
  der Match mit **zukünftiger `start_time`** wieder in der API auftaucht —
  wannspieltbig.de nutzt dann dieselbe match_id für ein neues Fixture (z. B.
  "BIG vs. TBA" → "BIG vs. magic"). Ohne das Clear bliebe der neue Match
  dauerhaft aus der Weekly-Summary gefiltert, obwohl sein Discord-Event existiert.

**Dashboard-Daten (`_compute_next_matches`):**
- `cleanly_finished`: `True` wenn in `_livescore_finished_ids`
- `has_discord_event`: `True` wenn `discord_event_id` gesetzt ODER in
  `_livescore_finished_ids`
- `voice_event_ok`, `reminder_ok`, `tracking_ok`: Timeline-basierte Health-Checks
- `issues`: Von `_match_health_issues()` — immer leer für Livescore-beendete Matches
- `reminder_missing` wird **nur im 0–30-Min-Fenster vor Kickoff** geflaggt
  (`0 < time_to_start <= 1800` — identisch zum Reminder-Sender). Ohne die untere
  Grenze wurden bereits gestartete, aber noch in der API verweilende Matches
  (nach Cleanup der Reminder bei start+4h) bis zum Verlassen der API fälschlich
  alarmiert (Aug-19-Storm).

**30-Min-Reminder:**
- Gefeuert im 29–30-Min-Fenster vor Kickoff. Forum-Thread mit Versus-Image
  (`compose_versus_image` in `core/versus_image.py` — lokale reine
  Bild-Komposition, 2:1, JPEG quality 85, game-spezifische Hintergründe,
  Schlagschatten, CS-Dreieck-Overlays). Game-Role-Ping **30 s verzögert**
  (`REMINDER_PING_DELAY`) in separater Nachricht.
- Opponent-Logo via images.weserv.nl-Proxy (HLTV-CDN blockt Server-IPs). Bei
  Proxy-Nicht-200 (z. B. Liquipedia blockt weserv) wird die Raw-URL direkt
  gefetcht (gleicher Direct-Fetch-Fallback wie `_build_event_cover_media`);
  erst wenn **beides** fehlschlägt → `None` → alte Two-Tile-Gallery
  (`big.png` + Opponent-Logo-URL).
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

**45-Min-WhatsApp-Ping:**
- Kleine CV2-Karte im `PING_WHATSAPP`-Channel 45 min vor Kickoff
  (`_check_for_whatsapp_pings`, Fenster `0 < time_to_start <= 2700`, bleibt
  für den gesamten 45-Min-Zeitraum offen). Button verlinkt auf die
  Share-Seite (`config.share_base_url`).
- Dedup via `_whatsapp_ping_sent` — ein Dict `match_id → Startzeit-ISO`
  (Stand zum Ping-Zeitpunkt), persistiert in `esports_state`.
  **Match-ID-Reuse**: Taucht ein Match mit **anderer zukünftiger Startzeit**
  wieder auf (z. B. "BIG vs. TBA" → "BIG vs. magic"), cleart
  `_process_match_updates` den Eintrag, damit das neue Fixture seinen eigenen
  Ping bekommt (Sep-2026: kein Ping für das magic-Fixture, weil der Flag aus
  dem TBA-Fixture stehen geblieben war). Legacy-Null-Einträge (altes
  Listen-Format) heilen sich beim ersten Poll selbst.

**Weekly Summary:** Eine durchgehend editierte Nachricht pro Woche (Mo–So
Europe/Berlin). Wochenwechsel → alte löschen, neue posten. CV2 mit Club-Logo
(`big_square.png`) und Tagesblöcken. Header: `## This Week` mit dem
Datumsintervall als `###`-Subtitle darunter + `-# Powered by wannspieltbig.de`
(kein Status-Link mehr). Button `later` öffnet einen ephemeren View mit allen
Matches nach der aktuellen Woche. Über `_is_upcoming_or_live` werden
**beendete/abgelaufene Matches gefiltert** (cancelled, Livescore-beendet,
`end_time` in der Vergangenheit, > 6 h alt ohne `end_time`) — die Übersicht
zeigt also keine vergangenen Matches der aktuellen Woche mehr.

**Event-Cover (2.5:1):** `_build_event_cover_media` → `compose_versus_image(..., h=640)` —
1600×640, JPEG. Design: **nur** Hintergrund + beide Team-Logos (via
`show_tournament=False, show_game_logo=False, show_info=False` — kein
Turnier-Label, kein Game-Logo, kein BO/Datum/Zeit). Design-Elemente
proportional skaliert (sf = 0.8).
Das Seitenverhältnis ist load-bearing: Discord zeigt Event-Cover im
Event-Header mit ~2.5:1 (800×320) — die 4:1-Variante (1600×400) wurde dort
verzerrt. Bei einer Cover-Kompositions-Änderung `EVENT_COVER_VERSION` bumpen;
`_reconcile_event_covers` lädt dann einmalig alle bestehenden Scheduled-Event-
Cover neu hoch (persistiert in `esports_state.event_cover_version`), sonst
würden Events mit stabilen Metadaten das alte Cover behalten. **Blindspot:**
Die Reconciliation läuft nur einmal pro Bump — Events, die danach von einem
älteren Build (z. B. vor dem Deployment der neuen Komposition) erstellt
wurden, behalten das alte Cover. Fix: `EVENT_COVER_VERSION` erneut bumpen →
nächster Restart lädt alle Covers neu hoch (Sep-2026: "BIG vs. magic" 4:1 →
2.5:1 geheilt).

**Spiegel-Modul:** `core/versus_image.py` ist gespiegelt im Share-Page-Service
[wannspieltbig-social-preview](https://github.com/RoaringBearsBIG/wannspieltbig-social-preview)
(dort `image.py` — der Service liefert die öffentlichen `bot.wannspieltbig.de`-Seiten
inkl. twitter:image-Variante). Änderungen an `compose_versus_image`/Loadern müssen
manuell in beide Richtungen nachgezogen werden. Der WhatsApp-Ping-Button im
Summary-Channel nutzt `config.share_base_url`.

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
- `/spa1timo`: Fun-Command, antwortet mit `"Lesen, Verstehen, Nachdenken,
  Schreiben (oder besser nicht)"`.

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
│                          #   validation.py, mod_views.py, versus_image.py
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
