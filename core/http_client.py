# core/http_client.py
import aiohttp
import asyncio
import logging
from typing import Optional, Dict, Any, Union
from core.config import config

log = logging.getLogger("roaringbot.http")

class HTTPClientManager:
    """Manages HTTP client sessions with connection pooling"""
    
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self.is_closed = False
    
    async def get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session with connection pooling"""
        if self._session is None or self._session.closed:
            async with self._lock:
                if self._session is None or self._session.closed:
                    await self._create_session()
        
        return self._session
    
    async def _create_session(self):
        """Create new HTTP session with optimized settings"""
        # Connection pooling configuration
        connector = aiohttp.TCPConnector(
            limit=config.max_connections,  # Total connection pool size
            limit_per_host=config.max_connections_per_host,  # Per-host limit
            ttl_dns_cache=300,  # DNS cache TTL (5 minutes)
            use_dns_cache=True,  # Enable DNS caching
            keepalive_timeout=30,  # Keep connections alive for 30 seconds
            enable_cleanup_closed=True,  # Clean up closed connections
            force_close=False,  # Reuse connections when possible
        )
        
        # Session timeout configuration
        timeout = aiohttp.ClientTimeout(
            total=config.http_timeout,  # Total timeout for request
            connect=config.http_timeout // 3,  # Connection timeout
            sock_read=config.http_timeout // 2,  # Socket read timeout
        )
        
        # Headers for better compatibility
        headers = {
            'User-Agent': 'RoaringBot/1.0 (Discord Bot; RSS Reader)',
            'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml, */*',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
        
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=headers,
            raise_for_status=False,  # Don't raise for HTTP errors, handle them manually
            skip_auto_headers=['User-Agent'],  # Use our custom User-Agent
        )
        
        self.is_closed = False
        log.info(f"Created HTTP session with {config.max_connections} max connections "
                f"({config.max_connections_per_host} per host)")
    
    async def close(self):
        """Close HTTP session and cleanup connections"""
        if self._session and not self._session.closed:
            async with self._lock:
                if self._session and not self._session.closed:
                    await self._session.close()
                    self.is_closed = True
                    log.info("HTTP session closed")
    
    async def get(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        """Make GET request using managed session"""
        return await self.request_with_retry('GET', url, **kwargs)
    
    async def post(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        """Make POST request using managed session"""
        return await self.request_with_retry('POST', url, **kwargs)
    
    async def put(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        """Make PUT request using managed session"""
        return await self.request_with_retry('PUT', url, **kwargs)
    
    async def request(self, method: str, url: str, **kwargs) -> aiohttp.ClientResponse:
        """Make request using managed session"""
        return await self.request_with_retry(method, url, **kwargs)
    
    async def request_with_retry(self, method: str, url: str, max_retries: int = 3, 
                                retry_delay: float = 1.0, **kwargs) -> aiohttp.ClientResponse:
        """Make HTTP request with retry logic for timeouts and connection errors"""
        last_exception = None
        
        for attempt in range(max_retries + 1):
            try:
                session = await self.get_session()
                response = await session.request(method, url, **kwargs)
                
                # If we get here, the request succeeded
                if attempt > 0:
                    log.info(f"Request to {url} succeeded on attempt {attempt + 1}")
                return response
                
            except (asyncio.TimeoutError, aiohttp.ClientTimeout, 
                   aiohttp.ServerTimeoutError) as e:
                last_exception = e
                if attempt < max_retries:
                    delay = retry_delay * (2 ** attempt)  # Exponential backoff
                    log.warning(f"Request to {url} timed out (attempt {attempt + 1}/{max_retries + 1}), "
                              f"retrying in {delay:.1f}s: {e}")
                    await asyncio.sleep(delay)
                    continue
                else:
                    log.error(f"Request to {url} failed after {max_retries + 1} attempts: {e}")
                    raise
                    
            except (aiohttp.ClientConnectionError, aiohttp.ClientConnectorError, 
                   aiohttp.ClientOSError) as e:
                last_exception = e
                if attempt < max_retries:
                    delay = retry_delay * (2 ** attempt)  # Exponential backoff
                    log.warning(f"Connection error for {url} (attempt {attempt + 1}/{max_retries + 1}), "
                              f"retrying in {delay:.1f}s: {e}")
                    await asyncio.sleep(delay)
                    continue
                else:
                    log.error(f"Connection to {url} failed after {max_retries + 1} attempts: {e}")
                    raise
                    
            except Exception as e:
                # For other exceptions, don't retry
                log.error(f"Non-retryable error for {url}: {e}")
                raise
        
        # This shouldn't be reached, but just in case
        if last_exception:
            raise last_exception
    
    def get_session_info(self) -> Dict[str, Any]:
        """Get information about current session"""
        if self._session is None or self._session.closed:
            return {"status": "closed", "connections": 0}
        
        connector = self._session.connector
        if hasattr(connector, '_conns'):
            # Get connection statistics
            connection_count = sum(len(conns) for conns in connector._conns.values())
            return {
                "status": "open",
                "connections": connection_count,
                "max_connections": config.max_connections,
                "max_per_host": config.max_connections_per_host,
            }
        
        return {"status": "open", "connections": "unknown"}

# Global HTTP client manager
http_client = HTTPClientManager()

# Convenience functions for common operations
async def get(url: str, **kwargs) -> aiohttp.ClientResponse:
    """Make GET request using global HTTP client"""
    return await http_client.get(url, **kwargs)

async def post(url: str, **kwargs) -> aiohttp.ClientResponse:
    """Make POST request using global HTTP client"""
    return await http_client.post(url, **kwargs)

async def put(url: str, **kwargs) -> aiohttp.ClientResponse:
    """Make PUT request using global HTTP client"""
    return await http_client.put(url, **kwargs)

async def request(method: str, url: str, **kwargs) -> aiohttp.ClientResponse:
    """Make request using global HTTP client"""
    return await http_client.request(method, url, **kwargs)

async def close_http_client():
    """Close global HTTP client"""
    await http_client.close()

def get_http_session_info() -> Dict[str, Any]:
    """Get HTTP session information"""
    return http_client.get_session_info()