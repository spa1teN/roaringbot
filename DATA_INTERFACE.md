# RoaringBot Data Interface

RoaringBot schreibt alle 15 Sekunden einen vollständigen Status-Snapshot nach
**`data/status.json`** (relativ zum Bot-Arbeitsverzeichnis, also
`/root/roaringbot/data/status.json` auf dem Host — der Ordner ist bereits als
Volume in `docker-compose.yml` gemountet: `./data:/app/data`).

Das ist die einzige Schnittstelle, die externe Tools (z. B. das Dashboard)
nutzen sollten, um den Live-Zustand des Bots abzufragen. Kein Log-Parsing
nötig.

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
    "next_matches": [                        // die 3 nächsten Matches (live oder anstehend), mit Health-Check
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
        "live_score": null,                       // bei laufendem CS-Match: {"map":1,"score":"7-5","maps":"0-0"}
        // issues: leer wenn alles ok, sonst eine Teilmenge von:
        // "no_discord_event" | "reminder_missing" | "event_not_started" | "tracking_missing" (nur CS)
        // jedes erkannte Issue wird zusätzlich als log.error geloggt (taucht im Fehler-Log-Graphen auf)
        "issues": []
      }
    ],
    "counters": {
      "api_errors": {"15m": 0, "1h": 0, "24h": 3},
      "reminder_errors": {"15m": 0, "1h": 0, "24h": 0}
    }
  },

  "moderation": {
    "updated_at": "2026-07-08T22:15:18Z",
    "honeypot_loop_alive": true,             // Heartbeat des Auto-Ban-Loops
    "honeypot_status": "ok",                 // "ok" | "disabled" | "error", zuletzt beobachteter Zustand
    "honeypot_last_error": null,
    "member_log_status": "ok",               // "ok" | "disabled" | "error", zuletzt beobachteter Webhook-Versand
    "member_log_last_error": null,
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
      "honeypot_bans": {"15m": 0, "1h": 0, "24h": 0}
    },
    "events": [                              // rollierendes Log (max_len=300): join/leave/kick/ban/timeout/unban
      {"at": "2026-07-08T22:10:03Z", "type": "ban", "user": "spammer#0001", "user_id": 123, "moderator": "admin#0001", "reason": "Spam", "guild": "Die Grünen"}
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
}
```

### Wichtige abgeleitete Kennzahlen für ein Dashboard

| Anzeige | Berechnung |
|---|---|
| Bot online/offline | `bot.gateway_status == "connected"` **und** `generated_at` nicht älter als ~2 Min |
| E-Sports-Polling hängt | `esports.last_poll_at` älter als `2 × poll_interval_minutes` |
| API-Ausfall (wie am 03.–05.07.) | `http.counters.timeouts["15m"]` oder `esports.counters.api_errors["15m"]` > 0 über mehrere Snapshots hinweg |
| Geburtstags-Feature kaputt | `birthday.sheets_connected == false` oder `birthday.last_check_result == "error"` |
| CS-Tracking aktiv | `esports.active_cs_trackers > 0`, Details in `esports.cs_trackers[]` |
| Reconnect-Häufung | `bot.counters.reconnects["1h"]` > 2–3 |
| Kassenbuch-Feature kaputt | `finance.sheets_connected == false` oder `finance.last_error` gesetzt |
| Monatsreport verpasst | `finance.last_report_month` entspricht nicht dem Vormonat, obwohl der 1. bereits vergangen ist |

## Konsum durch das Dashboard

- Volume-Mount: `dashboard/docker-compose.yml` → `/root/roaringbot/data:/data/roaringbot:ro`
- Reader: `dashboard/app/roaringbot_status.py` (`get_status()`), prüft
  Existenz, Parsing-Fehler und Alter der Datei (`stale`-Flag, Schwelle 120s).
- Endpoint: `GET /api/roaringbot/status` in `dashboard/app/main.py`.
- Antwort bei fehlender/kaputter Datei: `{"available": false, "reason": "..."}`
  statt eines Fehlers — das Frontend kann so unterscheiden zwischen
  "Bot nie gestartet"/"Volume nicht gemountet" und "Bot läuft, aber Feature X
  noch nie ausgelöst".

## Erweitern

Neue Signale hinzufügen: im jeweiligen Cog/Modul
`from core.status_reporter import status_reporter` importieren und an der
relevanten Stelle `status_reporter.record(...)` bzw. `.bump_counter(...)`
aufrufen — keine Änderung an `status_reporter.py` selbst nötig. Neue
Sektionen erscheinen automatisch im nächsten Snapshot.
