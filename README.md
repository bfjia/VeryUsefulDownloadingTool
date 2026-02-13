# YouTube Downloader (Flask + yt-dlp)

Simple web app to download YouTube videos as **video** (MP4) or **audio** (MP3) using yt-dlp.

## Setup

1. **Create and use a virtual environment** (recommended):
   ```bash
   cd /mnt/c/Users/BFJIA/OneDrive/ProjectMisc/YT_DL_SERVER
   sudo apt install -y python3-venv python3-pip   # if needed
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Login password**: Create a password file so the app can start. The app reads the login password from `.secrets/password` (one line, no trailing newline required). Alternatively set the `APP_PASSWORD` environment variable.

   ```bash
   mkdir -p .secrets
   echo -n 'your_password_here' > .secrets/password
   ```

3. **Optional**: FFmpeg is required for merging video+audio and for MP3 extraction. Install if needed:

   ```bash
   sudo apt install ffmpeg   # Linux
   # or download from https://ffmpeg.org
   ```

## Run

```bash
source venv/bin/activate
python app.py
```

Open http://127.0.0.1:5000 in your browser.

## Usage

- Paste a YouTube URL in the text box.
- Click **Download Video** for MP4 or **Download Audio** for MP3.
- **Advanced**: open “Advanced: use cookie file” and upload a Netscape-format cookie file if the video needs login or is restricted.

## Cookie file

Export cookies from your browser (e.g. with an extension like “Get cookies.txt”) in Netscape format and upload that file in the Advanced section when needed.
