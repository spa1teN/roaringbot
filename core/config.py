# core/config.py
import os
from typing import List, Optional
import logging

log = logging.getLogger("roaringbot.config")

class BotConfig:
    """Centralized configuration management for the bot"""
    
    def __init__(self):
        self._validate_required_env_vars()
    
    # Discord Configuration
    @property
    def discord_token(self) -> str:
        return os.getenv("DISCORD_TOKEN", "")
    
    @property
    def guild_id(self) -> Optional[int]:
        guild_id = os.getenv("GUILD_ID")
        return int(guild_id) if guild_id else None
    
    @property
    def owner_id(self) -> int:
        return int(os.getenv("BOT_OWNER_ID", "485051896655249419"))
    
    # Logging Configuration
    @property
    def log_webhook_url(self) -> Optional[str]:
        return os.getenv("LOG_WEBHOOK_URL")
    
    @property
    def log_level(self) -> str:
        return os.getenv("LOG_LEVEL", "INFO").upper()

    # Map Configuration
    @property
    def pin_cooldown_minutes(self) -> int:
        return int(os.getenv("MAP_PIN_COOLDOWN_MINUTES", "30"))
    
    # Cache Configuration
    @property
    def max_cache_size_mb(self) -> int:
        return int(os.getenv("MAX_CACHE_SIZE_MB", "100"))
    
    @property
    def max_memory_cache_items(self) -> int:
        return int(os.getenv("MAX_MEMORY_CACHE_ITEMS", "50"))
    
    # HTTP Configuration
    @property
    def http_timeout(self) -> int:
        return int(os.getenv("HTTP_TIMEOUT", "30"))
    
    @property
    def max_connections(self) -> int:
        return int(os.getenv("MAX_HTTP_CONNECTIONS", "100"))
    
    @property
    def max_connections_per_host(self) -> int:
        return int(os.getenv("MAX_HTTP_CONNECTIONS_PER_HOST", "10"))
        
    # E-Sports Configuration
    @property
    def esports_api_url(self) -> str:
        return os.getenv("ESPORTS_API_URL", "https://wannspieltbig.de/api/match_upcoming/")
    
    @property
    def esports_poll_interval_minutes(self) -> int:
        return int(os.getenv("ESPORTS_POLL_INTERVAL_MINUTES", "5"))
    
    @property
    def esports_summary_channel_id(self) -> Optional[int]:
        channel_id = os.getenv("ESPORTS_SUMMARY_CHANNEL_ID")
        return int(channel_id) if channel_id else None
    
    @property
    def esports_enabled(self) -> bool:
        return os.getenv("ESPORTS_ENABLED", "true").lower() in ("true", "1", "yes")
    
    @property
    def esports_vc1_id(self) -> Optional[int]:
        vc_id = os.getenv("ESPORTS_VC1")
        return int(vc_id) if vc_id else None
    
    @property
    def esports_vc2_id(self) -> Optional[int]:
        vc_id = os.getenv("ESPORTS_VC2")
        return int(vc_id) if vc_id else None
    
    @property
    def esports_update_channel_id(self) -> Optional[int]:
        channel_id = os.getenv("ESPORTS_UPDATE_CHANNEL_ID")
        return int(channel_id) if channel_id else None
    
    @property
    def esports_guild_id(self) -> Optional[int]:
        guild_id = os.getenv("ESPORTS_GUILD_ID")
        return int(guild_id) if guild_id else None
    
    @property
    def wsb_username(self) -> Optional[str]:
        return os.getenv("WSB_User")
    
    @property
    def wsb_password(self) -> Optional[str]:
        return os.getenv("WSB_PW")
    
    # Ping Role Configuration
    @property
    def ping_cs_role_id(self) -> Optional[int]:
        role_id = os.getenv("PING_CS")
        return int(role_id) if role_id else None
    
    @property
    def ping_lol_role_id(self) -> Optional[int]:
        role_id = os.getenv("PING_LOL")
        return int(role_id) if role_id else None
    
    @property
    def ping_tm_role_id(self) -> Optional[int]:
        role_id = os.getenv("PING_TM")
        return int(role_id) if role_id else None
    
    def _validate_required_env_vars(self):
        """Validate that required environment variables are set"""
        required_vars = ["DISCORD_TOKEN"]
        missing_vars = []
        
        for var in required_vars:
            if not os.getenv(var):
                missing_vars.append(var)
        
        if missing_vars:
            error_msg = f"Missing required environment variables: {', '.join(missing_vars)}"
            log.error(error_msg)
            raise ValueError(error_msg)
    
    def log_configuration(self):
        """Log current configuration (excluding sensitive data)"""
        log.info("Bot Configuration:")
        log.info(f"  Guild ID: {self.guild_id}")
        log.info(f"  Owner ID: {self.owner_id}")
        log.info(f"  Log Level: {self.log_level}")
        log.info(f"  Pin Cooldown: {self.pin_cooldown_minutes} minutes")
        log.info(f"  Max Cache Size: {self.max_cache_size_mb} MB")
        log.info(f"  HTTP Timeout: {self.http_timeout} seconds")
        log.info(f"  Webhook Logging: {'Enabled' if self.log_webhook_url else 'Disabled'}")
        log.info(f"  E-Sports Monitoring: {'Enabled' if self.esports_enabled else 'Disabled'}")
        if self.esports_enabled:
            log.info(f"    Poll Interval: {self.esports_poll_interval_minutes} minutes")
            log.info(f"    Summary Channel: {self.esports_summary_channel_id or 'Not configured'}")
            log.info(f"    Target Guild: {self.esports_guild_id or 'Auto-detect'}")

# Global configuration instance
config = BotConfig()
