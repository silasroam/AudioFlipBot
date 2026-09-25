"""Unit-тесты модуля downloader.py (yt-dlp): настройки, разбор info, маппинг ошибок.

Сеть и yt-dlp не используются: синхронная часть (download_sync) подменяется, поэтому
проверяется именно наша логика — обёртка asyncio.to_thread, таймаут, лимит 2000 МБ и
поиск итогового mp3 в info.
"""

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import downloader

MAX_SPEC = 2000 * 1024 * 1024


class FakeYDL:
    """Заглушка yt_dlp.YoutubeDL: пишет фейковый mp3 и отдаёт info как yt-dlp.

    Умеет отличать предварительную проверку (download=False) от самой загрузки:
    файл создаётся и requested_downloads появляется только при download=True.
    """

    def __init__(self, opts, info, filename=None):
        self.opts = opts
        self.info = info
        self.filename = filename or f"{info.get('id', 'file')}.mp3"
        self.download_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def to_screen(self, message, *args, **kwargs):
        return None

    def extract_info(self, url, download=True):
        self.download_calls.append(download)
        info = dict(self.info)
        if download:
            out_dir = Path(self.opts["outtmpl"]).parent
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / self.filename
            path.write_bytes(b"fake-mp3")
            info.setdefault("requested_downloads", [{"filepath": str(path)}])
        return info


# ============================== НАСТРОЙКИ yt-dlp ==============================


def test_build_ydl_opts_matches_spec():
    """Ровно те настройки, что требует задача (плюс защитные): audio->mp3 192, лимит 2000 МБ."""
    opts = downloader.build_ydl_opts()

    assert opts["format"] == "bestaudio/best"
    assert opts["postprocessors"] == [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }
    ]
    assert opts["outtmpl"] == "downloads/%(id)s.%(ext)s"
    assert opts["quiet"] is True
    assert opts["no_warnings"] is True
    assert opts["noprogress"] is True
    assert opts["max_filesize"] == MAX_SPEC
    assert opts["noplaylist"] is True
    assert opts["socket_timeout"] > 0


def test_build_ydl_opts_returns_isolated_copy():
    """Правки одного вызова не портят шаблон YDL_OPTS и другие вызовы."""
    first = downloader.build_ydl_opts()
    second = downloader.build_ydl_opts()

    first["postprocessors"][0]["preferredcodec"] = "wav"
    first["outtmpl"] = "/tmp/other/%(id)s.%(ext)s"

    assert second["postprocessors"][0]["preferredcodec"] == "mp3"
    assert second["outtmpl"] == "downloads/%(id)s.%(ext)s"
    assert downloader.YDL_OPTS["postprocessors"][0]["preferredcodec"] == "mp3"
    assert downloader.YDL_OPTS["outtmpl"] == "downloads/%(id)s.%(ext)s"


def test_build_ydl_opts_overrides_outtmpl_for_job_isolation():
    opts = downloader.build_ydl_opts("/tmp/job-1/%(id)s.%(ext)s")
    assert opts["outtmpl"] == "/tmp/job-1/%(id)s.%(ext)s"
    assert downloader.YDL_OPTS["outtmpl"] == "downloads/%(id)s.%(ext)s"


def test_default_link_timeout_is_ten_minutes():
    assert downloader.LINK_TIMEOUT == 600


# ============================== РАЗБОР ОШИБОК И INFO ==============================


@pytest.mark.parametrize(
    "details",
    [
        "File is larger than max-filesize (3000 bytes > 2000 bytes). Aborting.",
        "max_filesize exceeded",
        "ERROR: video is TOO LARGE",
    ],
)
def test_is_too_large_error_true(details):
    assert downloader.is_too_large_error(details) is True


def test_is_too_large_error_false():
    assert downloader.is_too_large_error("HTTP Error 404: Not Found") is False
    assert downloader.is_too_large_error("") is False


def test_pick_downloaded_file_prefers_postprocessed_filepath(tmp_path):
    final = tmp_path / "vid.mp3"
    final.write_bytes(b"mp3")
    stale = tmp_path / "vid.webm"
    stale.write_bytes(b"webm")

    info = {
        "id": "vid",
        "requested_downloads": [{"filepath": str(final)}],
        "filepath": str(stale),
    }
    assert downloader.pick_downloaded_file(info, tmp_path) == final


def test_pick_downloaded_file_falls_back_to_id(tmp_path):
    final = tmp_path / "abc123.mp3"
    final.write_bytes(b"mp3")
    assert downloader.pick_downloaded_file({"id": "abc123"}, tmp_path) == final


def test_pick_downloaded_file_returns_none_when_no_audio(tmp_path):
    assert downloader.pick_downloaded_file({"id": "missing"}, tmp_path) is None


def test_expected_size_reads_filesize_and_approx():
    assert downloader.expected_size({"filesize": 100}) == 100
    assert downloader.expected_size({"filesize": None, "filesize_approx": 250}) == 250
    assert downloader.expected_size(
        {"requested_formats": [{"filesize": 10}, {"filesize_approx": 5}]}
    ) == 15
    assert downloader.expected_size({"requested_formats": [{"filesize": 10}, {}]}) is None
    assert downloader.expected_size({}) is None
    assert downloader.expected_size(None) is None


def test_first_entry_unwraps_playlist():
    entry = {"id": "e1"}
    assert downloader.first_entry({"entries": [entry]}) is entry

    solo = {"id": "solo"}
    assert downloader.first_entry(solo) is solo
    assert downloader.first_entry(None) is None


# ============================== СИНХРОННАЯ ЧАСТЬ ==============================


def test_download_sync_returns_path_and_clean_title(tmp_path, monkeypatch):
    info = {"id": "vid1", "title": "  My Track  "}
    monkeypatch.setattr(
        downloader, "yt_dlp", SimpleNamespace(YoutubeDL=lambda opts: FakeYDL(opts, info))
    )

    path, title = downloader.download_sync("https://example.com/x", tmp_path)

    assert path == tmp_path / "vid1.mp3"
    assert title == "My Track"


def test_download_sync_prechecks_size_and_skips_download(tmp_path, monkeypatch):
    """Размер известен и больше лимита — файл вообще не качаем."""
    monkeypatch.setattr(downloader, "MAX_LINK_SIZE", 1000)
    fake = FakeYDL(downloader.build_ydl_opts(), {"id": "big", "title": "Big", "filesize": 5000})
    monkeypatch.setattr(downloader, "yt_dlp", SimpleNamespace(YoutubeDL=lambda opts: fake))

    with pytest.raises(downloader.LinkTooLargeError):
        downloader.download_sync("https://example.com/big", tmp_path)

    assert fake.download_calls == [False]  # была только проба метаданных
    assert list(tmp_path.glob("*.mp3")) == []


def test_download_sync_survives_failed_metadata_probe(tmp_path, monkeypatch):
    """Если проба метаданных упала, загрузка всё равно выполняется."""

    class ProbeFailYDL(FakeYDL):
        def extract_info(self, url, download=True):
            if not download:
                raise RuntimeError("metadata unavailable")
            return super().extract_info(url, download=download)

    monkeypatch.setattr(
        downloader,
        "yt_dlp",
        SimpleNamespace(YoutubeDL=lambda opts: ProbeFailYDL(opts, {"id": "vid", "title": "Track"})),
    )

    path, title = downloader.download_sync("https://example.com/x", tmp_path)

    assert path.name == "vid.mp3"
    assert title == "Track"


class _BaseYDL:
    """Мини-двойник YoutubeDL: только to_screen, нужен для проверки make_ydl."""

    def __init__(self, opts):
        self.opts = opts
        self.printed = []

    def to_screen(self, message, *args, **kwargs):
        self.printed.append(str(message))
        return "printed"


def test_make_ydl_flags_too_large_message(monkeypatch):
    monkeypatch.setattr(downloader, "yt_dlp", SimpleNamespace(YoutubeDL=_BaseYDL))

    ydl = downloader.make_ydl({})

    assert ydl.too_large_seen is False
    ydl.to_screen("\r[download] File is larger than max-filesize (9 bytes > 1 bytes). Aborting.")
    assert ydl.too_large_seen is True
    assert ydl.printed  # исходный to_screen всё равно вызван


def test_make_ydl_ignores_regular_messages(monkeypatch):
    monkeypatch.setattr(downloader, "yt_dlp", SimpleNamespace(YoutubeDL=_BaseYDL))

    ydl = downloader.make_ydl({})
    ydl.to_screen("[download] 100% of 1.00MiB in 00:00")

    assert ydl.too_large_seen is False


def test_download_sync_detects_too_large_from_ytdlp_message(tmp_path, monkeypatch):
    """yt-dlp молча не создаёт файл, но пишет про max-filesize — отдаём LinkTooLargeError."""

    class HugeYDL(FakeYDL):
        def extract_info(self, url, download=True):
            if download:
                self.to_screen(
                    "\r[download] File is larger than max-filesize (9 bytes > 1 bytes). Aborting."
                )
                return {"id": "big", "title": "Big"}  # файла на диске нет
            return {"id": "big", "title": "Big"}

    monkeypatch.setattr(
        downloader,
        "yt_dlp",
        SimpleNamespace(YoutubeDL=lambda opts: HugeYDL(opts, {"id": "big", "title": "Big"})),
    )

    with pytest.raises(downloader.LinkTooLargeError):
        downloader.download_sync("https://example.com/big", tmp_path)


def test_download_sync_uses_first_playlist_entry(tmp_path, monkeypatch):
    entry = {"id": "e1", "title": "First Track"}
    info = {"id": "playlist", "entries": [entry]}
    monkeypatch.setattr(
        downloader,
        "yt_dlp",
        SimpleNamespace(YoutubeDL=lambda opts: FakeYDL(opts, info, filename="e1.mp3")),
    )

    path, title = downloader.download_sync("https://example.com/set", tmp_path)

    assert path.name == "e1.mp3"
    assert title == "First Track"


def test_download_sync_without_ytdlp_raises_link_error(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "yt_dlp", None)
    with pytest.raises(downloader.LinkError):
        downloader.download_sync("https://example.com/x", tmp_path)


def test_download_sync_raises_when_no_audio_file(tmp_path, monkeypatch):
    # yt-dlp «отработал», но mp3 на диске нет (например, битый постпроцессинг).
    info = {"id": "vid2", "title": "No file"}
    monkeypatch.setattr(
        downloader,
        "yt_dlp",
        SimpleNamespace(
            YoutubeDL=lambda opts: FakeYDL(
                opts, {**info, "requested_downloads": []}, filename="notaudio.bin"
            )
        ),
    )
    with pytest.raises(downloader.LinkError):
        downloader.download_sync("https://example.com/x", tmp_path)


# ============================== АСИНХРОННАЯ ОБЁРТКА ==============================


async def test_download_audio_by_url_runs_in_worker_thread(tmp_path, monkeypatch):
    """Главное требование: синхронный yt-dlp не блокирует event loop (ушёл в поток)."""
    main_thread = threading.get_ident()
    seen: dict = {}

    def fake_sync(url, out_dir):
        seen["thread"] = threading.get_ident()
        seen["url"] = url
        seen["out_dir"] = Path(out_dir)
        path = Path(out_dir) / "song.mp3"
        path.write_bytes(b"fake-mp3")
        return path, "Song Title"

    monkeypatch.setattr(downloader, "download_sync", fake_sync)

    path, title = await downloader.download_audio_by_url("https://example.com/a", tmp_path)

    assert path.read_bytes() == b"fake-mp3"
    assert title == "Song Title"
    assert seen["url"] == "https://example.com/a"
    assert seen["thread"] != main_thread  # работа выполнена не в потоке event loop


async def test_download_audio_by_url_defaults_to_download_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "DOWNLOAD_DIR", tmp_path)
    seen: dict = {}

    def fake_sync(url, out_dir):
        seen["out_dir"] = Path(out_dir)
        path = Path(out_dir) / "x.mp3"
        path.write_bytes(b"x")
        return path, "x"

    monkeypatch.setattr(downloader, "download_sync", fake_sync)
    await downloader.download_audio_by_url("https://example.com/a")

    assert seen["out_dir"] == tmp_path


async def test_download_audio_by_url_too_large_by_file_size(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "MAX_LINK_SIZE", 10)

    def fake_sync(url, out_dir):
        path = Path(out_dir) / "big.mp3"
        path.write_bytes(b"x" * 11)
        return path, "Big"

    monkeypatch.setattr(downloader, "download_sync", fake_sync)

    with pytest.raises(downloader.LinkTooLargeError):
        await downloader.download_audio_by_url("https://example.com/big", tmp_path)


async def test_download_audio_by_url_too_large_by_error_text(tmp_path, monkeypatch):
    def fake_sync(url, out_dir):
        raise RuntimeError(
            "File is larger than max-filesize (3000 bytes > 2000 bytes). Aborting."
        )

    monkeypatch.setattr(downloader, "download_sync", fake_sync)

    with pytest.raises(downloader.LinkTooLargeError):
        await downloader.download_audio_by_url("https://example.com/big", tmp_path)


async def test_download_audio_by_url_maps_generic_error(tmp_path, monkeypatch):
    def fake_sync(url, out_dir):
        raise RuntimeError("HTTP Error 404: Not Found")

    monkeypatch.setattr(downloader, "download_sync", fake_sync)

    with pytest.raises(downloader.LinkError) as exc:
        await downloader.download_audio_by_url("https://example.com/x", tmp_path)

    assert not isinstance(exc.value, downloader.LinkTooLargeError)
    assert "404" in str(exc.value)


async def test_download_audio_by_url_keeps_link_error(tmp_path, monkeypatch):
    def fake_sync(url, out_dir):
        raise downloader.LinkError("yt-dlp не установлен")

    monkeypatch.setattr(downloader, "download_sync", fake_sync)

    with pytest.raises(downloader.LinkError) as exc:
        await downloader.download_audio_by_url("https://example.com/x", tmp_path)

    assert not isinstance(exc.value, downloader.LinkTooLargeError)


async def test_download_audio_by_url_timeout(tmp_path, monkeypatch):
    """Зависшую загрузку обрываем по таймауту (не ждём вечно)."""

    def slow_sync(url, out_dir):
        time.sleep(0.4)
        path = Path(out_dir) / "slow.mp3"
        path.write_bytes(b"x")
        return path, "Slow"

    monkeypatch.setattr(downloader, "download_sync", slow_sync)

    with pytest.raises(downloader.LinkError) as exc:
        await downloader.download_audio_by_url("https://example.com/x", tmp_path, timeout=0.05)

    assert "таймаут" in str(exc.value).lower()

