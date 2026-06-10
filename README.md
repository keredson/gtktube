# GTKTube

GTKTube is a local-first Python/GTK4 YouTube player. It uses `yt-dlp` to resolve
metadata and stream URLs, stores subscriptions and viewing state in SQLite, and
does not use a Google account.

## Run

Install runtime dependencies for GTK4/PyGObject and GStreamer on Debian/Ubuntu:

```sh
./scripts/install-apt-deps.sh
```

There is also a minimal GTK installer helper that checks installed packages and
uses `gksu` when available, falling back to `pkexec`:

```sh
./scripts/install-deps-gui.py
```

Then install the Python package dependencies:

```sh
python3 -m pip install -e .
```

If you use a virtualenv on Debian/Ubuntu, create it with system site packages so
it can see `python3-gi`:

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m gtktube
```

For a plain requirements install:

```sh
python3 -m pip install -r requirements.txt
```

Launch:

```sh
gtktube
```

## Playback Troubleshooting

GTKTube defaults to a conservative progressive stream format for `Gtk.Video`:
MP4/H.264/AAC up to 720p, with lower-quality fallbacks. You can override the
`yt-dlp` format selector when testing playback performance:

```sh
GTKTUBE_YTDLP_FORMAT='best[height<=480][acodec!=none][vcodec!=none]' python -m gtktube
```

The app stores data under the XDG data/cache/config directories, using
`~/.local/share/gtktube/gtktube.sqlite3` by default.

## Current Scope

- Play a YouTube URL through a GStreamer `playbin` pipeline rendered into GTK4.
- Subscribe to channels locally.
- Refresh recent subscription videos with `yt-dlp`.
- Search YouTube through `yt-dlp` and store local search history.
- Browse and search local watch history.
- Store watched time ranges in SQLite.
- Compute percent watched through a SQLite view.
