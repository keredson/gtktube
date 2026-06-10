# GTKTube

<p align="center">
  <img src="https://raw.githubusercontent.com/keredson/gtktube/main/gtktube/assets/gtktube.png" alt="GTKTube icon" width="128" height="128">
</p>

GTKTube is a local privacy-first Python/GTK4 YouTube player for Linux.
It stores subscriptions and viewing history locally in SQLite, and does not use a Google account.
The feed has no algorithm. It just shows recent videos from the channels you subscribe to.

## Features

- Browse recent videos from your subscribed channels without YouTube recommendations.
- Watch without ads.
- No tracking by GTKTube.
- Search YouTube for videos and channels without signing in.
- Subscribe and unsubscribe locally; no Google account, OAuth, cookies, or account sync.
- Play videos inside the GTK app with libmpv.
- Choose playback quality and speed, including speeds up to 4x.
- Use normal playback shortcuts for play/pause, seeking, fullscreen, and speed changes.
- Keep watching while browsing with a mini-player.
- Track watch history locally, including watched time ranges rather than only a last position.
- Search and review local watch history.
- Store thumbnails on disk and app data in SQLite.

## Screenshots

### Channel Browsing

![Channel grid with mini-player](https://raw.githubusercontent.com/keredson/gtktube/main/screenshots/Screenshot%20From%202026-06-10%2012-41-20.png)

### Player

![Video player](https://raw.githubusercontent.com/keredson/gtktube/main/screenshots/Screenshot%20From%202026-06-10%2012-42-24.png)

### Queue And Mini-Player

![Channel view with queue and mini-player](https://raw.githubusercontent.com/keredson/gtktube/main/screenshots/Screenshot%20From%202026-06-10%2012-43-41.png)

## Install

```sh
pip install gtktube
```

GTKTube also needs GTK4/PyGObject and libmpv from your Linux distribution.
If required system dependencies are missing, the app can launch a small installer helper.

## Run

GTKTube installs a desktop app entry, so you can launch it from your normal Linux app launcher.
You can also run it from a terminal:

```sh
gtktube
```

## Data Storage

GTKTube keeps its state on your machine:

- subscriptions and watch history in SQLite
- thumbnail cache under the user cache directory
- no Google account integration
- no cloud sync
- no analytics or tracking by GTKTube

## License

GTKTube is licensed under the GNU General Public License v3.0. See `LICENSE`.
