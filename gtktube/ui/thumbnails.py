from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from gtktube.models import Video


class ThumbnailMixin:
    def load_thumbnail(self, video: Video, picture: Gtk.Picture) -> None:
        url = self.display_thumbnail_url(video)
        self.load_cached_image(
            url,
            picture,
            self.jpeg_thumbnail_url(url),
            suffix=".jpg",
            width=232,
            height=131,
        )

    def load_cached_image(
        self,
        url: str,
        picture: Gtk.Picture,
        download_url: str | None = None,
        suffix: str = ".img",
        width: int = 232,
        height: int = 174,
        log_label: str | None = None,
    ) -> None:
        url = self.absolute_media_url(url)
        download_url = self.absolute_media_url(download_url or url)
        path = self.thumbnail_path(url, suffix)

        def decode_and_set(image_path: Path) -> bool:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(image_path),
                    width,
                    height,
                    True,
                )
                texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            except GLib.Error:
                if log_label:
                    self.log(f"{log_label} image decode failed path={image_path} url={url}")
                if image_path == path:
                    try:
                        image_path.unlink()
                    except OSError:
                        pass
                return False

            picture.set_paintable(texture)
            return False

        if path.exists():
            future = self.executor.submit(
                GdkPixbuf.Pixbuf.new_from_file_at_scale,
                str(path),
                width,
                height,
                True,
            )

            def done_cached() -> bool:
                try:
                    pixbuf = future.result()
                    texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                    picture.set_paintable(texture)
                except Exception:
                    if log_label:
                        self.log(f"{log_label} cached image decode failed path={path} url={url}")
                    try:
                        path.unlink()
                    except OSError:
                        pass
                return False

            future.add_done_callback(lambda _f: GLib.idle_add(done_cached))
            return

        future = self.executor.submit(self.download_thumbnail, download_url, path)

        def done_download() -> bool:
            try:
                downloaded = future.result()
            except Exception:
                return False
            if downloaded.exists():
                GLib.idle_add(decode_and_set, downloaded)
            return False

        future.add_done_callback(lambda _f: GLib.idle_add(done_download))

    def absolute_media_url(self, url: str) -> str:
        if url.startswith("//"):
            return f"https:{url}"
        return url

    def set_thumbnail_file(
        self, picture: Gtk.Picture, path: Path, width: int, height: int
    ) -> bool:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(path),
                width,
                height,
                True,
            )
        except GLib.Error:
            return False
        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        picture.set_paintable(texture)
        return True

    def thumbnail_path(self, url: str, suffix: str = ".img") -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.thumbnail_dir / f"{digest}{suffix}"

    def thumbnail_cache_suffix(self, url: str) -> str:
        # Some URLs have extensions followed by query params: IkU3c1kERcw/hqdefault.jpg?sqp=...
        path = url.split("?", 1)[0].lower()
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            if path.endswith(ext):
                return ext
        return ".img"

    def download_thumbnail(self, url: str, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "GTKTube/0.1",
                "Accept": "image/*,*/*;q=0.5",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = response.read(2_000_000)
        except (TimeoutError, urllib.error.URLError):
            return path
        tmp_path.write_bytes(data)
        tmp_path.replace(path)
        return path

    def display_thumbnail_url(self, video: Video) -> str:
        if video.id and "/playlist" not in video.url and "list=" not in video.url:
            return f"https://img.youtube.com/vi/{video.id}/mqdefault.jpg"
        return self.jpeg_thumbnail_url(video.thumbnail_url or "")

    def jpeg_thumbnail_url(self, url: str) -> str:
        if "i.ytimg.com/vi_webp/" in url:
            url = url.replace("i.ytimg.com/vi_webp/", "i.ytimg.com/vi/")
        url = (
            url.replace("maxresdefault.webp", "mqdefault.jpg")
            .replace("mqdefault.webp", "mqdefault.jpg")
            .replace("hqdefault.webp", "mqdefault.jpg")
            .replace("maxresdefault.jpg", "mqdefault.jpg")
            .replace("hqdefault.jpg", "mqdefault.jpg")
        )
        return url.split("?", 1)[0]
