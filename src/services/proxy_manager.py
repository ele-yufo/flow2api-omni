"""Proxy management module"""
from typing import Optional
from ..core.database import Database
from ..core.models import ProxyConfig

class ProxyManager:
    """Proxy configuration manager"""

    def __init__(self, db: Database):
        self.db = db

    async def get_proxy_url(self) -> Optional[str]:
        """Get proxy URL if enabled, otherwise return None"""
        config = await self.db.get_proxy_config()
        if config and config.enabled and config.proxy_url:
            return config.proxy_url
        return None

    async def update_proxy_config(self, enabled: bool, proxy_url: Optional[str]):
        """Update proxy configuration"""
        await self.db.update_proxy_config(enabled, proxy_url)

    async def get_proxy_config(self) -> ProxyConfig:
        """Get proxy configuration"""
        return await self.db.get_proxy_config()
