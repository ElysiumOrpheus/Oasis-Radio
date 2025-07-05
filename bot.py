import discord
from discord.ext import commands
from discord import ui
import os
import asyncio
import threading
import time
from typing import Any, Optional
from dotenv import load_dotenv
import yaml
import logging
import logging.config
import sys

from src.radio_pipeline import Pipeline, DiscordBotNotifier, load_config

# --- Logging Setup ---
logger = logging.getLogger(__name__)

# --- Bot Setup ---
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID_STR = os.getenv("DISCORD_COMMAND_CHANNEL_ID")
CHANNEL_ID = int(CHANNEL_ID_STR) if CHANNEL_ID_STR else None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# --- Configuration Helpers ---
CONFIG_PATH = "config.yml"
CONFIG_LOCK = asyncio.Lock()

async def load_bot_config():
    """Async helper to load config for the bot's use."""
    async with CONFIG_LOCK:
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                #new log
                logger.debug("Bot config loaded from disk.")
                return yaml.safe_load(f)
        except (FileNotFoundError, yaml.YAMLError) as e:
            logger.error(f"Bot config loader error: {e}")
            return None

async def save_bot_config(data):
    """Async helper to save config from the bot."""
    async with CONFIG_LOCK:
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False, indent=2)
                #new log
                logger.info(f"Bot config saved to disk.")
            return True
        except IOError as e:
            logger.error(f"Bot config save error: {e}")
            return False

# ==============================================================================
# SECTION: INTERACTIVE CONFIGURATION VIEW
# ==============================================================================

class StationConfigView(ui.View):
    """
    An interactive view that displays buttons for each station to toggle their status.
    """
    def __init__(self, stations: list, cog_instance: commands.Cog):
        super().__init__(timeout=300)
        self.stations = stations
        self.cog = cog_instance
        self.create_buttons()

    def create_buttons(self):
        """Dynamically creates a button for each station."""
        for station in self.stations:
            is_enabled = station.get('enabled', True)

            button = ui.Button(
                label=station['name'],
                custom_id=f"toggle_station_{station['name']}",
                style=discord.ButtonStyle.green if is_enabled else discord.ButtonStyle.red,
            )
            button.callback = self.button_callback
            self.add_item(button)

    async def button_callback(self, interaction: discord.Interaction):
        """The single callback handler for all dynamically created buttons."""
        station_name_to_toggle = interaction.data["custom_id"].replace("toggle_station_", "")
        #new log
        logger.info(f"User '{interaction.user}' clicked config button for station '{station_name_to_toggle}'.")
        if self.cog.pipeline_instance and self.cog.pipeline_instance.is_running():
            #new log
            logger.warning(f"User '{interaction.user}' tried to change config while pipeline is running.")
            await interaction.response.send_message(
                "⚠️ **Action failed.** Please stop the pipeline with `!stop` before changing the configuration.",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        station_name_to_toggle = interaction.data["custom_id"].replace("toggle_station_", "")

        config_data = await load_bot_config()
        if not config_data:
            await interaction.followup.send("❌ Error: Could not load `config.yml`.", ephemeral=True)
            return

        stations = config_data.get("radio_stations", [])
        station_found = False
        for station in stations:
            if station.get("name") == station_name_to_toggle:
                current_status = station.get('enabled', True)
                station['enabled'] = not current_status
                station_found = True
                #new log
                logger.info(f"Toggled station '{station_name_to_toggle}' enabled status to {not current_status}.")
                break

        if not station_found:
            await interaction.followup.send(f"❌ Error: Station `{station_name_to_toggle}` not found.", ephemeral=True)
            return

        if not await save_bot_config(config_data):
            await interaction.followup.send("❌ Error: Failed to write changes to `config.yml`.", ephemeral=True)
            return
        
        # Use the cog's helper method to create the embed
        new_embed = self.cog._create_station_embed(stations)
        new_view = StationConfigView(stations, self.cog)
        await interaction.edit_original_response(embed=new_embed, view=new_view)
        #new log
        logger.debug(f"Config view updated successfully after toggle.")

# --- UPDATED Custom Help Command ---
class CustomHelpCommand(commands.HelpCommand):
    """A highly customized help command class."""

    def get_command_signature(self, command):
        """Returns a clean command signature."""
        return f'`{self.context.prefix}{command.qualified_name} {command.signature}`'

    async def send_bot_help(self, mapping):
        """Handles the main `!help` command."""
        #new log
        logger.debug(f"Help command invoked by '{self.context.author}'.")
        embed = discord.Embed(
            title="Bot Command Help",
            description="Here are the available commands:",
            color=discord.Color.blurple()
        )
        # This loop is more robust for Cogs
        for cog, commands_list in mapping.items():
            # Filter out commands the user can't run, if any
            if filtered_commands := await self.filter_commands(commands_list, sort=True):
                for command in filtered_commands:
                    embed.add_field(name=self.get_command_signature(command), value=command.help, inline=False)

        # Add a footer to guide users
        embed.set_footer(text=f"Use `{self.context.prefix}help [command]` for more info on a specific command.")
        
        # <<< THIS WAS THE MISSING LINE
        await self.get_destination().send(embed=embed)


    async def send_command_help(self, command):
        """Handles `!help <command>`."""
        #new log
        logger.debug(f"Help command invoked for command '{command.name}' by '{self.context.author}'.")
        embed = discord.Embed(title=f"Help for `!{command.name}`", color=discord.Color.dark_teal())
        embed.add_field(name="Usage", value=self.get_command_signature(command), inline=False)
        embed.add_field(name="Description", value=command.help or "No description provided.", inline=False)
        if command.aliases:
            aliases = ", ".join([f"`{alias}`" for alias in command.aliases])
            embed.add_field(name="Aliases", value=aliases, inline=False)

        await self.get_destination().send(embed=embed)

    async def send_error_message(self, error):
        """Handles errors, like a command not being found."""
        #new log
        logger.warning(f"Help command error: {error}")
        await self.get_destination().send(f"❌ {error}")

# --- pipeline cog ---
class PipelineCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pipeline_instance: Optional[Pipeline] = None
        self.pipeline_thread: Optional[threading.Thread] = None
        self.whisper_model: Optional[Any] = None
        self.model_ready_event = asyncio.Event()

    # --- Cog Listener ---
    @commands.Cog.listener()
    async def on_ready(self):
        """Event listener for when the bot comes online."""
        logger.info(f'Bot logged in as {self.bot.user}')
        if CHANNEL_ID:
            channel = self.bot.get_channel(CHANNEL_ID)
            if channel:
                await channel.send("📻 Radio Pipeline Bot is online. Use `!help` for commands.")
                # Start pre-loading the model in the background
                asyncio.create_task(self._preload_whisper_model())
            else:
                logger.critical(f"Could not find channel with ID {CHANNEL_ID}.")
        else:
            logger.critical("DISCORD_COMMAND_CHANNEL_ID is not set in the .env file.")

    # --- Internal Helper Methods ---
    async def _preload_whisper_model(self):
        """Loads the Whisper model in a background thread."""
        logger.info("--- Initiating Whisper model pre-loading... ---")
        try:
            config = await load_bot_config()
            if not config:
                logger.critical("Cannot pre-load model, config file not found or invalid.")
                return

            model_name = config.get('whisper_model')
            if not model_name:
                logger.critical("'whisper_model' not specified in config.yml for pre-loading.")
                return

            #new log
            logger.info(f"Pre-loading model '{model_name}' in a background thread.")
            import whisper
            self.whisper_model = await asyncio.to_thread(whisper.load_model, model_name)

            logger.info(f"Whisper model '{model_name}' pre-loaded successfully.")
            self.model_ready_event.set()
            #new log
            logger.info("Model ready event has been set.")
        except Exception as e:
            logger.critical(f"Failed to pre-load Whisper model: {e}", exc_info=True)

    def _format_seconds(self, seconds: float) -> str:
        """Converts seconds into a human-readable HH:MM:SS string."""
        seconds = int(seconds)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02}"
    
    def _create_station_embed(self, stations: list) -> discord.Embed:
        """Creates the embed showing station statuses."""
        embed = discord.Embed(title="📻 Station Configuration", color=discord.Color.orange())
        description = []
        if not stations:
            description.append("No stations are configured in `config.yml`.")
        else:
            for station in stations:
                status_emoji = "🟢" if station.get("enabled", True) else "🔴"
                description.append(f"{status_emoji} **{station.get('name', 'Unnamed Station')}**")
        embed.description = "\n".join(description)
        embed.set_footer(text="Click a station to enable/disable it. Changes apply on next !start.")
        return embed

    # --- Bot Commands ---
    @commands.command(name="config", help="Shows an interactive menu to enable/disable stations.\nMust be used when the pipeline is stopped.")
    async def config(self, ctx: commands.Context):
        #new log
        logger.info(f"Command '!config' invoked by '{ctx.author}'.")
        if self.pipeline_instance and self.pipeline_instance.is_running():
            await ctx.send("⚠️ Please stop the pipeline with `!stop` before changing the configuration.")
            return

        config_data = await load_bot_config()
        stations = config_data.get("radio_stations", []) if config_data else []
        embed = self._create_station_embed(stations)
        view = StationConfigView(stations, self)
        await ctx.send(embed=embed, view=view)

    @commands.command(name="start", help="Starts the radio monitoring pipeline. Usage: !start [duration_in_minutes]")
    async def start(self, ctx: commands.Context, duration_minutes: int = 0):
        #new log
        logger.info(f"Command '!start' invoked by '{ctx.author}' with duration {duration_minutes} mins.")
        if self.pipeline_instance:
            #new log
            logger.warning(f"User '{ctx.author}' tried to start an already running pipeline.")
            await ctx.send("⚠️ The pipeline is already running.")
            return

        if not self.model_ready_event.is_set():
            #new log
            logger.info("Whisper model not ready, waiting for event...")
            await ctx.message.add_reaction("⏳")
            try:
                await asyncio.wait_for(self.model_ready_event.wait(), timeout=180.0)
                #new log
                logger.info("Model ready event received.")
                await ctx.message.remove_reaction("⏳", self.bot.user)
            except asyncio.TimeoutError:
                await ctx.message.remove_reaction("⏳", self.bot.user)
                await ctx.send("❌ **Start Failed:** The AI model took too long to load or failed. Check logs and restart the bot.")
                logger.error("Timeout waiting for Whisper model to load.")
                return

        if not self.whisper_model:
            await ctx.send("❌ **Start Failed:** The AI model could not be loaded. Check console logs for details.")
            return

        config_data = await load_bot_config()
        enabled_stations = [s for s in (config_data or {}).get("radio_stations", []) if s.get("enabled", True)]
        duration_text = f"for {duration_minutes} minutes" if duration_minutes > 0 else "indefinitely"
        station_info = "⚠️ No stations are enabled."
        if enabled_stations:
            station_list_str = "\n".join([f"• `{s['name']}`" for s in enabled_stations])
            station_info = f"Monitoring **{len(enabled_stations)}** station(s):\n{station_list_str}"

        await ctx.send(f"🚀 **Starting Pipeline {duration_text}...**\n\n{station_info}\n\n*Please wait...*")

        #new log
        logger.info("Creating and starting pipeline thread.")
        ready_event = threading.Event()
        notifier = DiscordBotNotifier(self.bot, CHANNEL_ID)
        self.pipeline_instance = Pipeline(config_path="config.yml", notifier=notifier, model=self.whisper_model)
        self.pipeline_thread = threading.Thread(target=self.pipeline_instance.start, args=(duration_minutes, ready_event), daemon=True)
        self.pipeline_thread.start()

        try:
            #new log
            logger.info("Waiting for pipeline ready_event from thread.")
            await asyncio.to_thread(ready_event.wait, timeout=60.0)
            #new log
            logger.info("Pipeline ready_event received.")
        except asyncio.TimeoutError:
            #new log
            logger.error("Timeout waiting for pipeline to signal it is ready.")
            #new log
            await ctx.send("❌ **Pipeline failed to confirm startup in time.** Check logs for errors.")
            return
            
        logger.info("Startup check: Pausing briefly to allow subprocesses to spin up...")
        await asyncio.sleep(3) 

        if self.pipeline_instance and self.pipeline_instance.is_running():
            active_stations = self.pipeline_instance.get_active_stations()
            if not active_stations:
                #new log
                logger.warning("Pipeline started, but no active station streams were detected.")
                await ctx.send("✅ **Pipeline started, but failed to connect to any enabled streams.** Check logs.")
            else:
                # ... (success message)
                station_names = ", ".join([f"`{s.name}`" for s in active_stations])
                #new log
                logger.info(f"Pipeline started successfully, monitoring: {station_names}")
                await ctx.send(f"✅ **Pipeline Started!** Monitoring {len(active_stations)} station(s) {duration_text}:\n{station_names}")
        else:
            #new log
            logger.critical("Pipeline thread finished, but the pipeline is not in a running state. Startup failed.")
            await ctx.send(f"❌ **Pipeline failed to start.** Check logs for critical errors.")

    @commands.command(name="stop", help="Stops the pipeline gracefully.")
    async def stop(self, ctx: commands.Context):
        #new log
        logger.info(f"Command '!stop' invoked by '{ctx.author}'.")
        if not self.pipeline_instance or not self.pipeline_instance.is_running():
            #new log
            logger.warning(f"User '{ctx.author}' tried to stop a non-running pipeline.")
            await ctx.send("⚠️ The pipeline is not currently running.")
            return

        await ctx.send("🛑 **Stopping station scanning...** Waiting for processes to finish.")
        #new log
        logger.info("Calling pipeline stop method...")
        await asyncio.to_thread(self.pipeline_instance.stop)
        if self.pipeline_thread:
            #new log
            logger.info("Waiting for pipeline thread to join...")
            await asyncio.to_thread(self.pipeline_thread.join)
            #new log
            logger.info("Pipeline thread has joined.")

        self.pipeline_instance, self.pipeline_thread = None, None
        await ctx.send("✅ **Pipeline stopped successfully.**")
        #new log
        logger.info("Pipeline has been fully stopped and instance cleared.")

    @commands.command(name="shutdown", help="Stops the pipeline and shuts down the bot.")
    async def shutdown(self, ctx: commands.Context):
        #new log
        logger.info(f"Command '!shutdown' invoked by '{ctx.author}'. Bot is shutting down.")
        await ctx.send("🛑 **Initiating full bot shutdown...**")
        if self.pipeline_instance and self.pipeline_instance.is_running():
            await ctx.send("↳ Stopping active pipeline first...")
            stop_ctx = await self.bot.get_context(ctx.message)
            await stop_ctx.invoke(self.bot.get_command('stop'))
        await ctx.send("✅ Bot is shutting down. Goodbye!")
        logger.info("Bot is shutting down via !shutdown command.")
        await self.bot.close()

    @commands.command(name="status", help="Shows the real-time status of the pipeline.")
    async def status(self, ctx: commands.Context):
        #new log
        logger.info(f"Command '!status' invoked by '{ctx.author}'.")
        if not self.pipeline_instance or not self.pipeline_instance.is_running():
            await ctx.send("ℹ️ The pipeline is currently stopped.")
            return

        embed = discord.Embed(title="📻 Pipeline Status: Running", color=discord.Color.green())
        uptime_seconds = time.time() - self.pipeline_instance.start_time
        embed.add_field(name="📈 Uptime", value=self._format_seconds(uptime_seconds), inline=True)
        chunk_duration = self.pipeline_instance.config['recording_chunk_duration_seconds']
        time_to_next_clip = chunk_duration - (uptime_seconds % chunk_duration)
        embed.add_field(name="⏱️ Next Clip In", value=f"{int(time_to_next_clip)} seconds", inline=True)

        all_stations_config = self.pipeline_instance.config.get('radio_stations', [])
        live_station_map = {s.name: s for s in self.pipeline_instance.stations}
        status_lines = []

        if not all_stations_config:
            status_lines.append("No stations are configured.")
        else:
            for station_config in all_stations_config:
                name = station_config.get('name', 'Unnamed Station')
                if not station_config.get('enabled', True):
                    status_lines.append(f"⚪ **{name}** (Disabled in config)")
                    continue
                live_station = live_station_map.get(name)
                if not live_station:
                    status_lines.append(f"❓ **{name}** (Inactive/Not Started)")
                elif live_station.has_failed_permanently:
                    status_lines.append(f"🔴 **{name}** (Failed - Check Logs)")
                elif live_station.has_completed_successfully:
                    status_lines.append(f"✅ **{name}** (Completed)")
                elif live_station.is_process_active:
                    status_lines.append(f"🟢 **{name}** (Recording)")
                else:
                    status_lines.append(f"🟡 **{name}** (Connecting/Retrying...)")

        embed.add_field(name=f"📡 Station States ({len(all_stations_config)} Configured)",
                        value="\n".join(status_lines) or "No stations found.",
                        inline=False)
        await ctx.send(embed=embed)
        #new log
        logger.debug("Status command successfully executed and sent.")

# --- Main Execution ---
async def main():
    """The main async function to setup and run the bot."""
    #new log
    print("Application starting...")
    try:
        config_data = load_config("config.yml")
        logging_config = config_data.get('logging')
        if logging_config:
            logging.config.dictConfig(logging_config)
            #new log
            print("Logging configured from 'config.yml'.")
        else:
            logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
            logger.warning("Logging configuration not found in config.yml. Using basic fallback.")
    except Exception as e:
        print(f"CRITICAL: Failed to load configuration or set up logging: {e}", file=sys.stderr)
        sys.exit(1)

    if not TOKEN or not CHANNEL_ID:
        logger.critical("DISCORD_BOT_TOKEN and/or DISCORD_COMMAND_CHANNEL_ID not found in .env file.")
        return

    bot.help_command = CustomHelpCommand()
    await bot.add_cog(PipelineCog(bot))
    #new log
    logger.info("Bot cogs and help command have been initialized.")

    logger.info("Configuration and logging initialized. Starting bot...")
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())