# GTKTube

<p align="center">
  <img src="https://raw.githubusercontent.com/keredson/gtktube/main/gtktube/assets/gtktube.png" alt="GTKTube icon" width="128" height="128">
</p>

GTKTube is a local privacy-first Python/GTK4 YouTube player for Linux.
It stores subscriptions and viewing history locally in SQLite, and does not require a Google account.
The feed has no algorithm. It just shows recent videos from the channels you subscribe to.

## Features

- Browse recent videos from your subscribed channels without YouTube recommendations.
- Watch without ads.
- No tracking by GTKTube.
- Search YouTube for videos and channels without signing in.
- Personalized home feed with recommended videos (requires browser cookies).
- Subscribe and unsubscribe locally; no Google account or OAuth required.
- Optionally import your YouTube subscriptions one time from browser cookies.
- Play videos inside the GTK app with libmpv.
- Choose playback quality and speed, including speeds up to 4x.
- Choose streaming or prefetch playback per quality/speed option; prefetch downloads
  the selected stream to a temporary app cache before playback to avoid throttling.
- Use normal playback shortcuts for play/pause, seeking, fullscreen, and speed changes.
- Keep watching while browsing with a mini-player.
- Save videos to Watch Later.
- Queue videos in Up Next and optionally save remaining queued videos to Watch Later
  when quitting.
- Download videos into GTKTube-managed local storage and play downloaded copies
  automatically when available.
- Track watch history locally, including watched time ranges rather than only a last position.
- Search and review local watch history.
- Optionally configure yt-dlp to extract browser cookies for age-restricted,
  members-only, recommendations, watch-history import, and subscription import features.
- Optionally use SponsorBlock to show and skip community-maintained segment ranges.
- Show video chapters, captions, SponsorBlock segments, and chapter ticks in the player.
- Limit how many videos a single channel can put in each feed day.
- Refresh subscriptions in parallel with per-channel progress in the sidebar, including
  automatic startup refreshes for channels that are due.
- Check PyPI for updates and upgrade/restart from inside the app.
- Store thumbnails on disk and app data in SQLite.

## Screenshots

### Channel Browsing

![Channel grid](https://raw.githubusercontent.com/keredson/gtktube/main/screenshots/channel.png)

### Player

![Video player](https://raw.githubusercontent.com/keredson/gtktube/main/screenshots/player.png)

### Mini-Player

![Mini-player while browsing](https://raw.githubusercontent.com/keredson/gtktube/main/screenshots/miniplayer.png)

## Install

```sh
pipx ensurepath
pipx install --system-site-packages gtktube
gtktube --install-desktop
```

GTKTube also needs GTK4/PyGObject, libmpv, and a JavaScript runtime for yt-dlp
from your Linux distribution.
If required system dependencies are missing, the app can launch a small installer helper.

On Debian/Ubuntu, install the system dependencies with:

```sh
sudo apt-get update
sudo apt-get install -y \
  python3-gi \
  python3-gi-cairo \
  gir1.2-gtk-4.0 \
  gir1.2-adw-1 \
  libmpv2 \
  python3-mpv \
  nodejs
```

The same package list is used by `scripts/install-apt-deps.sh` and the in-app
dependency installer.

## Run

GTKTube installs a desktop app entry, so you can launch it from your normal Linux app launcher.
You can also run it from a terminal:

```sh
gtktube
```

When launched as the installed `gtktube` command, GTKTube also installs or updates
its desktop launcher entry in your user application directory. If your desktop
environment does not show it immediately after installation, run `gtktube` once
from a terminal or run `gtktube --install-desktop`.

Useful startup flags:

- `gtktube --show-upgrade` opens the upgrade dialog even when GTKTube has not detected a newer version.
- `gtktube --show-deps-installer` opens the system dependency installer even when GTKTube thinks dependencies are present.
- `gtktube --db /path/to/gtktube.sqlite3` uses a specific SQLite database file.
- `gtktube --install-desktop` installs the desktop launcher entry and exits.
- `gtktube -v` or `gtktube --verbose` prints detailed playback diagnostics to stderr.

Development runs with `python -m gtktube` do not auto-install a desktop launcher or check PyPI for upgrades.

Developer-only modal preview flag:

- `python -m gtktube --debug-modal=open-url`
- `python -m gtktube --debug-modal=import-subscriptions`
- `python -m gtktube --debug-modal=sponsorblock`
- `python -m gtktube --debug-modal=up-next-quit`
- `python -m gtktube --debug-modal=update`

## Updates

When launched from an installed `gtktube` command, GTKTube checks PyPI for newer
releases. If an update is available, the app can run the appropriate upgrade
command and restart itself. Pipx installs use `pipx upgrade gtktube`; other
installs use `python3 -m pip install --upgrade gtktube`.

## Data Storage

GTKTube keeps its state on your machine:

- subscriptions and watch history in SQLite
- thumbnails, mpv cache files, and temporary prefetch playback cache under the user cache directory
- downloaded videos under the user data directory
- optional one-time browser-cookie reads for importing YouTube subscriptions
  and watch history
- no cloud sync
- no analytics or tracking by GTKTube

## License

GTKTube is licensed under the GNU General Public License v3.0. See `LICENSE`.
