# RoaringBot Data Interface

RoaringBot schreibt alle 15 Sekunden einen vollständigen Status-Snapshot nach
**`data/status.json`** (relativ zum Bot-Arbeitsverzeichnis, also
`/root/roaringbot/data/status.json` auf dem Host — der Ordner ist bereits als
Volume in `docker-compose.yml` gemountet: `./data:/app/data`).

Das ist die primäre Schnittstelle, die externe Tools (z. B. das Dashboard)
nutzen sollten, um den Live-Zustand des Bots abzufragen. Für schreibende
Zugriffe (Feedback-Status ändern, Notizen setzen) gibt es zusätzlich eine
REST-API — siehe [Feedback API](#feedback-api).

## Funktionsweise

- Implementiert in [`core/status_reporter.py`](core/status_reporter.py).
- Jedes Modul (Cog inkl. `cogs/finance.py`, `core/http_client.py`, `core/cache_manager.py`, `bot.py`)
  meldet Ereignisse über zwei einfache Methoden auf dem globalen Singleton
  `status_reporter`:
  - `status_reporter.record(section, **fields)` — setzt/überschreibt Felder
    in einer benannten Sektion (z. B. letzter Zeitstempel, letzter Fehler,
    aktueller Zustand).
  - `status_reporter.bump_counter(section, counter)` — zählt ein Ereignis für
    rollierende Fehlerraten (liefert automatisch `15m`/`1h`/`24h`-Fenster).
- Ein Hintergrund-Task (`_writer_loop`, gestartet in `bot.py:setup_hook`)
  schreibt alle 15s den aktuellen Zustand atomar (`tempfile` + `os.replace`)
  nach `data/status.json`.
- Fehlt ein Feld/eine Sektion in der Datei, wurde das entsprechende Ereignis
  seit dem letzten Bot-Start noch nicht ausgelöst (z. B. `birthday` erst nach
  dem ersten täglichen Check vollständig).

## Datei lesen

**Direkt (empfohlen für das Dashboard):** `data/` read-only in den
konsumierenden Container mounten, dann die JSON-Datei einlesen. Bereits
umgesetzt in `dashboard/docker-compose.yml`:

```yaml
volumes:
  - /root/roaringbot/data:/data/roaringbot:ro
```

Das Dashboard liest die Datei in `dashboard/app/roaringbot_status.py` und
stellt sie unter `GET /api/roaringbot/status` bereit (ergänzt `available`
und `stale`, siehe unten).

**Frisch/veraltet erkennen:** `generated_at` mit der aktuellen Zeit
vergleichen. Da der Writer alle 15s läuft, bedeutet ein Alter > ~2 Minuten,
dass der Bot hängt oder abgestürzt ist, ohne dass der Container das meldet
(das Dashboard macht das bereits über `stale: true/false`).

## Schema

Jede Sektion hat automatisch ein Feld `updated_at` (ISO-8601 UTC,
`YYYY-MM-DDTHH:MM:SSZ`) — der Zeitpunkt des letzten `record()`-Aufrufs für
diese Sektion. `counters`-Objekte liefern rollierende Zählungen der letzten
15 Minuten / 1 Stunde / 24 Stunden.

```jsonc
{
  "generated_at": "2026-07-08T22:15:45Z",   // Zeitpunkt dieses Snapshots
  "uptime_seconds": 331,                     // Sekunden seit Prozessstart

  "bot": {
    "updated_at": "2026-07-08T22:10:18Z",
    "user": "Roaring Bot#8113",
    "user_id": 1409734087966457886,
    "guild_count": 2,
    "latency_ms": 110,
    "gateway_status": "connected",           // "connected" | "disconnected"
    "loaded_cogs": ["ModerationCog", "EsportsCog", "BirthdayCog"],
    "counters": {                            // nur vorhanden nach erstem Ereignis
      "reconnects": {"15m": 0, "1h": 0, "24h": 0},
      "command_errors": {"15m": 0, "1h": 0, "24h": 0},
      "errors": {"15m": 0, "1h": 0, "24h": 0},        // nur ungefangene Discord-Event-Fehler (on_error)
      "log_errors": {"15m": 0, "1h": 0, "24h": 3},    // jeder ERROR+/CRITICAL-Log-Eintrag im gesamten Bot (ersetzt das alte Discord-Webhook-Logging)
      "log_messages": {"15m": 12, "1h": 340, "24h": 8100}  // jeder INFO+-Log-Eintrag - "Aktivität", garantiert nie leer solange der Bot läuft
    },
    "error_log": [                           // rollierendes Log der letzten WARNING+-Einträge, für den Dashboard-Graphen-Klick-Popup
      {"at": "2026-07-08T22:10:18Z", "level": "ERROR", "logger": "roaringbot.esports", "message": "Error in match monitoring: ..."}
    ]
  },

  "birthday": {
    "updated_at": "2026-07-08T22:10:15Z",
    "sheets_connected": true,                // Google-Sheets-Auth beim Start ok?
    "last_error": null,
    "channel_id": 1152147198218342430,
    "spreadsheet_id": "1HsxRMInz6...",
    "last_check_at": "2026-07-08T10:00:03Z", // erst nach erstem täglichen Lauf
    "last_check_result": "no_birthdays",     // "sent" | "no_birthdays" | "error"
    "invalid_date_entries": 0,               // Anzahl Zeilen mit kaputtem Datumsformat
    "registered_entries": 42,
    "last_birthday_names": ["Max"],          // nur gesetzt bei last_check_result == "sent"
    "send_errors": [],                       // nicht-leer wenn der channel.send() für den heutigen Post fehlschlug
    "upcoming_birthdays": [                  // strikt zukünftig (nicht heute), max. 5, aufsteigend
      {"name": "Anna", "date": "12.08", "date_iso": "2026-08-12", "days_until": 3}
    ],
    "recent_birthdays": [                    // letzte Vorkommen inkl. heute, bis zu 30 Tage zurück
      {"name": "Max", "date": "08.07", "date_iso": "2026-07-08", "days_since": 0}
    ],
    "sent_log": [                            // rollierendes Log: ein Eintrag pro tatsächlich gesendetem Post
      {"at": "2026-07-08T08:00:04Z", "name": "Max", "date_iso": "2026-07-08"}
    ]
  },

  "esports": {
    "updated_at": "2026-07-08T22:15:21Z",
    "monitoring_enabled": true,
    "poll_interval_minutes": 1,
    "last_poll_at": "2026-07-08T22:15:18Z",       // match_monitor-Loop
    "last_poll_success": true,
    "last_poll_error": null,
    "last_livescore_poll_at": "2026-07-08T22:15:20Z",  // live_score_updater-Loop (nur bei aktiven CS-Matches)
    "total_matches": 46,                     // ALLE Matches im lokalen Cache, inkl. wochenaltem API-Datenmüll das wannspieltbig.de nie bereinigt
    "active_matches": 46,                    // nicht abgesagt (gleiches Problem wie total_matches)
    "active_discord_events": 7,
    "scheduled_matches": 6,                  // Matches innerhalb der aktuellen Woche + laufend - das, was das Dashboard als "geplante Matches" zeigt
    "scheduled_discord_events": 6,           // davon mit tatsächlich existierendem Discord-Event
    "active_cs_trackers": 0,
    "weekly_summary_last_updated": "2026-07-08T22:15:21Z",
    "weekly_summary_message_id": 1523448617246134449,
    "weekly_summary_last_error": null,
    "last_reminder_sent_at": "2026-07-08T18:00:02Z",
    "last_reminder_match": "BIG vs. TBA",
    "cs_trackers": [                         // Snapshot aller aktiven CS-Score-Tracker
      {
        "match_id": 2319,
        "teams": "BIG vs. TBA",
        "map": 1,
        "score": "7-5",
        "maps": "0-0",
        "is_finished": false
      }
    ],
    "next_matches": [                        // alle anstehenden/laufenden Matches, mit Health-Check
      {
        "match_id": 2314,
        "teams": "BIG vs. TeamOrangeGaming",
        "tournament": "ESL Pro League",
        "game": "LoL",
        "start_time": "2026-07-09T18:00:00Z",
        "detail_url": "https://wannspieltbig.de/...",
        "is_live": true,
        "has_discord_event": true,
        "reminder_at": "2026-07-09T17:30:00Z",   // Kickoff - 30min
        "reminder_ok": false,                     // true sobald reminder_message_id/forum_thread_id gesetzt ist
        "tracking_at": null,                      // Kickoff - 5min, nur bei game == "cs", sonst null
        "tracking_ok": null,                      // null wenn nicht CS
        "voice_event_at": "2026-07-09T17:55:00Z", // Kickoff - 5min (geclamped auf jetzt+30s)
        "voice_event_ok": true,                   // true sobald der Discord-Event-Status != "scheduled" ist
        "live_score": null,                       // bei laufendem CS-Match: {"map":1,"map_name":"Cache","score":"7-5","maps":"0-0"}
                                                  // map_name kann null sein, solange die Map noch nicht feststeht → Anzeige fällt auf "Map N" zurück
        // issues: leer wenn alles ok, sonst eine Teilmenge von:
        // "no_discord_event" | "reminder_missing" | "event_not_started" | "tracking_missing" (nur CS)
        // jedes erkannte Issue wird zusätzlich als log.error geloggt (taucht im Fehler-Log-Graphen auf)
        "issues": []
      }
    ],
    "counters": {
      "api_errors": {"15m": 0, "1h": 0, "24h": 3},
      "reminder_errors": {"15m": 0, "1h": 0, "24h": 0},
      // Score-Herkunft des CS-Trackers:
      "score_updates_from_api": {"15m": 0, "1h": 2, "24h": 35},  // Sync hat Stände von wannspieltbig übernommen
      "score_updates_to_api": {"15m": 0, "1h": 0, "24h": 0}      // Button-Klicks haben Stände zu wannspieltbig geschrieben (PUT)
    }
  },

  "moderation": {
    "updated_at": "2026-07-08T22:15:18Z",
    "honeypot_loop_alive": true,             // Heartbeat des Auto-Ban-Loops
    "honeypot_status": "ok",                 // "ok" | "disabled" | "error", zuletzt beobachteter Zustand
    "honeypot_last_error": null,
    "member_log_status": "ok",               // "ok" | "disabled" | "error", zuletzt beobachteter Webhook-Versand
    "member_log_last_error": null,
    "member_log_channel_id": 123456789,      // Channel in den der Webhook postet; null wenn disabled
    "bot_trap_channel_id": null,             // Bot-Trap-Channel; null wenn disabled
    "bot_trap_status": "disabled",           // "ok" | "disabled" | "error"
    "bot_trap_last_error": null,
    "join_role_status": "ok",                // "ok" | "disabled" | "error", zuletzt beobachtete Rollenvergabe
    "join_role_last_error": null,
    "last_clear_count": 12,                  // letztes /clear-Kommando
    "last_clear_channel_id": 123456789,
    "last_clear_by": "admin#0001",
    "counters": {
      "joins": {"15m": 0, "1h": 1, "24h": 5},
      "leaves": {"15m": 0, "1h": 0, "24h": 2},
      "bans": {"15m": 0, "1h": 0, "24h": 0},
      "kicks": {"15m": 0, "1h": 0, "24h": 0},
      "timeouts": {"15m": 0, "1h": 0, "24h": 1},
      "unbans": {"15m": 0, "1h": 0, "24h": 0},
      "honeypot_bans": {"15m": 0, "1h": 0, "24h": 0},
      "bot_trap_bans": {"15m": 0, "1h": 0, "24h": 0}
    },
    "events": [                              // rollierendes Log (max_len=300): join/leave/kick/ban/timeout/unban/bot_trap_ban/honeypot_ban
      {"at": "2026-07-08T22:10:03Z", "type": "honeypot_ban", "user": "spammer#0001", "user_id": 123, "guild": "Die Grünen", "time_to_ban_ms": 178}
    ]
  },

  "http": {
    "last_error": "Timeout: https://wannspieltbig.de/api/match_upcoming/",
    "counters": {
      "requests": {"15m": 18, "1h": 78, "24h": 1900},   // erfolgreiche Requests
      "timeouts": {"15m": 0, "1h": 0, "24h": 4},
      "connection_errors": {"15m": 0, "1h": 0, "24h": 0},
      "other_errors": {"15m": 0, "1h": 0, "24h": 0}
    }
  },

  "cache": {
    "updated_at": "2026-07-08T22:10:15Z",
    "memory_items": 0,
    "memory_max_items": 50,
    "file_mb": 0.0,
    "file_max_mb": 100
  },

  "finance": {
    "updated_at": "2026-07-09T06:00:12Z",
    "sheets_connected": true,                // Google-Sheets-Auth (Kassenbuch) beim Start ok?
    "last_error": null,
    "current_balance": "3.609,05 €",         // Saldo-Spalte der jüngsten Buchung (String wie im Sheet)
    "current_balance_date": "2026-06-30",
    "transactions_recent": [                 // alle Buchungen der letzten RECENT_DAYS Tage (aktuell 90), älteste zuerst
      {
        "date": "2026-06-30",
        "amount": "-1.00",                   // vorzeichenbehafteter Betrag als String (Decimal)
        "category": "Bankgebühren",
        "note": "",
        "saldo": "3.609,05 €"
      }
    ],
    "last_report_result": "sent",            // "sent" | "error", erst nach erstem Monatsreport gesetzt
    "last_report_error": null,
    "last_report_at": "2026-07-01T06:00:04Z",
    "last_report_month": "2026-06",
    "last_report_bilanz": "+42.50 €",        // Monatsbilanz des letzten gesendeten Reports (Summe der Monats-Transaktionen)
    "report_log": [                          // rollierendes Log aller tatsächlich gesendeten Monatsreports
      {"at": "2026-07-01T06:00:04Z", "month": "2026-06", "sent_at": "2026-07-01T06:00:04Z"}
    ]
  }
  "feedback": {
    "updated_at": "2026-07-09T12:00:00Z",
    "bot_avatar_url": "https://cdn.discordapp.com/avatars/1409734087966457886/…",  // Bot-eigener Avatar für Dashboard-Darstellung
    "last_submission_at": "2026-07-09T11:58:00Z",  // zuletzt erfolgreich gespeichert, ISO 8601; fehlt vor erstem Submit
    "last_error": null,                              // null | "DB not connected" | "DB insert failed"
    "counters": {                                    // nur vorhanden nach erstem Ereignis
      "submissions": {"15m": 3, "1h": 8, "24h": 25},
      "submission_errors": {"15m": 0, "1h": 0, "24h": 1}
    },
    "guilds": [                              // per-guild aggregate counts mit Status-Breakdown
      {
        "guild_id": "1374489236215955506",   // Discord-Snowflake als String (JS-safe)
        "guild_name": "Die Grünen",
        "guild_avatar_url": "https://cdn.discordapp.com/icons/…",  // Guild-Icon; null wenn keins gesetzt
        "total": 12,
        "new": 3,
        "important": 1,
        "in_progress": 2,
        "archived": 6
      }
    ],
    "entries": [                             // letzte 20 Einsendungen, neueste zuerst
      {
        "id": 42,
        "guild_id": 1374489236215955506,     // BIGINT als Zahl (PostgreSQL-native), nicht String
        "guild_name": "Die Grünen",          // aus Discord-Cache angereichert
        "guild_avatar_url": "https://cdn.discordapp.com/icons/…",
        "user_id": 485051896655249419,       // 0 wenn is_anonymous == true
        "user_name": "admin",                // Discord Display-Name; null wenn anonymous
        "user_avatar_url": "https://cdn.discordapp.com/avatars/…",  // null wenn anonymous
        "is_anonymous": false,
        "subject": "moderation",             // "moderation" | "match_tracking" | "verein" | "other"
        "message": "…",                      // auf 200 Zeichen gekürzt
        "status": "new",                     // "new" | "important" | "in_progress" | "archived"
        "read": false,
        "admin_note": null,                  // interne Notiz vom Dashboard-Admin, null wenn keine
        "created_at": "2026-07-09T12:00:00+00:00"
      }
    ]
  }
}
```

### Wichtige abgeleitete Kennzahlen für ein Dashboard

| Anzeige | Berechnung |
|---|---|
| Bot online/offline | `bot.gateway_status == "connected"` **und** `generated_at` nicht älter als ~2 Min |
| E-Sports-Polling hängt | `esports.last_poll_at` älter als `2 × poll_interval_minutes` |
| API-Ausfall (wie am 03.–05.07.) | `http.counters.timeouts["15m"]` oder `esports.counters.api_errors["15m"]` > 0 über mehrere Snapshots hinweg |
| Feedback-Feature kaputt | `feedback.last_error` gesetzt oder `feedback.counters.submission_errors["1h"]` > 0 |
| Geburtstags-Feature kaputt | `birthday.sheets_connected == false` oder `birthday.last_check_result == "error"` |
| CS-Tracking aktiv | `esports.active_cs_trackers > 0`, Details in `esports.cs_trackers[]` |
| Reconnect-Häufung | `bot.counters.reconnects["1h"]` > 2–3 |
| Kassenbuch-Feature kaputt | `finance.sheets_connected == false` oder `finance.last_error` gesetzt |
| Monatsreport verpasst | `finance.last_report_month` entspricht nicht dem Vormonat, obwohl der 1. bereits vergangen ist |
| Ungelesenes Feedback | `feedback.guilds[].new > 0` — zeigt an, in welchen Guilds neue Einsendungen warten |


## Feedback API

Für schreibende Zugriffe (Status ändern, als gelesen markieren, Admin-Notizen
setzen) läuft im Bot-Prozess ein leichtgewichtiger aiohttp-HTTP-Server auf
Port **8080** (implementiert in [`core/api_server.py`](core/api_server.py)).
Er teilt sich den asyncio-Event-Loop mit dem Discord-Client.

### Endpunkte

| Methode | Pfad | Query/Body | Antwort |
|---|---|---|---|
| `GET` | `/api/feedback` | `?guild_id=X` (optional) | `[{id, guild_id, guild_name, guild_avatar_url, user_id, user_name, user_avatar_url, is_anonymous, subject, message, status, read, admin_note, created_at}]` |
| `GET` | `/api/feedback/unread-count` | `?guild_id=X` (required) | `{"count": 3}` |
| `PATCH` | `/api/feedback/{id}/read` | — | `{"ok": true}` |
| `PATCH` | `/api/feedback/{id}/status` | Query `?status=X` oder Body `{"status": "X"}` | `{"ok": true}` |
| `PATCH` | `/api/feedback/{id}/note` | Body `{"note": "…"}` | `{"ok": true}` |
| `GET` | `/api/bot/avatar` | — | `{"bot_avatar_url": "…"}` |

Alle Antworten sind `application/json`. Fehler liefern HTTP 400/500/503 mit
`{"error": "…"}` oder `{"ok": false, "error": "…"}`.

**Status-Werte:** `new` | `important` | `in_progress` | `archived`

### Netzwerk-Integration für `~/dashboard/`

Der Bot-Container ist Mitglied des `dashboard-network` (externes Netzwerk in
`docker-compose.yml`). Das Dashboard kann die API unter
`http://roaringbot:8080` erreichen, sobald es ebenfalls diesem Netzwerk
beitritt:

```yaml
# dashboard/docker-compose.yml — hinzufügen:
networks:
  - roaringbot-network   # external: true (wird von RoaringBot verwaltet)
```

Empfohlene Proxy-Endpoints im Dashboard (`dashboard/app/main.py`):

| Dashboard-Route | Proxy-Ziel |
|---|---|
| `GET /api/roaringbot/feedback/list?guild_id=X` | `GET http://roaringbot:8080/api/feedback?guild_id=X` |
| `GET /api/roaringbot/feedback/unread-count?guild_id=X` | `GET http://roaringbot:8080/api/feedback/unread-count?guild_id=X` |
| `PATCH /api/roaringbot/feedback/{id}/read` | `PATCH http://roaringbot:8080/api/feedback/{id}/read` |
| `PATCH /api/roaringbot/feedback/{id}/status?status=X` | `PATCH http://roaringbot:8080/api/feedback/{id}/status?status=X` |
| `PATCH /api/roaringbot/feedback/{id}/note` | `PATCH http://roaringbot:8080/api/feedback/{id}/note` |

Die Proxy-Endpoints sind analog zu den bestehenden Tausendsassa-Feedback-Routen
(`/api/tausendsassa/feedback/…`) aufgebaut.

## WhatsApp Share Pages (öffentlich)

Die öffentlichen Share-Seiten unter `bot.wannspieltbig.de` laufen seit
2026-08 als **eigener Service** (`wannspieltbig-social-preview`, Repo
`github.com/RoaringBearsBIG/wannspieltbig-social-preview`) — nginx proxied den
Subdomain direkt zu dessen Container. Routen-Vertrag siehe dessen
`DATA_INTERFACE.md`.

RoaringBot behält nur die reine Bild-Komposition für Discord
(`core/versus_image.py` — gespiegelt im Service als `image.py`; Änderungen
müssen manuell in beide Richtungen nachgezogen werden) und den
WhatsApp-Ping-Button (nutzt `SHARE_BASE_URL`).

## Konsum durch das Dashboard

- **Status (read-only):** Volume-Mount `./data:/data/roaringbot:ro` →
  `dashboard/app/roaringbot_status.py` → `GET /api/roaringbot/status`.
  Prüft Existenz, Parsing-Fehler und Alter (`stale`-Flag, Schwelle 120s).
- **Feedback (read/write):** REST-API auf Port 8080 im Bot-Container.
  Dashboard proxyed die Endpunkte (siehe [Feedback API](#feedback-api)).
- Antwort bei fehlender/kaputter Datei: `{"available": false, "reason": "…"}` statt eines Fehlers — das Frontend kann so unterscheiden zwischen "Bot nie gestartet" / "Volume nicht gemountet" und "Bot läuft, aber Feature X noch nie ausgelöst".

## Erweitern

Neue Signale hinzufügen: im jeweiligen Cog/Modul
`from core.status_reporter import status_reporter` importieren und an der
relevanten Stelle `status_reporter.record(...)` bzw. `.bump_counter(...)`
aufrufen — keine Änderung an `status_reporter.py` selbst nötig. Neue
Sektionen erscheinen automatisch im nächsten Snapshot.

Der `feedback`-Abschnitt kombiniert zwei Datenquellen:
- **Event-getrieben** (`cogs/feedback.py`): `last_submission_at`, `last_error`,
  `counters.submissions` / `counters.submission_errors` — bei jedem Submit.
- **Periodisch** (`bot.py:_feedback_stats_loop`, alle 60s): `guilds[]`
  (Aggregat-Counts), `entries[]` (letzte 20), `bot_avatar_url`. Guild-Namen,
  Avatare und User-Infos werden aus dem Discord-Cache (`bot.guilds`,
  `bot.get_user()`) angereichert.
