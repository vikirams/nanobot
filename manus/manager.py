"""Manus-enhanced channel manager."""

from nanobot.channels.manager import ChannelManager
from manus.channel import ManusWebChannel
from loguru import logger

class ManusChannelManager(ChannelManager):
    """
    Subclass of ChannelManager that adds support for the Manus Web Channel.
    """

    def _init_channels(self) -> None:
        """Initialize original channels and add Manus web channel."""
        # We call the original _init_channels first
        super()._init_channels()

        # Then we add our custom web channel
        # We assume config has been converted to ManusConfig or similar
        web_config = getattr(self.config.channels, "web", None)
        if web_config and getattr(web_config, "enabled", False):
            try:
                self.channels["web"] = ManusWebChannel(
                    web_config,
                    self.bus,
                    session_manager=self.session_manager
                )
                logger.info("Manus Web channel enabled")
            except Exception as e:
                logger.warning(f"Manus Web channel failed to initialize: {e}")
