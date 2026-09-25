"""Скачивание и извлечение аудио по ссылке через yt-dlp (YouTube, TikTok, VK, SoundCloud и др.).

Отдельный модуль, как converter.py и database.py: bot.py только импортирует функции.

Почему так:
  * yt-dlp синхронный и умеет «зависать» на сети, поэтому его вызов обёрнут в
    asyncio.to_thread(...) — event loop бота остаётся свободным (приём других сообщений,
    прогресс, health-сервер продолжают работать);
  * сверху стоит asyncio.wait_for(...) с таймаутом (по умолчанию 10 минут), чтобы
    зависшая загрузка не держала задачу вечно;
  * mp3-дорожку извлекает системный ffmpeg (postprocessor FFmpegExtractAudio),
    поэтому ffmpeg обязателен, как и для остальной конвертации.

Изоляция: каждая задача получает свою папку (out_dir), поэтому одинаковые id видео
у разных пользователей не пересекаются, а итоговый файл затем переименовывается в
имя из БД (см. bot.on_link).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from strings import LogMessages

try:  # бот должен работать и без yt-dlp: ссылки просто вернут понятную ошибку
    import yt_dlp
except ImportError:  # pragma: no cover - зависит от окружения
    yt_dlp = None

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Таймаут на одну загрузку по ссылке. По умолчанию 10 минут; меняется переменной
# окружения YTDLP_TIMEOUT (секунды) — удобно для отладки и для слабых серверов.
LINK_TIMEOUT = int(os.getenv("YTDLP_TIMEOUT", "600"))

# Столько же, сколько у файлов из Telegram: больше и скачивать, и отправлять бессмысленно.
MAX_LINK_SIZE = 2000 * 1024 * 1024

# Настройки yt-dlp: лучшее аудио -> mp3 192 kbps, лимит 2000 МБ.
#   format               — берём чисто аудиодорожку, иначе лучшее, что есть;
#   postprocessors       — ffmpeg вынимает звук и перекодирует в mp3;
#   outtmpl              — временное имя по id видео (переименуем перед отправкой);
#   quiet/no_warnings    — не сыпать служебными сообщениями в логи Render;
#   noprogress           — отдельно гасим прогресс-бар загрузки (quiet его не выключает);
#   max_filesize         — жёсткий предел размера (защита от гигантских видео);
#   noplaylist           — одна ссылка = один трек (а не весь плейлист/сборник);
#   socket_timeout       — не висеть вечно на мёртвом соединении;
#   retries              — пережить кратковременные сетевые сбои.
YDL_OPTS = {
    "format": "bestaudio/best",
    "postprocessors": [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "192",
    }],
    "outtmpl": "downloads/%(id)s.%(ext)s",
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "max_filesize": MAX_LINK_SIZE,
    "noplaylist": True,
    "socket_timeout": 30,
    "retries": 3,
}

# Слова-маркеры из сообщений yt-dlp об отказе скачивать слишком большой файл
# (downloader/http.py: "File is larger than max-filesize ... Aborting.").
TOO_LARGE_MARKERS = ("max-filesize", "max_filesize", "larger than", "too large")


class LinkError(RuntimeError):
    """Ссылка недоступна/битая, это не медиа или загрузка не удалась."""


class LinkTooLargeError(LinkError):
    """Медиа по ссылке больше допустимого лимита (2000 МБ)."""


def build_ydl_opts(outtmpl: str | None = None) -> dict:
    """Копия YDL_OPTS для передачи в YoutubeDL.

    outtmpl позволяет изолировать задачу в своей папке (downloads/<uuid>/%(id)s.%(ext)s),
    чтобы параллельные загрузки не затирали файлы друг друга.
    """
    opts = dict(YDL_OPTS)
    opts["postprocessors"] = [dict(pp) for pp in YDL_OPTS["postprocessors"]]
    if outtmpl:
        opts["outtmpl"] = outtmpl
    return opts


def is_too_large_error(details: str) -> bool:
    """True, если yt-dlp прервал загрузку из-за превышения лимита размера."""
    low = (details or "").lower()
    return any(marker in low for marker in TOO_LARGE_MARKERS)


def first_entry(info: dict | None) -> dict | None:
    """Первый осмысленный элемент (для плейлиста/сборника) или сам info."""
    if not info:
        return info
    entries = [entry for entry in (info.get("entries") or []) if entry]
    return entries[0] if entries else info


def expected_size(info: dict | None) -> int | None:
    """Ожидаемый размер выбранного формата в байтах или None, если неизвестен.

    Смотрим filesize -> filesize_approx, а если аудио собирается из нескольких
    дорожек — суммируем requested_formats. None означает «заранее не знаем», тогда
    решает уже max_filesize на этапе скачивания.
    """
    if not info:
        return None

    for key in ("filesize", "filesize_approx"):
        value = info.get(key)
        if value:
            return int(value)

    total = 0
    formats = info.get("requested_formats") or []
    for fmt in formats:
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        if not size:
            return None
        total += int(size)
    return total or None



def pick_downloaded_file(info: dict, out_dir: Path) -> Path | None:
    """Путь к готовому mp3 после постобработки или None.

    Порядок: filepath из requested_downloads (yt-dlp обновляет его после ffmpeg-шага) ->
    filepath/_filename из info -> downloads/<id>.mp3 -> единственный mp3 в папке задачи.
    """
    candidates: list[str] = []
    requested = info.get("requested_downloads") or []
    if requested and isinstance(requested[0], dict):
        candidates.append(requested[0].get("filepath") or "")
    candidates.append(info.get("filepath") or "")
    candidates.append(info.get("_filename") or "")

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)

    video_id = info.get("id")
    if video_id:
        by_id = out_dir / f"{video_id}.mp3"
        if by_id.is_file():
            return by_id

    mp3_files = sorted(out_dir.glob("*.mp3"))  # последний шанс: один трек в папке задачи
    return mp3_files[0] if mp3_files else None


def make_ydl(opts: dict):
    """Создаёт YoutubeDL, который замечает сообщение yt-dlp об отказе по max-filesize.

    Почему так: в quiet-режиме yt-dlp не бросает исключение из-за превышения размера —
    он печатает «File is larger than max-filesize ... Aborting» и возвращает info БЕЗ
    файла на диске. Сообщение доходит до to_screen (просто не печатается), поэтому
    перехватываем его и помечаем ydl.too_large_seen, чтобы отдать LinkTooLargeError.
    Вынесено в функцию, чтобы тесты подменяли её одной подменой.
    """
    ydl = yt_dlp.YoutubeDL(opts)
    ydl.too_large_seen = False
    original_to_screen = ydl.to_screen

    def to_screen(message, *args, **kwargs):
        if is_too_large_error(str(message)):
            ydl.too_large_seen = True
        return original_to_screen(message, *args, **kwargs)

    ydl.to_screen = to_screen
    return ydl


def download_sync(url: str, out_dir: Path) -> tuple[Path, str]:
    """Синхронная часть: yt-dlp качает и конвертирует в mp3. Возвращает (путь, title).

    Вызывать только через download_audio_by_url: напрямую дёрнуть эту функцию из
    корутины нельзя — она блокирует поток.
    """
    if yt_dlp is None:
        raise LinkError(LogMessages.YTDLP_MISSING)

    out_dir.mkdir(parents=True, exist_ok=True)
    opts = build_ydl_opts(str(out_dir / "%(id)s.%(ext)s"))

    with make_ydl(opts) as ydl:
        # Предварительная проверка размера: yt-dlp при превышении max_filesize молча
        # пропускает формат и не создаёт файл, поэтому лучше узнать заранее (заодно
        # экономим трафик). Если размер неизвестен — решает max_filesize при загрузке.
        probe_size = None
        try:
            probe_size = expected_size(first_entry(ydl.extract_info(url, download=False)))
        except Exception:
            probe_size = None  # метаданные не отдались — не мешаем основной попытке

        if probe_size and probe_size > MAX_LINK_SIZE:
            raise LinkTooLargeError(
                f"размер {probe_size} байт > лимита {MAX_LINK_SIZE} байт"
            )

        info = first_entry(ydl.extract_info(url, download=True))
        refused_by_size = bool(getattr(ydl, "too_large_seen", False))

    if refused_by_size:
        raise LinkTooLargeError("yt-dlp отказался скачивать: файл больше max_filesize")

    if not info:
        raise LinkError("yt-dlp не вернул метаданные")

    path = pick_downloaded_file(info, out_dir)
    if path is None:
        raise LinkError("аудиофайл не найден после обработки")

    title = (info.get("title") or "").strip()
    return path, title


async def download_audio_by_url(
    url: str, out_dir: Path | str | None = None, timeout: int = LINK_TIMEOUT
) -> tuple[Path, str]:
    """Скачивает аудио по ссылке, не блокируя event loop. Возвращает (путь_к_mp3, title).

    Бросает LinkTooLargeError, если медиа больше MAX_LINK_SIZE, и LinkError при любой
    другой проблеме (недоступная ссылка, нет ffmpeg, таймаут).
    """
    target = Path(out_dir) if out_dir is not None else DOWNLOAD_DIR

    try:
        path, title = await asyncio.wait_for(
            asyncio.to_thread(download_sync, url, target), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        raise LinkError(f"таймаут загрузки ({timeout} сек)") from exc
    except LinkError:
        raise  # LinkTooLargeError — тоже LinkError, поэтому поднимаем как есть
    except Exception as exc:  # ошибки сети, парсеров, ffmpeg-постобработки
        details = str(exc)
        if is_too_large_error(details):
            raise LinkTooLargeError(details[:300]) from exc
        raise LinkError(details[:300]) from exc

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise LinkError("не удалось прочитать скачанный файл") from exc

    if size > MAX_LINK_SIZE:
        raise LinkTooLargeError(f"размер {size} байт > лимита {MAX_LINK_SIZE} байт")

    return path, title
