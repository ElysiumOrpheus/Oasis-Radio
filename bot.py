import discord
from discord.ext import commands
from discord import ui
import os
import asyncio
import threading
import time
from typing import Any
from dotenv import load_dotenv
import yaml

from src.radio_pipeline import Pipeline, DiscordBotNotifier

# --- Bot Setup ---
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID_STR = os.getenv("DISCORD_COMMAND_CHANNEL_ID")
CHANNEL_ID = int(CHANNEL_ID_STR) if CHANNEL_ID_STR else None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# --- State Management ---
pipeline_instance: Pipeline | None = None
pipeline_thread: threading.Thread | None = None
whisper_model: Any | None = None
model_ready_event = asyncio.Event()


# --- Configuration Helpers ---
CONFIG_PATH = "config.yml"
CONFIG_LOCK = asyncio.Lock()

async def load_bot_config():
    """Async helper to load config for the bot's use."""
    async with CONFIG_LOCK:
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except (FileNotFoundError, yaml.YAMLError) as e:
            print(f"❌ Bot config loader error: {e}")
            return None

async def save_bot_config(data):
    """Async helper to save config from the bot."""
    async with CONFIG_LOCK:
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False, indent=2)
            return True
        except IOError as e:
            print(f"❌ Bot config save error: {e}")
            return False

# --- Helper Function ---
def format_seconds(seconds: float) -> str:
    """Converts seconds into a human-readable HH:MM:SS string."""
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"

async def preload_whisper_model():
    """Loads the Whisper model in a background thread and sets an event when done."""
    global whisper_model
    print("--- Initiating Whisper model pre-loading... ---")
    try:
        config = await load_bot_config()
        if not config:
            print("❌ CRITICAL: Cannot pre-load model, config file not found or invalid.")
            return

        model_name = config.get('whisper_model')
        if not model_name:
             print("❌ CRITICAL: 'whisper_model' not specified in config.yml for pre-loading.")
             return

        import whisper
        whisper_model = await asyncio.to_thread(whisper.load_model, model_name)

        print(f"✅ Whisper model '{model_name}' pre-loaded successfully.")
        model_ready_event.set()
    except Exception as e:
        print(f"❌ CRITICAL: Failed to pre-load Whisper model: {e}")


# ==============================================================================
# SECTION: INTERACTIVE CONFIGURATION VIEW
# ==============================================================================

def create_station_embed(stations: list) -> discord.Embed:
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

class StationConfigView(ui.View):
    """
    An interactive view that displays buttons for each station to toggle their status.
    """
    def __init__(self, stations: list):
        super().__init__(timeout=300)
        self.stations = stations
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
        global pipeline_instance

        if pipeline_instance and pipeline_instance.is_running():
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
                break

        if not station_found:
            await interaction.followup.send(f"❌ Error: Station `{station_name_to_toggle}` not found.", ephemeral=True)
            return

        if not await save_bot_config(config_data):
            await interaction.followup.send("❌ Error: Failed to write changes to `config.yml`.", ephemeral=True)
            return

        new_embed = create_station_embed(stations)
        new_view = StationConfigView(stations)
        await interaction.edit_original_response(embed=new_embed, view=new_view)


# --- UPDATED Custom Help Command ---
class CustomHelpCommand(commands.HelpCommand):
    """A highly customized help command class."""

    def get_command_signature(self, command):
        """Returns a clean command signature."""
        return f'`{self.context.prefix}{command.qualified_name} {command.signature}`'

    async def send_bot_help(self, mapping):
        """Handles the main `!help` command."""
        embed = discord.Embed(
            title="Bot Command Help",
            description="Here are the available commands:",
            color=discord.Color.blurple()
        )
        for command in self.context.bot.commands:
            embed.add_field(name=self.get_command_signature(command), value=command.help, inline=False)

        embed.set_footer(text=f"Use `{self.context.prefix}help [command]` for more info on a specific command.")
        await self.get_destination().send(embed=embed)

    async def send_command_help(self, command):
        """Handles `!help <command>`."""
        embed = discord.Embed(title=f"Help for `!{command.name}`", color=discord.Color.dark_teal())
        embed.add_field(name="Usage", value=self.get_command_signature(command), inline=False)
        embed.add_field(name="Description", value=command.help or "No description provided.", inline=False)
        if command.aliases:
            aliases = ", ".join([f"`{alias}`" for alias in command.aliases])
            embed.add_field(name="Aliases", value=aliases, inline=False)

        await self.get_destination().send(embed=embed)

    async def send_error_message(self, error):
        """Handles errors, like a command not being found."""
        await self.get_destination().send(f"❌ {error}")

bot.help_command = CustomHelpCommand()


# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'✅ Bot logged in as {bot.user}')
    if CHANNEL_ID:
        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            # FIX 1: Removed the "Pre-loading AI model" message for a cleaner startup.
            await channel.send("📻 Radio Pipeline Bot is online. Use `!help` for commands.")
            asyncio.create_task(preload_whisper_model())
        else:
            print(f"❌ CRITICAL: Could not find channel with ID {CHANNEL_ID}.")
    else:
        print("❌ CRITICAL: DISCORD_COMMAND_CHANNEL_ID is not set in the .env file.")

# ==============================================================================
# SECTION: BOT COMMANDS
# ==============================================================================

@bot.command(name="config", help="Shows an interactive menu to enable/disable stations.\nMust be used when the pipeline is stopped.")
async def config(ctx):
    if pipeline_instance and pipeline_instance.is_running():
        await ctx.send("⚠️ Please stop the pipeline with `!stop` before changing the configuration.")
        return

    config_data = await load_bot_config()
    stations = config_data.get("radio_stations", []) if config_data else []
    embed = create_station_embed(stations)
    view = StationConfigView(stations)
    await ctx.send(embed=embed, view=view)


@bot.command(name="start", help="Starts the radio monitoring pipeline. Usage: !start [duration_in_minutes]")
async def start_pipeline(ctx, duration_minutes: int = 0):
    global pipeline_instance, pipeline_thread

    if pipeline_instance:
        await ctx.send("⚠️ The pipeline is already running.")
        return

    # FIX 1: Wait for the model to be ready, but don't send a message about it.
    if not model_ready_event.is_set():
        await ctx.message.add_reaction("⏳") # Add a reaction to indicate work is being done
        try:
            await asyncio.wait_for(model_ready_event.wait(), timeout=180.0) # 3 minute timeout
            await ctx.message.remove_reaction("⏳", bot.user)
        except asyncio.TimeoutError:
            await ctx.message.remove_reaction("⏳", bot.user)
            await ctx.send("❌ **Start Failed:** The AI model took too long to load or failed. Please check the console logs and try restarting the bot.")
            return

    if not whisper_model:
        await ctx.send("❌ **Start Failed:** The AI model could not be loaded due to an error. Check the console logs for details.")
        return

    config_data = await load_bot_config()
    enabled_stations = [s for s in (config_data or {}).get("radio_stations", []) if s.get("enabled", True)]
    duration_text = f"for {duration_minutes} minutes" if duration_minutes > 0 else "indefinitely"

    if enabled_stations:
        station_list_str = "\n".join([f"• `{s['name']}`" for s in enabled_stations])
        station_info = f"Attempting to monitor **{len(enabled_stations)}** enabled station(s):\n{station_list_str}"
    else:
        station_info = "⚠️ No stations are enabled in the configuration."

    start_message = (f"🚀 **Starting Pipeline {duration_text}...**\n\n{station_info}\n\n"
                     f"*Please wait, this may take a moment. A final confirmation will follow.*")
    await ctx.send(start_message)

    ready_event = threading.Event()
    notifier = DiscordBotNotifier(bot, CHANNEL_ID)
    pipeline_instance = Pipeline(config_path="config.yml", notifier=notifier, model=whisper_model)

    pipeline_thread = threading.Thread(target=pipeline_instance.start, args=(duration_minutes, ready_event), daemon=True)
    pipeline_thread.start()

    try:
        await asyncio.to_thread(ready_event.wait, timeout=60.0)
    except asyncio.TimeoutError:
         await ctx.send("❌ **Pipeline failed to start.** The startup process timed out. Check the console for errors with ffmpeg or stream connections.")
         if pipeline_instance: pipeline_instance.stop()
         if pipeline_thread: pipeline_thread.join()
         pipeline_instance, pipeline_thread = None, None
         return

    if pipeline_instance and pipeline_instance.is_running():
        active_stations = pipeline_instance.get_active_stations()
        enabled_stations_count = len(enabled_stations)

        if enabled_stations_count == 0:
            await ctx.send("✅ **Pipeline Started, but no stations are enabled.** Use `!config` to add stations, then `!stop` and `!start` again.")
        elif not active_stations:
             await ctx.send("✅ **Pipeline started, but failed to connect to any enabled streams.** Check stream URLs and console logs.")
        else:
            station_names = ", ".join([f"`{s.name}`" for s in active_stations])
            await ctx.send(f"✅ **Pipeline Started!**\nMonitoring {len(active_stations)} station(s) {duration_text}:\n{station_names}")
    else:
        await ctx.send(f"❌ **Pipeline failed to start.** Check the console for critical errors.")


# FIX 2: Renamed command from 'stop_pipeline' to 'stop_command' and updated help text.
@bot.command(name="stop", help="Stops scanning the stations and shuts down the pipeline gracefully.")
async def stop_command(ctx):
    global pipeline_instance, pipeline_thread

    if not pipeline_instance or not pipeline_instance.is_running():
        await ctx.send("⚠️ The pipeline is not currently running.")
        return

    await ctx.send("🛑 **Stopping station scanning...** Waiting for all processes to finish gracefully.")

    await asyncio.to_thread(pipeline_instance.stop)
    if pipeline_thread:
        await asyncio.to_thread(pipeline_thread.join)

    pipeline_instance, pipeline_thread = None, None
    await ctx.send("✅ **Pipeline stopped successfully.** All stations are now offline.")


# FIX 2: Added a new `shutdown` command that stops the pipeline and then the bot.
@bot.command(name="shutdown", help="Stops the pipeline (if running) and shuts down the bot completely.")
async def shutdown_command(ctx):
    global pipeline_instance
    await ctx.send("🛑 **Initiating full bot shutdown...**")

    # Check if the pipeline is running and stop it first.
    if pipeline_instance and pipeline_instance.is_running():
        await ctx.send("↳ Stopping active pipeline first...")
        stop_ctx = await bot.get_context(ctx.message) # Create a new context to invoke the stop command
        await stop_ctx.invoke(bot.get_command('stop'))

    await ctx.send("✅ Bot is shutting down. Goodbye!")
    await bot.close()


@bot.command(name="status", help="Shows the detailed real-time status of all configured stations.")
async def status(ctx):
    global pipeline_instance

    if not pipeline_instance or not pipeline_instance.is_running():
        # FIX 1: Removed check for model loading. The user just needs to know it's stopped.
        await ctx.send("ℹ️ The pipeline is currently stopped.")
        return

    embed = discord.Embed(title="📻 Pipeline Status: Running", color=discord.Color.green())

    uptime_seconds = time.time() - pipeline_instance.start_time
    embed.add_field(name="📈 Uptime", value=format_seconds(uptime_seconds), inline=True)

    chunk_duration = pipeline_instance.config['recording_chunk_duration_seconds']
    time_to_next_clip = chunk_duration - (uptime_seconds % chunk_duration)
    embed.add_field(name="⏱️ Next Clip In", value=f"{int(time_to_next_clip)} seconds", inline=True)

    all_stations_config = pipeline_instance.config.get('radio_stations', [])
    live_station_map = {s.name: s for s in pipeline_instance.stations}
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
            elif live_station.is_process_active:
                status_lines.append(f"🟢 **{name}** (Recording)")
            else:
                status_lines.append(f"🟡 **{name}** (Connecting/Retrying...)")

    embed.add_field(
        name=f"📡 Station States ({len(all_stations_config)} Configured)",
        value="\n".join(status_lines) or "No stations found.",
        inline=False
    )

    await ctx.send(embed=embed)


# --- Main Execution ---
if __name__ == "__main__":
    if not TOKEN or not CHANNEL_ID:
        print("❌ CRITICAL: DISCORD_BOT_TOKEN and/or DISCORD_COMMAND_CHANNEL_ID not found in .env file.")
    else:
        bot.run(TOKEN)