# YouTube Downloader (Flask + yt-dlp)

Simple web app to download YouTube videos as **video** (MP4) or **audio** (MP3) using yt-dlp.

## Setup

1. **Create and use a virtual environment** (recommended):
   ```bash
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

## Setup with nginx

To run the app behind nginx (e.g. on a server with a domain), use a WSGI server such as **Gunicorn** and proxy to it with nginx.

1. **Install Gunicorn** (in the same venv as the app):
   ```bash
   source venv/bin/activate
   pip install gunicorn
   ```

2. **Run the app with Gunicorn** (e.g. bound to a local port or socket):
   ```bash
   # Listen on 127.0.0.1:5000 (nginx will proxy to this)
   gunicorn -w 1 -b 127.0.0.1:5000 "app:app"
   ```
   Use `-w 1` to avoid parallel yt-dlp runs if you prefer; increase workers if you want more concurrency.    For a Unix socket instead:
   ```bash
   gunicorn -w 1 -b unix:/tmp/yt_dl_server.sock "app:app"
   ```

3. **nginx setup (HTTPS)** — reverse proxy with TLS so the app is served over **https** only. You need a **domain name** pointing to your server (Let’s Encrypt does not issue certs for bare IPs).

   **3a. Install nginx and Certbot** (Debian/Ubuntu):
   ```bash
   sudo apt update
   sudo apt install nginx certbot python3-certbot-nginx
   ```

   **3b. Create the site config.** Use a temporary HTTP-only server block so Certbot can complete the ACME challenge; Certbot will then add HTTPS and redirect HTTP → HTTPS.
   ```bash
   sudo nano /etc/nginx/sites-available/yt-dl-server
   ```
   Paste the block below. Set `server_name` to **your domain** (e.g. `downloads.example.com`). Do not use a raw IP if you want Let’s Encrypt to work.

   **Initial config (HTTP only; Certbot will add HTTPS):**
   ```nginx
   server {
       listen 80;
       listen [::]:80;
       server_name your-domain.com;

       client_max_body_size 2M;

       location / {
           proxy_pass http://127.0.0.1:5000;
           proxy_http_version 1.1;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_buffering off;
           proxy_read_timeout 300s;
           proxy_connect_timeout 75s;
       }
   }
   ```
   If Gunicorn uses a **Unix socket**, use `proxy_pass http://unix:/tmp/yt_dl_server.sock;` instead of `http://127.0.0.1:5000;`.

   **3c. Enable the site and reload nginx:**
   ```bash
   sudo ln -s /etc/nginx/sites-available/yt-dl-server /etc/nginx/sites-enabled/
   sudo rm -f /etc/nginx/sites-enabled/default
   sudo nginx -t && sudo systemctl reload nginx
   ```

   **3d. Obtain the HTTPS certificate and turn on HTTPS.** Run Certbot with your domain; it will add a `listen 443 ssl` server and redirect port 80 to HTTPS:
   ```bash
   sudo certbot --nginx -d your-domain.com
   ```
   Follow the prompts (email, agree to terms). Choose to redirect HTTP to HTTPS when asked so the site is **HTTPS-only**.

   **3e. Verify:** Open **https://your-domain.com** in a browser. You should see the login page over HTTPS. HTTP requests will be redirected to HTTPS.

   **Troubleshooting:**
   - **502 Bad Gateway:** Gunicorn is not running or not listening on the address in `proxy_pass`. Start the app (e.g. `systemctl start yt-dl-server`) and check `journalctl -u yt-dl-server`.
   - **Certbot fails:** Ensure DNS for your domain points to this server and that ports 80 and 443 are open (e.g. `sudo ufw allow 80`, `sudo ufw allow 443`, `sudo ufw reload`).
   - **Config test fails:** Run `sudo nginx -t` and fix the reported file and line.

4. **Production**: Set `SECRET_KEY` and `FLASK_ENV=production` for the Gunicorn process (e.g. in a systemd unit or your shell):
   ```bash
   export SECRET_KEY="your-random-secret-key"
   export FLASK_ENV=production
   gunicorn -w 1 -b 127.0.0.1:5000 "app:app"
   ```

### Auto-start on boot (systemd)

Use a systemd service so the app and nginx start automatically when the system boots.

1. **Create a systemd service file** for the YouTube downloader app. Replace `YOUR_USER` with the Linux user that will run the app, and `/path/to/YT_DL_SERVER` with the real path to the project (e.g. `/home/ubuntu/YT_DL_SERVER`).

   ```bash
   sudo nano /etc/systemd/system/yt-dl-server.service
   ```

   Paste the following (adjust `User`, `WorkingDirectory`, and `ExecStart` paths, and set `SECRET_KEY` to a random string):

   ```ini
   [Unit]
   Description=YouTube Downloader (Gunicorn)
   After=network.target

   [Service]
   Type=simple
   User=YOUR_USER
   Group=YOUR_USER
   WorkingDirectory=/path/to/YT_DL_SERVER
   Environment="PATH=/path/to/YT_DL_SERVER/venv/bin"
   Environment="SECRET_KEY=your-random-secret-key-here"
   Environment="FLASK_ENV=production"
   ExecStart=/path/to/YT_DL_SERVER/venv/bin/gunicorn -w 1 -b 127.0.0.1:5000 "app:app"
   Restart=on-failure
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```

   Save and exit (in nano: Ctrl+O, Enter, Ctrl+X).

2. **Reload systemd, enable and start the service** so it runs now and on every boot:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable yt-dl-server
   sudo systemctl start yt-dl-server
   sudo systemctl status yt-dl-server
   ```
   You should see `active (running)`. If there are errors, check logs with:
   ```bash
   sudo journalctl -u yt-dl-server -f
   ```

3. **Make nginx start on boot** (if it does not already):
   ```bash
   sudo systemctl enable nginx
   sudo systemctl start nginx
   ```

4. **Useful commands** after setup:
   - Restart the app: `sudo systemctl restart yt-dl-server`
   - Stop the app: `sudo systemctl stop yt-dl-server`
   - View app logs: `sudo journalctl -u yt-dl-server -n 50` (last 50 lines) or `-f` to follow

The app is served over HTTPS as described in the nginx setup above.

## Usage

- Paste a YouTube URL in the text box.
- Click **Download Video** for MP4 or **Download Audio** for MP3.
- **Advanced**: open “Advanced: use cookie file” and upload a Netscape-format cookie file if the video needs login or is restricted.

### Download via curl (no browser)

You can download a URL with a POST request using curl. First log in to get a session cookie, then call the download endpoint with that cookie.

1. **Log in** (saves the session cookie to `cookies.txt`):
   ```bash
   curl -c cookies.txt -X POST -d "password=YOUR_PASSWORD" https://your-server/login
   ```
   Replace `YOUR_PASSWORD` with your app password and `https://your-server` with your base URL (e.g. `http://127.0.0.1:5000` for local dev).

2. **Download video** (MP4) or **audio** (MP3):
   ```bash
   # Video (MP4)
   curl -b cookies.txt -X POST -F "url=https://www.youtube.com/watch?v=VIDEO_ID" https://your-server/ddddd/vvvvv -o video.mp4

   # Audio (MP3)
   curl -b cookies.txt -X POST -F "url=https://www.youtube.com/watch?v=VIDEO_ID" https://your-server/ddddd/aaaaa -o audio.mp3
   ```

   Optional: to use a Netscape-format cookie file for age-restricted or member-only videos, add `-F "cookies=@/path/to/cookies.txt"` to the curl command.

## Cookie file

Export cookies from your browser (e.g. with an extension like “Get cookies.txt”) in Netscape format and upload that file in the Advanced section when needed.
