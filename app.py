"""
Flask app for downloading YouTube videos/audio via yt-dlp.
"""
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from yt_dlp import YoutubeDL

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")

# Limit upload size (e.g. cookie file) to 1 MB
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
# Session security: cookie not readable by JS, same-site only
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if os.environ.get("FLASK_ENV") == "production":
    app.config["SESSION_COOKIE_SECURE"] = True

# Password: read from .secrets/password at startup (or APP_PASSWORD env); only the hash is kept in memory
_APP_ROOT = os.path.dirname(os.path.abspath(__file__))
_SECRETS_DIR = os.path.join(_APP_ROOT, ".secrets")
_PASSWORD_FILE = os.path.join(_SECRETS_DIR, "password")


def _load_password() -> str:
    pw = os.environ.get("APP_PASSWORD", "").strip()
    if pw:
        return pw
    if not os.path.isfile(_PASSWORD_FILE):
        raise RuntimeError(
            f"Password file not found: {_PASSWORD_FILE}. Create .secrets/password with one line (the login password)."
        )
    with open(_PASSWORD_FILE, "r", encoding="utf-8") as f:
        pw = f.read().strip()
    if not pw:
        raise RuntimeError(f"Password file is empty: {_PASSWORD_FILE}")
    return pw


_PASSWORD_HASH = generate_password_hash(_load_password(), method="scrypt")

# Persistent cookie file: once a cookie file is used successfully, it is saved here
# and used for all future requests (until someone uploads a new one that succeeds).
_COOKIE_DIR = os.path.join(_APP_ROOT, "data")
PERSISTENT_COOKIE_PATH = os.path.join(_COOKIE_DIR, "cookies.txt")

# Pending downloads when return_url=1: token -> (file_path, download_filename)
_PENDING_DOWNLOADS = {}

# YouTube URL patterns
YT_WATCH_RE = re.compile(
    r"(?:youtube\.com/watch\?.*\bv=([a-zA-Z0-9_-]{11})|youtu\.be/([a-zA-Z0-9_-]{11})|youtube\.com/shorts/([a-zA-Z0-9_-]{11}))",
    re.I,
)
YT_PLAYLIST_RE = re.compile(r"youtube\.com/playlist\?.*\blist=", re.I)
# Any valid YouTube URL (watch, short, playlist)
YT_VALID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?|shorts/)|youtu\.be/|youtube\.com/playlist\?)",
    re.I,
)


def _normalize_youtube_url(url: str) -> str | None:
    """
    Return a clean single-video YouTube URL (only v= or youtu.be/shorts id), or None if not a valid video URL.
    Rejects playlist URLs (caller should check _is_playlist_url first).
    """
    if not url or not url.strip():
        return None
    url = url.strip()
    if YT_PLAYLIST_RE.search(url):
        return None
    m = YT_WATCH_RE.search(url)
    if m:
        video_id = m.group(1) or m.group(2) or m.group(3)
        return f"https://www.youtube.com/watch?v={video_id}"
    return url


def _is_playlist_url(url: str) -> bool:
    """True if the URL is explicitly a YouTube playlist URL."""
    if not url or not url.strip():
        return False
    return bool(YT_PLAYLIST_RE.search(url.strip()))


def _is_youtube_url(url: str) -> bool:
    """True if the URL is a valid YouTube URL (watch, short, or playlist)."""
    if not url or not url.strip():
        return False
    return bool(YT_VALID_RE.search(url.strip()))


def _prepare_url(url: str) -> tuple[str | None, str | None]:
    """
    Validate and optionally sanitize URL. Returns (url_to_use, error_message).
    Playlist URLs are allowed; backend will download only the first video.
    """
    url = (url or "").strip()
    if not url:
        return None, "Please enter a URL."
    if not _is_youtube_url(url):
        return None, "Please enter a valid YouTube URL."
    if _is_playlist_url(url):
        return url, None  # allow playlist; _download will use playlist_items=1
    normalized = _normalize_youtube_url(url)
    return (normalized if normalized else url), None


def _video_id_from_url(url: str) -> str | None:
    """Extract YouTube video id from URL, or None."""
    if not url:
        return None
    m = YT_WATCH_RE.search(url.strip())
    if m:
        return m.group(1) or m.group(2) or m.group(3)
    return None


def _title_for_filename(info: dict | None, ext: str, url_fallback_id: str | None = None) -> str:
    """
    Build a safe download filename. Prefer video title from info; fall back to
    video id from URL (never from info, to avoid "!" placeholder). Never return "!".
    """
    # Use only URL-derived id for fallback (yt-dlp info can have id="!" when title fails)
    video_id = url_fallback_id
    if not video_id or len(str(video_id)) < 5:
        video_id = (info or {}).get("id") if info else None
    if not video_id or str(video_id).strip() in ("!", "") or len(str(video_id)) < 5:
        video_id = "video"
    video_id = str(video_id).strip()

    if not info:
        return f"{video_id}.{ext}"

    raw = (info.get("fulltitle") or info.get("title") or "").strip()
    # Strip unicode "!" variants (e.g. full-width ！ U+FF01) so we treat as missing
    for c in ("!", "\uFF01", "\u01C3", "\u203C"):
        raw = raw.replace(c, "")
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw or raw == "NA" or len(raw) <= 1:
        return f"{video_id}.{ext}"

    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", raw)
    safe = re.sub(r"\s+", " ", safe).strip()
    safe = (safe[:200]) if safe else ""
    # Reject if result looks like placeholder (e.g. only punctuation left)
    if not safe or safe in ("!", "\uFF01", "NA"):
        return f"{video_id}.{ext}"
    name = f"{safe}.{ext}"
    if name.startswith("!") or name.startswith("\uFF01"):
        return f"{video_id}.{ext}"
    return name


def _download(url: str, as_audio: bool, cookiefile_path: str | None) -> tuple[str | None, dict | None]:
    """
    Download from URL with yt-dlp. Returns (path to the downloaded file, info_dict) or (None, None) on failure.
    """
    if not url or not url.strip():
        return None, None

    out_dir = tempfile.mkdtemp(prefix="ytdl_")
    out_tmpl = os.path.join(out_dir, "out.%(ext)s")

    opts = {
        "outtmpl": out_tmpl,
        "quiet": True,
        "no_warnings": True,
    }
    if cookiefile_path and os.path.isfile(cookiefile_path):
        opts["cookiefile"] = cookiefile_path
    # Playlist URL: download only the first video
    if _is_playlist_url(url):
        opts["playlist_items"] = "1"

    if as_audio:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        opts["merge_output_format"] = "mp4"

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None, None
        files = list(Path(out_dir).iterdir())
        if not files:
            return None, None
        return str(files[0]), info
    except Exception:
        return None, None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("authenticated"):
            return redirect(url_for("index"))
        return render_template("login.html")
    password = (request.form.get("password") or "").strip()
    # Constant-time comparison is handled by check_password_hash
    if not password or not check_password_hash(_PASSWORD_HASH, password):
        return render_template("login.html", error="Invalid password."), 401
    session["authenticated"] = True
    session.permanent = True  # use permanent session (default 31 days)
    return redirect(request.args.get("next") or url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("authenticated", None)
    return redirect(url_for("login"))


@app.before_request
def require_auth():
    if request.endpoint in ("login", "logout"):
        return None
    if session.get("authenticated"):
        return None
    if request.method == "GET":
        return redirect(url_for("login", next=request.url))
    return jsonify({"error": "Authentication required.", "login_url": url_for("login")}), 401


@app.route("/")
def index():
    return render_template("index.html")


def _handle_download(as_audio: bool):
    url, err = _prepare_url(request.form.get("url") or "")
    if err:
        return jsonify({"error": err}), 400

    cookiefile_path = None
    uploaded_cookie_path = None
    if "cookies" in request.files:
        f = request.files["cookies"]
        if f and f.filename:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
                f.save(tmp.name)
                cookiefile_path = tmp.name
                uploaded_cookie_path = tmp.name
    if cookiefile_path is None and os.path.isfile(PERSISTENT_COOKIE_PATH):
        cookiefile_path = PERSISTENT_COOKIE_PATH

    try:
        path, info = _download(url, as_audio=as_audio, cookiefile_path=cookiefile_path)
        # On success with an uploaded cookie, persist it so everyone can use it
        if path and os.path.isfile(path) and uploaded_cookie_path and os.path.isfile(uploaded_cookie_path):
            try:
                os.makedirs(_COOKIE_DIR, exist_ok=True)
                shutil.copy2(uploaded_cookie_path, PERSISTENT_COOKIE_PATH)
            except OSError:
                pass
    finally:
        if uploaded_cookie_path and os.path.isfile(uploaded_cookie_path):
            try:
                os.unlink(uploaded_cookie_path)
            except OSError:
                pass

    if not path or not os.path.isfile(path):
        return jsonify({"error": "Download failed. Check the URL and try again."}), 400

    download_name = "audio.mp3" if as_audio else "video.mp4"

    # If return_url=1 (form or query), return JSON with a download URL instead of the file
    return_url = request.form.get("return_url") or request.args.get("return_url")
    if return_url and str(return_url).strip() in ("1", "true", "yes"):
        token = secrets.token_urlsafe(16)
        _PENDING_DOWNLOADS[token] = (path, download_name)
        download_url = url_for("download_by_token", token=token, _external=True)
        return jsonify({"download_url": download_url, "filename": download_name})

    file_size = os.path.getsize(path)
    _CHUNK = 64 * 1024

    def _stream_and_cleanup():
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(_CHUNK)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.unlink(path)
                dirpath = os.path.dirname(path)
                if os.path.isdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass

    # Stream in chunks so Gunicorn does not use sendfile(); avoids worker
    # crash (SystemExit) when the client disconnects mid-download.
    safe_name = download_name.replace("\\", "\\\\").replace('"', '\\"')
    return Response(
        _stream_and_cleanup(),
        mimetype="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Content-Length": str(file_size),
        },
        direct_passthrough=True,
    )


@app.route("/ddddd/vvvvv", methods=["POST"])
def download_video():
    return _handle_download(as_audio=False)


@app.route("/ddddd/aaaaa", methods=["POST"])
def download_audio():
    return _handle_download(as_audio=True)


@app.route("/download/<token>", methods=["GET"])
def download_by_token(token):
    """Serve a file that was requested with return_url=1; delete after sending."""
    entry = _PENDING_DOWNLOADS.pop(token, None)
    if not entry:
        return jsonify({"error": "Download link invalid or already used."}), 404
    path, download_name = entry
    if not path or not os.path.isfile(path):
        return jsonify({"error": "File no longer available."}), 404
    file_size = os.path.getsize(path)
    _CHUNK = 64 * 1024

    def _stream_and_cleanup():
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(_CHUNK)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.unlink(path)
                dirpath = os.path.dirname(path)
                if os.path.isdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass

    safe_name = download_name.replace("\\", "\\\\").replace('"', '\\"')
    return Response(
        _stream_and_cleanup(),
        mimetype="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Content-Length": str(file_size),
        },
        direct_passthrough=True,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
