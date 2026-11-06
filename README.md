# tg-gallery-bot

Telegram bot: Twitter/X + Instagram posts, reels, profiles, stories via gallery-dl. Runs from its own venv.

## Setup
```
./setup.sh                      # venv + deps + .env
$EDITOR .env                    # BOT_TOKEN, ALLOWED_USER_IDS
# cookies (Netscape format, e.g. from a "cookies.txt" browser export, throwaway account):
#   cookies/instagram.txt   (required, nearly everything on IG needs login)
#   cookies/twitter.txt     (optional, needed for age-gated/rate-limited content)
chmod 600 cookies/*.txt
.venv/bin/python bot.py
```
Get your Telegram ID by sending `/id` to the bot (works for anyone, nothing else does).

## Usage
- Post / reel / single story link: media, caption, `by: user`, post link.
- Profile / `instagram.com/stories/<user>` / highlight (including `instagram.com/s/<id>` share links): downloads everything, media + username link. No item limit.
  Files are sent in albums of 10 as soon as they are downloaded (then deleted from `tmp/`), so disk use stays small and results show up early.
  The username link is on the first album. gallery-dl is paused while an album uploads. A profile stops if no new file arrives for `PROFILE_IDLE_TIMEOUT` seconds (default 300); there is no total time limit.
  While one profile runs, other requests wait (`MAX_CONCURRENT=1`); `/cancel` stops it.
- Multiple files always go out as albums (10 per album, remainder last: 11 -> 10+1, 12 -> 10+2). Photos, videos and GIFs share an album (GIFs become plain videos there); documents get their own album. If Telegram rejects an album, it is resent as a document album before falling back to single files. Only a lone file is sent on its own.
- `/cancel` kills your running download.

## Run as a service
See the header of `tg-gallery-bot.service`.

## Maintenance
```
.venv/bin/pip install -U gallery-dl && systemctl --user restart tg-gallery-bot   # IG/Twitter break it often
.venv/bin/python cleanup.py            # preview stale tmp dirs (dry-run)
.venv/bin/python cleanup.py --delete   # remove them
.venv/bin/python -m unittest discover -s tests
```
Auth errors (expired cookies) alert the first ID in `ALLOWED_USER_IDS`, at most once an hour.

## Uploads over 50 MB
Videos are re-encoded with ffmpeg (<=720p) if installed; otherwise skipped with a note. To lift the limit,
run a local Bot API server and set `LOCAL_API_URL` + `MAX_UPLOAD_MB=2000`.
