"""CLI for Manus-enhanced nanobot gateway."""

import asyncio
import typer
from loguru import logger
from rich.console import Console

from nanobot import __logo__
from nanobot.config.loader import load_config, get_data_dir
from nanobot.bus.queue import MessageBus
from nanobot.session.manager import SessionManager
from nanobot.cron.service import CronService
from nanobot.heartbeat.service import HeartbeatService

# Manus extensions
from manus.loop import ManusAgentLoop
from manus.manager import ManusChannelManager
from manus.config import ManusConfig, ManusChannelsConfig, WebConfig

app = typer.Typer(help="nanobot Manus Extension Gateway")
console = Console()

def _make_provider(config):
    """Helper to create provider from config (copied from nanobot.cli.commands)."""
    from nanobot.cli.commands import _make_provider as original_make_provider
    return original_make_provider(config)

@app.command()
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    web_port: int = typer.Option(8000, "--web-port", help="Web API port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start the nanobot gateway with Manus extensions."""
    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{__logo__} Starting nanobot gateway with Manus extensions...")

    # Load base config and wrap it in ManusConfig
    base_config = load_config()
    config_dict = base_config.model_dump()

    # Ensure 'web' config is present
    if "channels" not in config_dict:
        config_dict["channels"] = {}
    if "web" not in config_dict["channels"]:
        config_dict["channels"]["web"] = {"enabled": True, "port": web_port}

    config = ManusConfig.model_validate(config_dict)

    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    # Create services
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Create Manus-enhanced agent
    agent = ManusAgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
    )

    # Set cron callback
    async def on_cron_job(job) -> str | None:
        return await agent.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
        )
    cron.on_job = on_cron_job

    # Create heartbeat service
    async def on_heartbeat(prompt: str) -> str:
        return await agent.process_direct(prompt, session_key="heartbeat")

    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        on_heartbeat=on_heartbeat,
        interval_s=30 * 60,
        enabled=True
    )

    # Create Manus-enhanced channel manager
    channels = ManusChannelManager(config, bus, session_manager=session_manager)

    console.print(f"[green]✓[/green] Web API enabled on http://{config.channels.web.host}:{config.channels.web.port}")
    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Other channels: {', '.join(channels.enabled_channels)}")

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        finally:
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())

if __name__ == "__main__":
    app()
