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
- Jedes Modul (Cog, `core/http_client.py`, `core/cache_manager.py`, `bot.py`)
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
      "errors": {"15m": 0, "1h": 0, "24h": 0}
    }
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
    "last_birthday_names": ["Max"]           // nur gesetzt bei last_check_result == "sent"
  },

  "esports": {
    "updated_at": "2026-07-08T22:15:21Z",
    "monitoring_enabled": true,
    "poll_interval_minutes": 1,
    "last_poll_at": "2026-07-08T22:15:18Z",       // match_monitor-Loop
    "last_poll_success": true,
    "last_poll_error": null,
    "last_livescore_poll_at": "2026-07-08T22:15:20Z",  // live_score_updater-Loop (nur bei aktiven CS-Matches)
    "total_matches": 46,
    "active_matches": 46,                    // nicht abgesagt
    "active_discord_events": 7,
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
    "counters": {
      "api_errors": {"15m": 0, "1h": 0, "24h": 3},
      "reminder_errors": {"15m": 0, "1h": 0, "24h": 0}
    }
  },

  "moderation": {
    "updated_at": "2026-07-08T22:15:18Z",
    "honeypot_loop_alive": true,             // Heartbeat des Auto-Ban-Loops
    "last_clear_count": 12,                  // letztes /clear-Kommando
    "last_clear_channel_id": 123456789,
    "last_clear_by": "admin#0001",
    "counters": {
      "joins": {"15m": 0, "1h": 1, "24h": 5},
      "leaves": {"15m": 0, "1h": 0, "24h": 2},
      "bans": {"15m": 0, "1h": 0, "24h": 0},
      "kicks": {"15m": 0, "1h": 0, "24h": 0},
      "timeouts": {"15m": 0, "1h": 0, "24h": 1},
      "honeypot_bans": {"15m": 0, "1h": 0, "24h": 0}
    }
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
