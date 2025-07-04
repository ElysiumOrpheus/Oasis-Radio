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

# --- Logging Setup ---
logger = logging.getLogger(__name__)

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from dotenv import load_dotenv

load_dotenv()

# ==============================================================================
# SECTION 1: CONFIGURATION & UTILITIES
# ==============================================================================

def load_config(path: str = "config.yml") -> Dict[str, Any]:
    """Loads the YAML configuration file."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
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
        self.channel = self.bot.get_channel(self.channel_id)
        self.loop = asyncio.get_running_loop()

    def send(self, message: str, pings: List[str] = []) -> None:
        """Sends a message to the pre-configured Discord channel."""
        if not self.channel or not self.loop.is_running():
            logger.warning(f"Notifier: Cannot find channel or event loop is not running. Message not sent: {message}")
            return

        ping_str = " ".join([f"<@{user_id}>" for user_id in pings])
        full_message = f"{ping_str}\n{message}".strip()

        asyncio.run_coroutine_threadsafe(self.channel.send(full_message), self.loop)

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

    def __init__(self, name: str, stream_url: str, config: Dict[str, Any], notifier: DiscordBotNotifier):
        self.name = name
        self.stream_url = stream_url
        self.config = config
        self.notifier = notifier
        self.sanitized_name = re.sub(r'[^\w\.-]', '_', self.name)
        self.process: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.is_running = False
        self.has_failed_permanently = False
        self.stop_event = threading.Event() ### <<< FIX 1: Add a threading.Event for interruptible waits

    @property
    def is_process_active(self) -> bool:
        """Returns True if the FFmpeg subprocess is currently running."""
        return self.process is not None and self.process.poll() is None

    def start_recording(self, total_duration_seconds: int) -> None:
        """Starts the FFmpeg recording process in a separate thread."""
        self.is_running = True
        self.has_failed_permanently = False
        self.stop_event.clear() # Ensure the event is not set from a previous run
        self.thread = threading.Thread(target=self._record_loop, args=(total_duration_seconds,), daemon=True)
        self.thread.start()

    def stop_recording(self) -> None:
        """Gracefully stops the FFmpeg recording process."""
        logger.info(f"[Recorder - {self.name}] Stop signal received. Attempting graceful shutdown...")
        self.is_running = False
        self.stop_event.set() ### <<< FIX 2: Set the event to interrupt any waiting threads (like in the retry delay)
        if self.process and self.process.poll() is None:
            try:
                # Give ffmpeg a moment to quit via 'q', but don't wait long.
                # The primary stop mechanism is now terminating the process.
                self.process.stdin.write(b'q\n')
                self.process.stdin.flush()
                self.process.terminate() # More forceful to ensure a quick stop
                self.process.wait(timeout=5)
                logger.info(f"[Recorder - {self.name}] FFmpeg shutdown gracefully.")
            except (IOError, ValueError, subprocess.TimeoutExpired) as e:
                logger.warning(f"[Recorder - {self.name}] Graceful shutdown failed ({e}). Killing process.")
                self.process.kill() # Final resort
        if self.thread:
            self.thread.join()

    def _record_loop(self, total_duration_seconds: int) -> None:
        paths = self.config['paths']
        retries = self.config.get('recording_retries', 3)
        retry_delay = self.config.get('recording_retry_delay_seconds', 60)
        chunk_duration = self.config['recording_chunk_duration_seconds']

        output_dir = os.path.join(paths['recordings'], self.sanitized_name)
        os.makedirs(output_dir, exist_ok=True)

        output_template = os.path.join(output_dir, f'{self.sanitized_name}_%Y-%m-%d_%H-%M-%S.aac')
        headers = {'Referer': 'https://www.iheart.com/', 'User-Agent': 'Mozilla/5.0'}
        ffmpeg_headers = "".join([f"{key}: {value}\r\n" for key, value in headers.items()])

        command = ['ffmpeg', '-v', 'error', '-headers', ffmpeg_headers, '-i', self.stream_url, '-t', str(total_duration_seconds),
                   '-f', 'segment', '-segment_time', str(chunk_duration), '-c:a', 'copy', '-strftime', '1', output_template]

        attempt = 0
        while self.is_running and attempt <= retries:
            if attempt > 0:
                logger.warning(f"[Recorder - {self.name}] Retrying... (Attempt {attempt}/{retries}) in {retry_delay}s")
                ### <<< FIX 3: Replace the blocking time.sleep() with an interruptible event.wait()
                # This will wait for `retry_delay` seconds OR until `stop_event.set()` is called.
                was_interrupted = self.stop_event.wait(timeout=retry_delay)
                if was_interrupted:
                    break # Exit the loop immediately if stop was called during the delay

            logger.info(f"[Recorder - {self.name}] Starting FFmpeg process (Attempt {attempt + 1}).")

            try:
                self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
                self.process.wait()
                stderr_bytes = self.process.stderr.read() if self.process.stderr else b""

                if not self.is_running:
                    break

                if self.process.returncode == 0:
                    logger.info(f"[Recorder - {self.name}] Recording session finished.")
                    return

                error_output = stderr_bytes.decode('utf-8', errors='ignore').strip()
                error_reason = "Unknown FFmpeg error"
                for pattern, reason in self.FFMPEG_ERROR_PATTERNS.items():
                    if pattern in error_output:
                        error_reason = reason
                        break

                logger.error(f"[Recorder - {self.name}] FFmpeg failed. Reason: {error_reason}")
                attempt += 1

            except Exception as e:
                # Check if the stop event was set, which might have caused the exception (e.g., by terminating the process)
                if self.stop_event.is_set():
                    logger.info(f"[Recorder - {self.name}] Process interrupted by stop signal.")
                    break
                logger.error(f"[Recorder - {self.name}] An unexpected error occurred: {e}", exc_info=True)
                attempt += 1

        if self.is_running:
            self.has_failed_permanently = True
            final_error_message = f"🛑 **Critical Recorder Failure**\n**Station:** `{self.name}`\nThe recorder has stopped after {retries + 1} failed attempts. Check logs for details."
            self.notifier.send(final_error_message)
            logger.critical(f"[Recorder - {self.name}] All recording attempts have failed.")
        else:
            logger.info(f"[Recorder - {self.name}] Recording loop has been stopped.")

class AudioProcessor(FileSystemEventHandler):
    def __init__(self, model: Any, config: Dict[str, Any], notifier: DiscordBotNotifier):
        self.model = model
        self.config = config
        self.notifier = notifier
        self.keywords = {str(k).lower().strip() for k in config.get('keywords', []) if k}
        self.semaphore = threading.Semaphore(config['max_concurrent_transcriptions'])
        self.write_lock = threading.Lock()
        self.processing_threads: List[threading.Thread] = []

    def on_created(self, event: Any) -> None:
        if event.is_directory or not event.src_path.endswith('.aac'): return
        time.sleep(1)
        try:
            if os.path.exists(event.src_path) and os.path.getsize(event.src_path) > 1024:
                thread = threading.Thread(target=self._process_file, args=(event.src_path,), daemon=True)
                self.processing_threads.append(thread)
                thread.start()
        except FileNotFoundError: pass

    def _process_file(self, audio_path: str) -> None:
        with self.semaphore:
            sanitized_station_name = os.path.basename(os.path.dirname(audio_path))
            station_name = sanitized_station_name.replace('_', ' ')
            logger.info(f"[Processor] Processing: {os.path.basename(audio_path)} for '{station_name}'")
            transcription = self._transcribe(audio_path, station_name)
            if transcription:
                hit_found = self._analyze(transcription, station_name, os.path.basename(audio_path))
                if not hit_found: self._cleanup(audio_path, transcription['path'])
            else:
                self._cleanup(audio_path)

    def _transcribe(self, audio_path: str, station_name: str) -> Optional[Dict[str, str]]:
        try:
            result = self.model.transcribe(audio_path, fp16=False)
            text = result["text"].strip()
            transcription_path = os.path.join(self.config['paths']['transcriptions'], f"{os.path.splitext(os.path.basename(audio_path))[0]}.txt")
            with open(transcription_path, "w", encoding="utf-8") as f: f.write(text)
            logger.info(f"[Transcriber - {station_name}] Transcription saved.")
            return {"text": text, "path": transcription_path}
        except Exception as e:
            logger.error(f"[Transcriber - {station_name}] Error: {e}", exc_info=True)
            return None

    def _analyze(self, transcription: Dict[str, str], station_name: str, audio_filename: str) -> bool:
        content_lower = transcription['text'].lower()
        for keyword in self.keywords:
            if re.search(r'\b' + re.escape(keyword) + r'\b', content_lower):
                logger.info(f"🚨 HIT! Keyword: '{keyword}' on Station: '{station_name}'")
                hit_report = (f"--- 🚨 HIT FOUND 🚨 ---\n"
                              f"**Station:**     `{station_name}`\n"
                              f"**Keyword:**     `{keyword}`\n"
                              f"**Source File:** `{audio_filename}`\n"
                              f"**Full Text:**   \n```\n{transcription['text']}\n```"
                              f"-----------------")
                user_ids_str = os.getenv("DISCORD_USER_IDS_TO_PING", "")
                pings = [uid.strip() for uid in user_ids_str.split(',') if uid.strip()]
                self.notifier.send(hit_report, pings)
                with self.write_lock:
                    with open(self.config['paths']['hits_file'], 'a', encoding='utf-8') as hits_f:
                        hits_f.write(hit_report.replace("`", "") + "\n\n")
                return True
        return False

    def _cleanup(self, audio_path: str, transcription_path: Optional[str] = None) -> None:
        try:
            if transcription_path and os.path.exists(transcription_path): os.remove(transcription_path)
            if os.path.exists(audio_path): os.remove(audio_path)
        except Exception as e: logger.error(f"[Cleaner] Error: {e}", exc_info=True)

    def wait_for_completion(self) -> None:
        logger.info("[Processor] Waiting for file processing to complete...")
        for t in self.processing_threads: t.join()
        logger.info("[Processor] File processing complete.")


class Pipeline:
    """The main orchestrator for the radio monitoring pipeline."""
    def __init__(self, config_path: str = "config.yml", notifier: Optional[DiscordBotNotifier] = None, model: Optional[Any] = None):
        self.config = load_config(config_path)
        self.notifier = notifier
        self.model = model
        self.stations: List[Station] = []
        self.observer: Optional[Observer] = None # type: ignore
        self.processor: Optional[AudioProcessor] = None
        self._is_running = False
        self.start_time: Optional[float] = None

    def is_running(self) -> bool:
        return self._is_running

    def get_active_stations(self) -> List[Station]:
        """Returns a list of only the stations with an active FFmpeg process."""
        if not self.is_running():
            return []
        return [station for station in self.stations if station.is_process_active]

    def _setup_environment(self) -> bool:
        print("--- Setting up environment ---")
        if not shutil.which("ffmpeg"):
            print("❌ CRITICAL: ffmpeg is not found.")
            return False
        for folder in self.config['paths'].values():
            if folder.endswith('.txt'): os.makedirs(os.path.dirname(folder), exist_ok=True)
            else: os.makedirs(folder, exist_ok=True)
        print("✅ Environment setup complete.")
        return True

    def _load_model(self) -> bool:
        """Loads the Whisper model, or confirms if a pre-loaded one was provided."""
        if self.model:
            print("✅ Using pre-loaded Whisper model.")
            return True

        print(f"--- Loading Whisper model ('{self.config['whisper_model']}') ---")
        try:
            import whisper
            self.model = whisper.load_model(self.config['whisper_model'])
            print("✅ Whisper model loaded successfully.")
            return True
        except Exception as e:
            print(f"❌ CRITICAL: Failed to load Whisper model: {e}")
            if self.notifier: self.notifier.send(f"❌ **CRITICAL:** Failed to load Whisper model: {e}")
            return False

    def start(self, duration_min: int = 0, ready_event: Optional[threading.Event] = None) -> None:
        """Initializes and starts all components of the pipeline."""
        try:
            if not self._setup_environment() or not self._load_model():
                self._is_running = False
                return

            all_stations_config = self.config.get('radio_stations', [])
            enabled_stations_config = [
                s for s in all_stations_config if s.get('enabled', True)
            ]

            if not enabled_stations_config:
                print("⚠️ WARNING: No stations are enabled in 'config.yml'. The pipeline will run but monitor nothing.")
                if self.notifier and not all_stations_config:
                     self.notifier.send("❌ **CRITICAL:** No radio stations defined in `config.yml`.")
                     self._is_running = False
                     return

            self.start_time = time.time()
            total_seconds = duration_min * 60 if duration_min > 0 else 31536000

            self.processor = AudioProcessor(model=self.model, config=self.config, notifier=self.notifier)
            self.observer = Observer()
            self.observer.schedule(self.processor, self.config['paths']['recordings'], recursive=True)
            self.observer.start()
            print(f"\n[Observer] Watching for new audio files in '{self.config['paths']['recordings']}'...")

            print(f"\n--- Starting Recorders for {len(enabled_stations_config)} Enabled Station(s) ---")
            for station_config in enabled_stations_config:
                print(f"[Pipeline] Initializing: {station_config['name']}")
                station = Station(station_config['name'], station_config['stream_url'], self.config, self.notifier)
                self.stations.append(station)
                station.start_recording(total_seconds)

            self._is_running = True
            print("\n--- Pipeline is running. ---")

        finally:
            if ready_event:
                ready_event.set()

        if self._is_running:
            while self._is_running:
                time.sleep(1)
            print("[Main] Pipeline thread received stop signal. Cleaning up...")
            self._shutdown()

    def stop(self) -> None:
        """Sets the flag to stop the pipeline loop."""
        print("\n[Main] Shutdown initiated via stop command.")
        if self.is_running() and self.notifier:
            self.notifier.send("🛑 **Pipeline stopping.**")
        self._is_running = False
        self.start_time = None

    def _shutdown(self) -> None:
        """The actual cleanup logic that runs after the pipeline loop exits."""
        # --- 1. Stop Recorders in Parallel ---
        print("[Main] Sending stop signal to all recorders in parallel...")
        stop_threads = []
        for station in self.stations:
            thread = threading.Thread(target=station.stop_recording)
            thread.daemon = True
            thread.start()
            stop_threads.append(thread)

        for thread in stop_threads:
            thread.join()
        print("[Main] All recorders have been stopped.")

        # --- 2. Stop the File Watcher ---
        print("[Main] Stopping observer...")
        if self.observer and self.observer.is_alive():
            self.observer.stop()
            self.observer.join()

        # --- 3. Wait for final transcriptions ---
        if self.processor:
            self.processor.wait_for_completion()

        print("\n✅ [Main] Shutdown successful.")