# RoaringBot - Discord Bot

## Overview
RoaringBot is a Discord bot written in Python that provides comprehensive e-sports match monitoring, birthday reminders, and moderation features for Discord servers. Built with enterprise-grade architecture including validation, caching, and configuration management.

## Core Features

### 🎂 Birthday Reminders
- Daily birthday check at 10:00 German time (DST-aware)
- Reads from Google Spreadsheet worksheet "Register" via Service Account
- Uses columns "Discord" (username) and "Geburtsdatum" (dd.mm.yyyy)
- Skips members with "Datum Austritt" set (inactive members) or "-" as Discord name
- Posts birthday embed with bold names and configurable Discord emote to channel

### 🎮 E-Sports Match Monitoring
- Automated polling of wannspieltbig.de API for BIG team matches
- Discord scheduled event creation with voice channel integration
- **Automatic Discord Event Management:**
  - Events automatically start when match begins (status: Scheduled → Active)
  - Events automatically end when match finishes (status: Active → Completed)
  - **Enhanced Auto-Ending Logic**: Events are automatically ended when matches disappear from API response (indicating completion), eliminating dependence on unreliable API `end_time` field
  - Fallback auto-end after 4 hours for matches without proper end times
  - Comprehensive logging and error handling for event status changes
- **Continuous Weekly Summaries:**
  - Single weekly overview message that updates automatically throughout the week
  - Shows current week matches (Monday to Sunday) with live updates as matches are added/cancelled
  - Automatically creates new weekly message when week changes and deletes previous week's message
  - Updates every 5-15 minutes during match monitoring with BIG logo thumbnail
- Match cancellation handling with automatic event deletion
- **30-Minute Match Reminders:**
  - Automated reminder messages posted 30 minutes before each match starts
  - Game-specific role pings (configurable via PING_CS, PING_LOL, PING_TM environment variables)
  - Rich embed format showing teams, tournament, start time, and Discord event links
  - Automatic cleanup of reminder messages after matches end
  - Reminder tracking and persistence across bot restarts
- **Advanced Counter-Strike Live Score Tracking:**
  - Guaranteed automatic score tracking for every CS match (starts up to 20 minutes before match begins)
  - **Enhanced Duplicate Prevention:** Persistent tracking of monitored matches across bot restarts to prevent duplicate score messages
  - Interactive buttons: Team round wins, Manual score input modal
  - Proper CS overtime rules (12-12 → first to 16, 15-15 → first to 19, etc.)
  - **Unified Map Completion Confirmation:** Both increment buttons and manual score input trigger confirmation system when winning scores are reached
  - Real-time score synchronization with wannspieltbig.de API using actual matchmap IDs
  - Multi-map progression through Best of 3/5 matches
  - Visual overtime indicators (OT1, OT2, OT3, etc.)
  - Interactive match selection interface for starting tracking
- Custom game emotes for different esports titles

### 🛡️ Moderation System  
- Member join/leave/ban/kick/timeout logging via Discord webhooks
- Auto-role assignment on member join
- Message clearing with bulk delete (1-100 messages)
- Interactive dashboard with buttons

## Commands

### E-Sports Commands (Admin only)
- `/wannspieltbig_status` - Show e-sports monitoring status and statistics
- `/wannspieltbig_summary` - Manually send weekly match summary
- `/wannspieltbig_refresh` - Manually refresh match data from API
- `/wannspieltbig_start` - Start CS game tracking with interactive match selection

### Moderation Commands
- `/mod_dashboard` - Access moderation configuration dashboard (Admin only)
- `/clear <amount>` - Delete 1-100 messages from current channel (Admin only)

## Environment Configuration

### Required Environment Variables
```bash
DISCORD_TOKEN=your_bot_token_here           # Discord bot token (required)
```

### Optional Environment Variables
```bash
# Bot Configuration
BOT_OWNER_ID=485051896655249419             # Bot owner Discord ID
LOG_WEBHOOK_URL=https://discord.com/api/... # Discord webhook for logging

# Birthday Configuration
BIRTHDAY_CHANNEL_ID=123456789              # Channel for birthday messages
BIRTHDAY_SPREADSHEET_ID=abc123             # Google Spreadsheet ID
GOOGLE_SERVICE_ACCOUNT_FILE=config/google_credentials.json  # Path to service account JSON
BIRTHDAY_EMOTE_ID=123456789               # Discord emote ID for birthday messages

# E-Sports Configuration
ESPORTS_ENABLED=true                        # Enable e-sports monitoring
ESPORTS_API_URL=https://wannspieltbig.de/api/match_upcoming/  # API endpoint
ESPORTS_POLL_INTERVAL_MINUTES=5             # How often to check for match updates
ESPORTS_SUMMARY_CHANNEL_ID=123456789        # Channel for weekly summaries
ESPORTS_UPDATE_CHANNEL=123456789            # Channel for live CS score updates
ESPORTS_GUILD_ID=1383680285186723881        # Specific guild ID for Discord events (hardcoded)
ESPORTS_VC1=123456789                       # Voice channel 1 ID for events
ESPORTS_VC2=123456789                       # Voice channel 2 ID for events
WSB_USER=username                           # Wannspieltbig.de API username
WSB_PW=password                            # Wannspieltbig.de API password

# Match Reminder Configuration
PING_CS=123456789                          # Role ID for Counter-Strike match pings
PING_LOL=123456789                         # Role ID for League of Legends match pings
PING_TM=123456789                          # Role ID for Trackmania match pings

# Cache Configuration
MAX_CACHE_SIZE_MB=100                       # Max disk cache size
MAX_MEMORY_CACHE_ITEMS=50                   # Max memory cache items

# HTTP Configuration
HTTP_TIMEOUT=30                             # HTTP request timeout
MAX_HTTP_CONNECTIONS=100                    # Max HTTP connection pool size
MAX_HTTP_CONNECTIONS_PER_HOST=10            # Max connections per host
```

## Project Structure

```
RoaringBot/
├── bot.py                    # Main bot file with enhanced logging
├── CLAUDE.md                 # This documentation file
├── cogs/
│   ├── birthday.py          # Birthday reminder from Google Spreadsheet
│   ├── esports.py           # E-sports match monitoring and CS score tracking
│   └── moderation.py        # Moderation features
├── core/                    # Core system modules
│   ├── config.py            # Centralized configuration management
│   ├── validation.py        # System validation and health checks
│   ├── cache_manager.py     # Advanced caching system (LRU + file)
│   ├── http_client.py       # HTTP client with connection pooling
│   ├── colors.py            # Color constants and utilities
│   ├── timezone_util.py     # Timezone handling utilities
│   └── mod_views.py        # Moderation UI components
├── data/
│   └── cache/              # Managed file cache directory
├── logs/                   # Application logs with rotation
├── config/                 # Runtime configuration storage
│   ├── esports_data.json   # E-sports match and event tracking data
│   └── google_credentials.json  # Google Service Account key (not in git)
└── test/                   # API testing and screenshots
```

## Technical Architecture

### Core Systems

#### Configuration Management (`core/config.py`)
- Environment variable-based configuration
- Type-safe property accessors
- Validation and logging of configuration state
- Default values for optional settings

#### Validation System (`core/validation.py`)
- Comprehensive startup validation
- Discord token format validation
- System requirements checking (Python version, packages)
- Network connectivity testing

#### Caching System (`core/cache_manager.py`)
- **LRU Memory Cache**: Fast access with automatic eviction
- **Managed File Cache**: Disk-based with size limits and cleanup
- **Automatic Cleanup**: Periodic tasks and size-based management
- **Access Time Tracking**: LRU eviction based on usage patterns

#### HTTP Client Management (`core/http_client.py`)
- **Connection Pooling**: Optimized connection reuse
- **DNS Caching**: 5-minute TTL for improved performance
- **Keep-Alive**: 30-second connection persistence
- **Rate Limiting**: Built-in request throttling
- **Custom User-Agent**: Proper identification for external services
- **Retry Logic**: Automatic retry for timeouts and connection errors with exponential backoff
- **Error Resilience**: Handles network issues gracefully with configurable retry attempts

### Enhanced Bot Features

#### Advanced Logging
- **Webhook Integration**: Real-time Discord logging
- **Timed Log Rotation**: Daily log file rotation with cleanup
- **Structured Logging**: Consistent format across all modules
- **Error Tracking**: Exception details with stack traces
- **Performance Metrics**: Cache statistics and system health

#### Startup Validation
- System requirements verification
- Configuration validation
- Network connectivity testing
- Dependency verification


### E-Sports System Architecture

#### Match Monitoring (`cogs/esports.py`)
- **API Polling**: Regular checks of wannspieltbig.de API (default: 5 minutes, configurable)
- **Event Lifecycle Management**: Automatic creation, updating, and deletion of Discord events
- **Automatic Event Status Control**: 
  - Events automatically transition from "Scheduled" to "Active" when match start time arrives
  - Events automatically transition from "Active" to "Completed" when match end time arrives
  - Fallback mechanism auto-ends events after 4 hours for safety
  - Real-time status checking every 5 minutes (configurable via poll interval)
- **Persistent Storage**: JSON-based storage for match data, Discord event mappings, and monitored match tracking
- **Timezone Handling**: German timezone conversion for scheduling and display
- **Multi-Match Tracking**: Concurrent monitoring of multiple matches

#### CS Score Tracking System
- **Automatic Triggering**: Monitors CS matches and starts tracking 5 minutes before match begins
- **CSGameTracker Class**: Manages round-by-round scoring with proper overtime state tracking
- **Interactive UI Components**:
  - MatchSelectionView: Interactive button interface for selecting matches to track
  - ScoreUpdateView: Team round win buttons + Manual score input button
  - ManualScoreModal: Clean modal interface for direct score input with validation
  - MapConfirmationView: Confirm/cancel map completion when winning score is reached
- **Correct CS Scoring Rules**:
  - Regular time: First to 13 rounds wins
  - Overtime only triggered by exact ties: 12-12 → first to 16, 15-15 → first to 19, 18-18 → first to 22, etc.
  - Proper overtime state persistence through score changes
- **Enhanced Features**:
  - Interactive match selection with team names and start times displayed
  - Manual score input via modal with real-time validation (0-30 rounds)
  - Map completion confirmation system (prevents accidental map endings)
  - Visual overtime indicators (Map X (OT1), Map X (OT2), etc.)
  - Seamless message updates (no ephemeral responses)
  - User-friendly interface without requiring match or map IDs
- **API Integration**: Real-time synchronization with wannspieltbig.de using actual matchmap IDs extracted from match data
- **Multi-Map Support**: Automatic progression through Best of 3/5 matches with confirmation flow
- **Admin Permissions**: All score updates and confirmations restricted to administrators only
- **Network Resilience**: Automatic retry logic handles API timeouts and connection errors gracefully

#### Weekly Summary System
- **Continuous Updates**: Single weekly message that updates automatically throughout the week
- **Current Week Focus**: Shows matches for current week (Monday to Sunday) in German timezone  
- **Smart Week Management**: Automatically creates new weekly message and deletes previous week's message
- **Live Synchronization**: Updates every 5-15 minutes when match data changes (new matches, cancellations)
- **Visual Design**: BIG logo as thumbnail (big.png file attachment) with game-specific emotes
- **Interactive Elements**: Clickable Discord event links and organized display by day
- **Persistent Tracking**: Maintains weekly message across bot restarts and updates

### Birthday System (`cogs/birthday.py`)
- **Google Sheets Integration**: Reads from "Register" worksheet via gspread with Service Account auth
- **Daily Check**: `@tasks.loop(time=09:00 UTC)` with German timezone gate (runs at 10:00 MEZ/MESZ)
- **Spreadsheet Parsing**: Dynamically finds "Discord", "Geburtsdatum", "Datum Austritt" columns by header index
- **Member Filtering**: Skips inactive members (with exit date), placeholder names ("-"), and empty fields
- **Custom Emote**: Configurable Discord emote via `BIRTHDAY_EMOTE_ID` env var
- **Bold Formatting**: Birthday names displayed in bold markdown
- **Error Resilience**: Skips invalid dates, logs warnings, retries next day on failures
- **Conditional Loading**: Cog only loads when `BIRTHDAY_CHANNEL_ID` and `BIRTHDAY_SPREADSHEET_ID` are set

### Caching Strategy
1. **Memory Layer**: LRU cache for frequently accessed data
2. **File Layer**: Persistent storage with size management
3. **Multi-Level**: Automatic promotion/demotion between layers
4. **Cleanup Tasks**: Hourly maintenance and size enforcement
5. **Performance Monitoring**: Cache hit rates and statistics

## Setup and Deployment

### Prerequisites
- Python 3.8+
- Required Python packages (see dependencies)
- Discord bot token
- Google Service Account JSON for birthday feature
- Proper file system permissions for cache directories

### Installation Steps
1. Set required environment variables (minimum: `DISCORD_TOKEN`)
2. Install dependencies: `pip install -r requirements.txt`
3. Place Google Service Account JSON in `config/google_credentials.json` (for birthday feature)
4. Run: `python bot.py`

### Dependencies (requirements.txt)
- `discord.py>=2.3.0` - Discord API wrapper
- `gspread>=6.0.0` - Google Sheets API client
- `google-auth>=2.0.0` - Google authentication
- `aiohttp>=3.8.0` - Async HTTP requests
- `psutil>=5.9.0` - System metrics
- `feedparser>=6.0.10` - RSS/Atom feed parsing
- `requests>=2.31.0` - Synchronous HTTP (validation)
- `PyYAML>=6.0` - Configuration file support
- `pytz>=2023.3` - Timezone handling for German time

### System Requirements
- **Memory**: 256MB+ recommended
- **Disk**: 50MB+ for cache and logs
- **Network**: Stable internet connection for Discord API and Google Sheets

### Bot Permissions Required
- Send Messages
- Use Slash Commands  
- Manage Messages (for clear command)
- Read Message History
- Use External Emojis
- Manage Roles (for auto-join role)
- Manage Events (for creating Discord scheduled events)
- View Audit Log (for moderation logging)

## Development Notes

### Architecture Principles
- **Modular Design**: Clear separation of concerns
- **Configuration-Driven**: Environment-based configuration
- **Performance-First**: Multi-layer caching and connection pooling
- **Reliability**: Comprehensive validation and error handling
- **Observability**: Detailed logging and metrics

### Code Quality
- Type hints throughout for better maintainability
- Async/await patterns for optimal performance
- Comprehensive error handling with graceful degradation
- Resource management with proper cleanup
- Consistent logging and monitoring

### Recent Improvements (2026-02)
- **Added Birthday Reminder System**: Daily Google Spreadsheet check at 10:00 German time, posts birthday embeds with custom emote
- **Removed Map System**: Deleted Germany map cog and all related files (map_gen, map_config, map_storage, map_views, shapefiles), removed geopandas/shapely/Pillow dependencies

### Previous Improvements (2025-09)
- **Fixed CS Score Tracking Duplicates**: Enhanced persistent storage to prevent duplicate score tracking messages across bot restarts
- **Improved Weekly Summary System**: Continuous weekly message updates instead of weekly-only posting, with proper current week filtering  
- **Enhanced Match Monitoring**: Better duplicate prevention and real-time synchronization of weekly overviews
- **Fixed Discord Event Auto-Ending (2025-09-23)**: Implemented automatic event ending when matches disappear from API response, eliminating reliance on unreliable `end_time` field from API
- **Fixed CS Score Confirmation Bug (2025-09-23)**: Manual score input now properly shows map completion confirmation view when winning scores are reached, matching increment button behavior
- **Fixed API Data Validation (2025-10-14)**: Enhanced EsportsMatch constructor with proper null checking to handle incomplete match data from wannspieltbig.de API, eliminating recurring "NoneType object is not subscriptable" errors

### Production Considerations
- Environment variable configuration (never hardcode secrets)
- Log rotation and size management
- Resource monitoring and cleanup
- Graceful shutdown handling
- Network resilience with automatic retries and exponential backoff
- Enhanced error handling for API timeouts and connection issues
- User-friendly interfaces with interactive components instead of command parameters

## Troubleshooting

### Log Analysis
Application logs are stored in the `/logs` directory with automatic rotation:
- `roaringbot.log` - Current day's logs
- `roaringbot.log.YYYY-MM-DD` - Historical logs

### Common Issues
1. **Discord Event Start Failures**: "Channel already has an active event"
   - **Cause**: Multiple events trying to use the same voice channel
   - **Resolution**: Fixed in 2025-09-23 update with improved event lifecycle management

2. **Network Timeouts**: "Timeout on reading data from socket"
   - **Cause**: Temporary network issues with wannspieltbig.de API
   - **Resolution**: Automatic retry logic with exponential backoff

3. **Missing Map IDs**: "No map ID available for match"
   - **Cause**: CS match data incomplete from API
   - **Resolution**: Graceful degradation - tracking continues without API updates

4. **API Data Errors**: "NoneType object is not subscriptable" (Fixed 2025-10-14)
   - **Cause**: wannspieltbig.de API returning match entries with missing tournament/team data
   - **Resolution**: Enhanced EsportsMatch constructor with proper null checking and descriptive error messages

5. **PyNaCl Warning**: "PyNaCl is not installed, voice will NOT be supported"
   - **Status**: Expected behavior - bot doesn't require voice functionality