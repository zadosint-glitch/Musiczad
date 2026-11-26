# MAFIA MUSIC API PRO

YouTube metadata search + official YouTube embedded player. This project does **not** download or extract MP3 audio.

## Features
- `/api/search` supports `type=info`, `type=play`, and `type=both`.
- Admin dashboard for adding/removing/enabling/disabling YouTube Data API keys.
- Automatic key failover: quota/invalid-key/rate-limit/server errors cause the next key to be tried. Failed keys are put into a one-hour cooldown.
- Client API keys with optional expiry and request limits.
- Recent request logs, health endpoint, responsive white/electric-blue glass UI.
- Official YouTube iframe player route: `/player/VIDEO_ID`.

## Local run (Termux / Linux)
```bash
pkg update -y
pkg install python -y
pip install -r requirements.txt
export ADMIN_USER=MAFIA
export ADMIN_PASSWORD='choose-a-strong-password'
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
python app.py
```
Open `http://127.0.0.1:5000`. First run prints a generated client API key in the terminal. Save it.

## Admin
- Open `/login`.
- Default username is `MAFIA`.
- Set `ADMIN_PASSWORD` before public deployment. The fallback password `CHANGE_THIS_PASSWORD` is not suitable for deployment.
- Add multiple YouTube Data API v3 keys under Admin > YouTube API keys.

## API
```text
GET /api/health
GET /api/search?q=Parvati&type=info&api_key=CLIENT_KEY
GET /api/search?q=Parvati&type=play&api_key=CLIENT_KEY
GET /api/search?q=Parvati&type=both&api_key=CLIENT_KEY
```
You may also send the client key in `X-API-Key` header.

- `info`: metadata and YouTube video URLs.
- `play`: metadata plus official YouTube player `embedUrl` and `watchUrl`.
- `both`: same combined response as `play` (metadata + player details).

The `play` mode returns player information; it does not return an MP3 file. Use `/player/VIDEO_ID` to open the embedded player page.

## YouTube API key setup
1. Create/select a project at https://console.cloud.google.com/
2. Enable YouTube Data API v3.
3. Create API credentials (API key).
4. Add the key in Admin > YouTube API keys. Add more than one key for failover.
5. Restrict keys to YouTube Data API v3 in Google Cloud where possible.

YouTube quota and policies are controlled by Google and may change. Multiple keys must belong to projects you are authorized to use; key rotation does not grant extra quota beyond Google's rules.

## Render deployment
1. Push project files to a private GitHub repository.
2. Create a Render Web Service from that repository.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. Set `ADMIN_USER`, a strong `ADMIN_PASSWORD`, and a long random `SECRET_KEY`.
6. Add a persistent disk and set `DATABASE_PATH=/var/data/mafia_music.db` if your Render plan supports persistent disks.

The included `render.yaml` requests a 1 GB persistent disk; disk availability/pricing depends on the current Render plan. If the plan does not support it, remove the `disk` section and use an appropriate writable database path, understanding that local SQLite data may be lost on redeploy/restart.

## Security notes
- Never commit YouTube keys or client keys to public GitHub.
- Use HTTPS in production.
- Set a strong admin password and secret key.
- API keys are stored in SQLite; protect the database and server access.
- Do not expose the admin panel publicly without strong credentials.
