# Radio Contest Hunter

This is an automated Python pipeline designed to monitor a live radio stream, transcribe the audio in near real-time, and send alerts to a Discord channel when specific contest-related keywords are detected.

It uses `ffmpeg` to record the stream, `watchdog` to monitor for new audio files, and OpenAI's `Whisper` model for highly accurate transcription. This tool helps you never miss a "cue to call" or a chance to win.

## Features

-   **Gapless Recording:** Records a live audio stream into manageable, timestamped chunks without losing any audio between files.
-   **Concurrent Processing:** Uses a `Semaphore` to transcribe multiple audio files at once, maximizing throughput without overwhelming your system.
-   **Real-time File Monitoring:** Utilizes `watchdog` to immediately process audio files the moment they are fully written to disk.
-   **Accurate Keyword Detection:** Employs regular expressions (`regex`) for whole-word matching, ensuring high precision and eliminating false positives (e.g., won't trigger "win" in "winning").
-   **Instant Discord Notifications:** Sends a formatted alert to a Discord webhook, pinging specified users with the keyword, source file, and the full transcribed text.
-   **Smart Cleanup:** Automatically deletes audio and transcription files that do not contain any hits, saving disk space.
-   **Graceful Shutdown:** Can be stopped with `Ctrl+C` at any time, ensuring all pending files are processed before the program fully exits.
-   **Secure and Maintainable:** Keeps secrets (`.env`) and keyword lists (`keywords.txt`) separate from the source code.

## Project Structure

```
your_project_folder/
├── .env                  # Your secret credentials (Discord webhook, etc.)
├── .gitignore            # Tells Git which files to ignore
├── keywords.txt          # Your customizable list of keywords to search for
├── README.md             # This file
├── requirements.txt      # List of Python libraries needed for the project
├── radio_pipeline.py     # The main Python script
├── audio_to_process/     # Folder where ffmpeg saves audio chunks
├── transcriptions/       # Folder where text transcriptions are stored
└── venv/                 # Python virtual environment
```

---

## Setup and Installation

### Prerequisites

-   **Python 3.8+**
-   **ffmpeg:** You must have `ffmpeg` installed and accessible in your system's PATH. You can download it from [ffmpeg.org](https://ffmpeg.org/download.html).

### Step-by-Step Guide

1.  **Clone the Repository (or download the files)**
    ```bash
    git clone https://your-repository-url.com/project.git
    cd project
    ```

2.  **Create and Activate a Virtual Environment**
    This isolates the project's dependencies from your system.
    ```bash
    # Create the virtual environment
    python -m venv venv

    # Activate it
    # On Windows:
    .\venv\Scripts\activate
    # On macOS/Linux:
    source venv/bin/activate
    ```

3.  **Install Required Libraries**
    Create a `requirements.txt` file with the following content:
    ```txt
    # requirements.txt
    openai-whisper
    requests
    watchdog
    python-dotenv
    ```
    Then, install them all with one command:
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure Your Environment**
    You need to create two configuration files in the root directory:

    -   **`.env`:** Create this file to store your secrets. **This file should NOT be committed to Git.**
        ```ini
        # .env file
        DISCORD_WEBHOOK_URL="your_discord_webhook_url_here"
        DISCORD_USER_IDS_TO_PING="your_id,your_friends_id"
        ```

    -   **`keywords.txt`:** Create this file and populate it with one keyword or phrase per line.
        ```
        # keywords.txt
        contest
        giveaway
        chance to win
        call now
        caller number
        text the keyword
        ```

---

## ▶️ How to Run

1.  Make sure your virtual environment is activated.
2.  Run the script from your terminal:
    ```bash
    python radio_pipeline.py
    ```
3.  The script will prompt you to enter the total recording duration in minutes. Enter `0` to have it run indefinitely until you stop it with `Ctrl+C`.

    ```
    Enter total recording duration in minutes (or 0 to run forever): 60
    ```
4.  The pipeline will start. You will see status updates in the console and receive notifications in Discord upon a successful hit.

---

## ⚙️ How It Works

The pipeline follows these steps:
1.  **Record:** The `recorder_thread` starts `ffmpeg`, which continuously records the live stream and saves it into segmented `.aac` files (e.g., 5 minutes each) in the `audio_to_process` folder.
2.  **Detect:** The `watchdog` observer monitors the `audio_to_process` folder. It cleverly waits until the *next* file is created before processing the *previous* one, ensuring the file is complete.
3.  **Transcribe:** A new thread is spawned for each completed audio file. A `Semaphore` limits the number of concurrent transcriptions to prevent system overload. The thread calls the `Whisper` model to convert audio to text.
4.  **Analyze:** The resulting text is scanned for any keywords from `keywords.txt` using case-insensitive, whole-word matching.
5.  **Notify & Save:** If a keyword is found, a detailed notification is sent to Discord, and the hit report is saved to `CONTEST_HITS.txt`. The source audio and transcription files are preserved.
6.  **Cleanup:** If no keywords are found, the corresponding audio and transcription files are deleted to conserve space.

---

## 🔧 Configuration

Most configuration is done via files (`.env`, `keywords.txt`), but some core parameters can be adjusted directly in `radio_pipeline.py`:

-   `STREAM_URL`: The URL of the radio stream to record.
-   `RECORDING_CHUNK_DURATION_SECONDS`: The length of each audio segment in seconds (default is 300, or 5 minutes).
-   `MAX_CONCURRENT_TRANSCRIPTIONS`: The number of transcription processes to run in parallel (default is 2). Adjust based on your CPU/GPU power.