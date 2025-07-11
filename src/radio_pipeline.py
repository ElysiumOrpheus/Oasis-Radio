import time
import os
import threading
import subprocess
import shutil
import re
import yaml
import sys
import asyncio
from typing import Dict, Any, List, Optional
import logging
import logging.config

from dotenv import load_dotenv

load_dotenv()

# --- Logging Setup ---
logger = logging.getLogger(__name__)

# --- REMOVED WATCHDOG ---
# We no longer need watchdog as processing is triggered directly.

# ==============================================================================
# SECTION 1: CONFIGURATION & UTILITIES
# ==============================================================================

def load_config(path: str = "config.yml") -> Dict[str, Any]:
    """Loads the YAML configuration file."""
    logger.debug(f"Attempting to load configuration from '{path}'.")
    try:
        with open(path, 'r', encoding='utf-8') as f:
            logger.info(f"Configuration loaded successfully from '{path}'.")
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.critical(f"Configuration file not found at '{path}'.")
        sys.exit(1)
    except yaml.YAMLError as e:
        logger.critical(f"Error parsing the YAML configuration file: {e}", exc_info=True)
        sys.exit(1)

class DiscordBotNotifier:
    """A class to handle sending messages through the Discord bot client."""
    def __init__(self, bot_client: Any, channel_id: int):
        self.bot = bot_client
        self.channel_id = channel_id
        # No longer storing loop or channel here
        logger.debug(f"DiscordBotNotifier initialized for channel ID {channel_id}.")

    def send(self, message: str, pings: List[str] = []) -> None:
        """Sends a message to the pre-configured Discord channel."""
        # Get the channel and loop on-demand to ensure they are current
        channel = self.bot.get_channel(self.channel_id)
        
        if not channel:
            logger.warning(f"Notifier: Cannot find channel with ID {self.channel_id}. Message not sent: {message}")
            return
            
        if not self.bot.loop.is_running():
            logger.warning(f"Notifier: Event loop is not running. Message not sent: {message}")
            return

        ping_str = " ".join([f"<@{user_id}>" for user_id in pings])
        full_message = f"{ping_str}\n{message}".strip()

        logger.debug(f"Queuing Discord message for channel {self.channel_id}.")
        asyncio.run_coroutine_threadsafe(channel.send(full_message), self.bot.loop)
# ==============================================================================
# SECTION 2: CORE LOGIC CLASSES
# ==============================================================================

class Station:
    """Represents a single radio station and manages its recording process."""
    FFMPEG_ERROR_PATTERNS = {
        "Server returned 5XX Server Error": "Stream server issue (e.g., 502 Bad Gateway)",
        "404 Not Found": "Stream is offline (404 Not Found)",
        "403 Forbidden": "Access forbidden (403)",
        "Connection refused": "Connection refused by server",
    }

    # --- MODIFIED: Added 'processor' to init ---
    def __init__(self, station_config: Dict[str, Any], global_config: Dict[str, Any], notifier: DiscordBotNotifier, processor: 'AudioProcessor'):
        self.station_config = station_config
        self.global_config = global_config
        self.notifier = notifier
        self.processor = processor # Store the processor instance

        self.name = self.station_config['name']
        self.stream_url_template = self.station_config['stream_url']
        self.sanitized_name = re.sub(r'[^\w\.-]', '_', self.name)

        self.process: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.is_running = False
        self.has_failed_permanently = False
        self.has_completed_successfully = False
        self.stop_event = threading.Event()
        logger.info(f"Station object created for '{self.name}'.")

    def _get_dynamic_stream_url(self) -> str:
        """Replaces placeholders in the stream URL template."""
        url = self.stream_url_template
        if '{timestamp}' in url:
            url = url.replace('{timestamp}', str(int(time.time())))
            logger.debug(f"[{self.name}] Generated dynamic URL: {url}")
        return url

    @property
    def is_process_active(self) -> bool:
        """Returns True if the FFmpeg subprocess is currently running."""
        return self.process is not None and self.process.poll() is None

    def start_recording(self, total_duration_seconds: int) -> None:
        """Starts the FFmpeg recording process in a separate thread."""
        logger.info(f"[Recorder - {self.name}] Starting recording for {total_duration_seconds} seconds.")
        self.is_running = True
        self.has_failed_permanently = False
        self.has_completed_successfully = False
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._record_loop, args=(total_duration_seconds,), daemon=True)
        self.thread.start()
        logger.info(f"[Recorder - {self.name}] Recording thread started.")

    def stop_recording(self) -> None:
        """Gracefully stops the FFmpeg recording process."""
        logger.info(f"[Recorder - {self.name}] Stop signal received. Attempting graceful shutdown...")
        self.is_running = False
        self.stop_event.set()

        if self.is_process_active:
            logger.info(f"[Recorder - {self.name}] Terminating FFmpeg process (PID: {self.process.pid})...")
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
                logger.info(f"[Recorder - {self.name}] FFmpeg terminated gracefully.")
            except subprocess.TimeoutExpired:
                logger.warning(f"[Recorder - {self.name}] FFmpeg did not terminate in time. Killing...")
                self.process.kill()
                self.process.wait()
                logger.info(f"[Recorder - {self.name}] FFmpeg process killed.")

        if self.thread and self.thread.is_alive():
            logger.debug(f"[Recorder - {self.name}] Waiting for recording thread to join.")
            self.thread.join()
            logger.info(f"[Recorder - {self.name}] Shutdown complete.")

    # --- HEAVILY MODIFIED: This is the core logic change ---
    def _record_loop(self, total_duration_seconds: int) -> None:
        logger.info(f"[Recorder - {self.name}] Record loop entered.")
        paths = self.global_config['paths']
        retries = self.global_config.get('recording_retries', 3)
        retry_delay = self.global_config.get('recording_retry_delay_seconds', 60)
        chunk_duration = self.global_config['recording_chunk_duration_seconds']

        output_dir = os.path.join(paths['recordings'], self.sanitized_name)
        os.makedirs(output_dir, exist_ok=True)
        logger.debug(f"[{self.name}] Ensured output directory exists: {output_dir}")

        ffmpeg_config = self.global_config.get('ffmpeg_settings', {})
        loglevel = ffmpeg_config.get('loglevel', 'error')
        audio_codec = ffmpeg_config.get('codec', 'copy')

        global_headers = ffmpeg_config.get('headers', {}).copy()
        station_headers = self.station_config.get('headers', {})
        final_headers = {**global_headers, **station_headers}
        ffmpeg_headers_str = "".join([f"{key}: {value}\r\n" for key, value in final_headers.items()])

        is_finite_run = total_duration_seconds > 0
        time_elapsed = 0
        attempt = 0

        while self.is_running and attempt <= retries:
            remaining_time = float('inf')
            if is_finite_run:
                remaining_time = total_duration_seconds - time_elapsed
                if remaining_time <= 0:
                    logger.info(f"[Recorder - {self.name}] Target duration of {total_duration_seconds}s reached. Stopping recording.")
                    self.has_completed_successfully = True
                    break

            current_chunk_duration = min(chunk_duration, remaining_time)

            if attempt > 0:
                logger.warning(f"[Recorder - {self.name}] Retrying... (Attempt {attempt}/{retries}) in {retry_delay}s")
                if self.stop_event.wait(timeout=retry_delay):
                    logger.info(f"[Recorder - {self.name}] Stop signal received during retry wait. Aborting.")
                    break

            dynamic_url = self._get_dynamic_stream_url()
            timestamp_str = time.strftime('%Y-%m-%d_%H-%M-%S')
            output_filename = os.path.join(output_dir, f'{self.sanitized_name}_{timestamp_str}.aac')

            logger.info(f"[Recorder - {self.name}] Starting FFmpeg to record '{os.path.basename(output_filename)}'.")

            command = ['ffmpeg', '-v', loglevel]
            if ffmpeg_headers_str:
                command.extend(['-headers', ffmpeg_headers_str])
            command.extend([
                '-i', dynamic_url,
                '-t', str(current_chunk_duration),
                '-c:a', audio_codec,
                '-y',
                output_filename
            ])
            logger.debug(f"[{self.name}] Executing FFmpeg command: {' '.join(command)}")

            try:
                self.process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
                _, stderr_bytes = self.process.communicate()

                if not self.is_running:
                    logger.info(f"[Recorder - {self.name}] Process stopped by user command during recording.")
                    break

                if self.process.returncode == 0:
                    logger.info(f"[Recorder - {self.name}] Successfully recorded chunk to {os.path.basename(output_filename)}.")
                    self.processor.queue_file_for_processing(output_filename)

                    time_elapsed += current_chunk_duration # Use the requested duration
                    attempt = 0
                    continue
                else:
                    if os.path.exists(output_filename):
                        try:
                            os.remove(output_filename)
                            logger.warning(f"[{self.name}] Removed incomplete output file due to FFmpeg error: {output_filename}")
                        except OSError as e:
                            logger.error(f"[{self.name}] Failed to remove incomplete output file: {e}")

                    error_output = stderr_bytes.decode('utf-8', errors='ignore').strip()
                    error_reason = "Unknown FFmpeg error"
                    for pattern, reason in self.FFMPEG_ERROR_PATTERNS.items():
                        if pattern in error_output:
                            error_reason = reason
                            break
                    logger.error(f"[Recorder - {self.name}] FFmpeg process failed. Reason: {error_reason}. Return Code: {self.process.returncode}")
                    logger.debug(f"[{self.name}] Full FFmpeg stderr: {error_output}")
                    attempt += 1

            except Exception as e:
                if self.stop_event.is_set(): break
                logger.error(f"[Recorder - {self.name}] An unexpected error occurred in the record loop: {e}", exc_info=True)
                attempt += 1

        if self.is_running and not self.has_completed_successfully:
            self.has_failed_permanently = True
            final_error_message = f"🛑 **Critical Recorder Failure**\n**Station:** `{self.name}`\nThe recorder has stopped after {retries + 1} failed attempts. Check logs for details."
            self.notifier.send(final_error_message)
            logger.critical(f"[Recorder - {self.name}] All recording attempts have failed.")
        elif self.has_completed_successfully:
            logger.info(f"[Recorder - {self.name}] Recording loop finished successfully after reaching target duration.")
        else:
            logger.info(f"[Recorder - {self.name}] Recording loop has been stopped gracefully.")
        logger.info(f"[Recorder - {self.name}] Record loop exited.")

# --- MODIFIED: No longer inherits from FileSystemEventHandler ---
class AudioProcessor:
    def __init__(self, model: Any, config: Dict[str, Any], notifier: DiscordBotNotifier):
        self.model = model
        self.config = config
        self.notifier = notifier
        self.keywords = {str(k).lower().strip() for k in config.get('keywords', []) if k}
        self.semaphore = threading.Semaphore(config['max_concurrent_transcriptions'])
        self.write_lock = threading.Lock()
        self.processing_threads: List[threading.Thread] = []
        logger.info(f"AudioProcessor initialized with {len(self.keywords)} keywords and {config['max_concurrent_transcriptions']} concurrent transcriptions.")

    # --- NEW: This method replaces on_created and is called directly ---
    def queue_file_for_processing(self, audio_path: str):
        """Queues a completed audio file for transcription and analysis."""
        logger.info(f"[Processor] New audio file queued for processing: {os.path.basename(audio_path)}.")
        # No need to check size or sleep, as we know the file is complete.
        try:
            if os.path.exists(audio_path):
                thread = threading.Thread(target=self._process_file, args=(audio_path,), daemon=True)
                self.processing_threads.append(thread)
                thread.start()
            else:
                logger.warning(f"[Processor] Queued file disappeared before processing could start: {audio_path}")
        except Exception as e:
            logger.error(f"An error occurred while queueing file for processing: {e}", exc_info=True)

    def _process_file(self, audio_path: str) -> None:
        logger.debug(f"[Processor] Acquiring semaphore for '{os.path.basename(audio_path)}'")
        with self.semaphore:
            logger.debug(f"[Processor] Semaphore acquired for '{os.path.basename(audio_path)}'")
            sanitized_station_name = os.path.basename(os.path.dirname(audio_path))
            station_name = sanitized_station_name.replace('_', ' ')
            logger.info(f"[Processor] Processing: {os.path.basename(audio_path)} for '{station_name}'")

            transcription = self._transcribe(audio_path, station_name)
            if not transcription:
                logger.warning(f"[Processor] Transcription failed or was skipped for {os.path.basename(audio_path)}. Proceeding to cleanup.")
                self._cleanup(audio_path)
                return

            found_keyword = self._analyze(transcription)

            if found_keyword:
                logger.info(f"🚨 HIT! Keyword: '{found_keyword}' on Station: '{station_name}'")
                hit_report = (f"--- 🚨 HIT FOUND 🚨 ---\n"
                              f"**Station:**     `{station_name}`\n"
                              f"**Keyword:**     `{found_keyword}`\n"
                              f"**Source File:** `{os.path.basename(audio_path)}`\n"
                              f"**Full Text:**   \n```\n{transcription['text']}\n```"
                              f"-----------------")

                user_ids_str = os.getenv("DISCORD_USER_IDS_TO_PING", "")
                pings = [uid.strip() for uid in user_ids_str.split(',') if uid.strip()]
                self.notifier.send(hit_report, pings)

                with self.write_lock:
                    logger.debug(f"[Processor] Writing hit report to {self.config['paths']['hits_file']}")
                    with open(self.config['paths']['hits_file'], 'a', encoding='utf-8') as hits_f:
                        hits_f.write(hit_report.replace("`", "") + "\n\n")

                logger.info(f"[Processor] Hit found. Preserving '{os.path.basename(audio_path)}' and its transcription.")
            else:
                logger.info(f"[Processor] No keywords found in: {os.path.basename(audio_path)}")
                no_hit_report = (f"✅ **Analysis Complete (No Hit)**\n"
                                 f"**Station:** `{station_name}`\n"
                                 f"**Source File:** `{os.path.basename(audio_path)}`\n"
                                 f"*(Audio and transcription files have been deleted)*")
                self.notifier.send(no_hit_report, pings=[])
                self._cleanup(audio_path, transcription['path'])
        logger.debug(f"[Processor] Semaphore released for '{os.path.basename(audio_path)}'")


    def _transcribe(self, audio_path: str, station_name: str) -> Optional[Dict[str, str]]:
        logger.info(f"[Transcriber - {station_name}] Starting transcription for {os.path.basename(audio_path)}.")
        try:
            # Add a small delay and retry mechanism for reading the file, in case of filesystem lag
            for _ in range(3):
                if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1024:
                    break
                time.sleep(1)
            else:
                logger.warning(f"[Transcriber - {station_name}] File is empty or invalid after checks: {os.path.basename(audio_path)}")
                return None

            result = self.model.transcribe(audio_path, fp16=False)
            text = result["text"].strip()
            transcription_dir = self.config['paths']['transcriptions']
            os.makedirs(transcription_dir, exist_ok=True)
            transcription_path = os.path.join(transcription_dir, f"{os.path.splitext(os.path.basename(audio_path))[0]}.txt")
            with open(transcription_path, "w", encoding="utf-8") as f: f.write(text)
            logger.info(f"[Transcriber - {station_name}] Transcription successful. Text length: {len(text)}.")
            return {"text": text, "path": transcription_path}
        except RuntimeError as e:
            if "cannot reshape tensor of 0 elements" in str(e):
                logger.warning(f"[Transcriber - {station_name}] Skipped transcription for an empty/invalid audio file: {os.path.basename(audio_path)}")
                return None
            else:
                logger.error(f"[Transcriber - {station_name}] A runtime error occurred: {e}", exc_info=True)
                return None
        except Exception as e:
            logger.error(f"[Transcriber - {station_name}] Error: {e}", exc_info=True)
            return None

    def _analyze(self, transcription: Dict[str, str]) -> Optional[str]:
        logger.debug(f"[Analyzer] Analyzing transcription for keywords...")
        content_lower = transcription['text'].lower()
        for keyword in self.keywords:
            if re.search(r'\b' + re.escape(keyword) + r'\b', content_lower):
                logger.debug(f"[Analyzer] Keyword '{keyword}' found.")
                return keyword
        logger.debug("[Analyzer] No keywords found.")
        return None

    def _cleanup(self, audio_path: str, transcription_path: Optional[str] = None) -> None:
        logger.info(f"[Cleaner] Cleaning up files for {os.path.basename(audio_path)}.")
        try:
            if transcription_path and os.path.exists(transcription_path):
                os.remove(transcription_path)
                logger.debug(f"[Cleaner] Removed transcription: {transcription_path}")
            if os.path.exists(audio_path):
                os.remove(audio_path)
                logger.debug(f"[Cleaner] Removed audio: {audio_path}")
        except Exception as e: logger.error(f"[Cleaner] Error: {e}", exc_info=True)

    def wait_for_completion(self) -> None:
        active_threads = [t for t in self.processing_threads if t.is_alive()]
        logger.info(f"[Processor] Waiting for {len(active_threads)} file processing thread(s) to complete...")
        for t in self.processing_threads: t.join()
        logger.info("[Processor] File processing complete.")


class Pipeline:
    """The main orchestrator for the radio monitoring pipeline."""
    def __init__(self, config_path: str = "config.yml", notifier: Optional[DiscordBotNotifier] = None, model: Optional[Any] = None):
        self.config = load_config(config_path)
        self.notifier = notifier
        self.model = model
        self.stations: List[Station] = []
        self.processor: Optional[AudioProcessor] = None
        self._is_running = False
        self.start_time: Optional[float] = None
        logger.info("Pipeline object initialized.")

    def is_running(self) -> bool:
        return self._is_running

    def get_active_stations(self) -> List[Station]:
        if not self.is_running():
            return []
        active_stations = [station for station in self.stations if station.is_process_active]
        logger.debug(f"get_active_stations called. Found {len(active_stations)} active stations.")
        return active_stations

    def _setup_environment(self) -> bool:
        logger.info("Setting up environment...")
        if not shutil.which("ffmpeg"):
            logger.critical("ffmpeg executable not found in system PATH.")
            return False
        logger.info("ffmpeg executable found.")
        for name, folder in self.config['paths'].items():
            logger.debug(f"Ensuring directory for '{name}' exists at '{folder}'")
            if folder.endswith('.txt'): os.makedirs(os.path.dirname(folder), exist_ok=True)
            else: os.makedirs(folder, exist_ok=True)
        logger.info("Environment setup complete.")
        return True

    def _load_model(self) -> bool:
        if self.model:
            logger.info("Whisper model was pre-loaded, skipping load step.")
            return True
        model_name = self.config['whisper_model']
        logger.info(f"Loading Whisper model ('{model_name}')...")
        try:
            import whisper
            self.model = whisper.load_model(self.config['whisper_model'])
            logger.info("Whisper model loaded successfully.")
            return True
        except Exception as e:
            logger.critical(f"Failed to load Whisper model: {e}", exc_info=True)
            if self.notifier: self.notifier.send(f"❌ **CRITICAL:** Failed to load Whisper model: {e}")
            return False

    # --- MODIFIED: Observer code removed, processor passed to Station ---
    def start(self, duration_min: int = 0, ready_event: Optional[threading.Event] = None) -> None:
        """Initializes and starts all components of the pipeline."""
        logger.info(f"Pipeline start initiated. Duration: {duration_min} minutes.")
        try:
            if not self._setup_environment() or not self._load_model():
                self._is_running = False
                logger.critical("Pipeline start aborted due to environment or model loading failure.")
                return

            all_stations_config = self.config.get('radio_stations', [])
            enabled_stations_config = [s for s in all_stations_config if s.get('enabled', True)]
            logger.info(f"Found {len(enabled_stations_config)} enabled stations out of {len(all_stations_config)} total.")

            if not enabled_stations_config:
                logger.warning("No stations are enabled in 'config.yml'. The pipeline will run but monitor nothing.")
                if self.notifier and not all_stations_config:
                     self.notifier.send("❌ **CRITICAL:** No radio stations defined in `config.yml`.")
                     self._is_running = False
                     return

            self.start_time = time.time()
            total_seconds = duration_min * 60 if duration_min > 0 else 31536000 # Default to 1 year for indefinite

            self.processor = AudioProcessor(model=self.model, config=self.config, notifier=self.notifier)
            
            # OBSERVER CODE IS REMOVED.
            
            for station_config in enabled_stations_config:
                station = Station(
                    station_config=station_config,
                    global_config=self.config,
                    notifier=self.notifier,
                    processor=self.processor # Pass the processor instance here
                )
                self.stations.append(station)
                station.start_recording(total_seconds)

            self._is_running = True
            logger.info("--- Pipeline is now running. ---")

        finally:
            if ready_event:
                logger.debug("Setting pipeline ready_event.")
                ready_event.set()

        if self._is_running:
            logger.info("Pipeline entering main monitoring loop.")
            while self._is_running:
                time.sleep(1)
            logger.info("Pipeline monitoring loop exited. Initiating shutdown.")
            self._shutdown()

    def stop(self) -> None:
        logger.info("Pipeline stop command received.")
        if self.is_running() and self.notifier:
            self.notifier.send("🛑 **Pipeline stopping.**")
        self._is_running = False
        self.start_time = None

    # --- MODIFIED: Observer shutdown code removed ---
    def _shutdown(self) -> None:
        """The actual cleanup logic that runs after the pipeline loop exits."""
        logger.info("--- Pipeline shutdown sequence started. ---")
        
        logger.info("Stopping all station recorders...")
        stop_threads = []
        for station in self.stations:
            thread = threading.Thread(target=station.stop_recording)
            thread.daemon = True
            thread.start()
            stop_threads.append(thread)
        for thread in stop_threads:
            thread.join()
        logger.info("All station recorders stopped.")

        # OBSERVER SHUTDOWN CODE REMOVED.

        if self.processor:
            logger.info("Waiting for final audio processing to complete...")
            self.processor.wait_for_completion()
            logger.info("Final audio processing complete.")

        logger.info("--- Pipeline shutdown successful. ---")