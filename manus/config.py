"""Manus extension configuration."""

from pydantic import BaseModel, Field
from nanobot.config.schema import Config, ChannelsConfig

class WebConfig(BaseModel):
    """Web channel configuration."""
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

class ManusChannelsConfig(ChannelsConfig):
    """Channels configuration with web support."""
    web: WebConfig = Field(default_factory=WebConfig)

class ManusConfig(Config):
    """Config with Manus extensions."""
    channels: ManusChannelsConfig = Field(default_factory=ManusChannelsConfig)
