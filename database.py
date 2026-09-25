"""SQLite-учёт обработанных файлов пользователя (aiosqlite).

Отдельный модуль, как converter.py: bot.py просто импортирует функции. Все операции
асинхронные и НИКОГДА не выбрасывают исключения наружу — при любой проблеме с БД
возвращается безопасный результат, а конвертация и отправка продолжаются.

Таблица:
    users_files(user_id INTEGER PRIMARY KEY, file_count INTEGER DEFAULT 0)
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

try:  # бот должен работать даже если aiosqlite вдруг не установлен
    import aiosqlite
except ImportError:  # pragma: no cover - зависит от окружения
    aiosqlite = None

# База лежит рядом с кодом и создаётся автоматически при старте (init_db).
DB_PATH = Path(__file__).resolve().parent / "bot_database.db"

# Медиа-расширения, которые срезаем при вычислении базового имени файла.
MEDIA_EXTS = {
    ".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".aac", ".flac", ".wma",
    ".aiff", ".aif", ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".3gp",
    ".mpeg", ".mpg", ".bin",
}

# Сериализуем read-modify-write: два файла одного пользователя не получат один номер.
_DB_LOCK = asyncio.Lock()


# Имена, которые клиент/Telegram подставляет сам: "video_2026-09-25_12-30-00.mp4",
# "audio_2024-01-01_10-00-00.mp3", просто набор цифр или hex-мусор. Осмысленным именем
# это не считаем — вместо него берём базовое слово по типу медиа (video/audio/document/voice).
GENERIC_NAME_RE = re.compile(
    r"^(?:video|audio|voice|video_note|animation|document|photo|sticker|gif)"
    r"_\d{4}-\d{2}-\d{2}[_ ]\d{2}-\d{2}-\d{2}(?:[_ ]\d+)?$"  # <type>_2026-09-25_12-30-00
    r"|^\d{6,}$"  # только цифры
    r"|^[0-9a-f]{16,40}$",  # hex / uuid-подобный мусор
    re.IGNORECASE,
)

# Управляющие символы и разделители пути: имя уходит в file_name (метаданные Telegram),
# а не в файловую систему, поэтому достаточно не пускать в него мусор.
UNSAFE_NAME_RE = re.compile(r"[\x00-\x1f\x7f/\\]+")


def is_generic_name(name: str) -> bool:
    """True для авто-сгенерированных имён (video_2026-09-25_12-30-00, набор цифр/hex)."""
    return bool(GENERIC_NAME_RE.match((name or "").strip()))


def clean_stem(original_filename: str | None, fallback: str = "audio") -> str:
    """Базовое имя без расширения.

    Пустое, небезопасное или авто-сгенерированное имя заменяется на `fallback` — базовое
    слово по типу медиа: video / audio / document / voice.
    """
    fallback = (fallback or "").strip() or "audio"
    name = (original_filename or "").strip()
    if name:
        suffix = Path(name).suffix.lower()
        if suffix in MEDIA_EXTS:  # отрезаем только известные медиа-расширения
            name = name[: -len(suffix)]
        name = UNSAFE_NAME_RE.sub("_", name).strip().strip(".")

    if not name or is_generic_name(name):
        return fallback
    return name


async def init_db(db_path: Path | str = DB_PATH) -> None:
    """Создаёт таблицу users_files, если её ещё нет. Ошибки только логируются."""
    if aiosqlite is None:
        print("⚠️ aiosqlite не установлен — нумерация файлов работать не будет")
        return

    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users_files (
                    user_id    INTEGER PRIMARY KEY,
                    file_count INTEGER DEFAULT 0
                )
                """
            )
            await db.commit()
        print(f"🗄 База данных готова: {db_path}")
    except Exception as exc:  # БД не должна мешать запуску бота
        print(
            f"⚠️ Не удалось инициализировать БД ({type(exc).__name__}: {exc}) — "
            f"нумерация файлов будет без счётчика"
        )


async def get_and_increment_file_name(
    user_id: int,
    original_filename: str | None,
    extension: str,
    fallback: str = "audio",
) -> tuple[str, str]:
    """Возвращает (имя файла с суффиксом, красивый Title) и увеличивает счётчик.

    Работает для любых типов: `original_filename` — исходное имя файла, `fallback` — базовое
    слово по типу медиа (video/audio/document/voice) на случай, если имени нет или оно
    авто-сгенерированное (см. clean_stem).

    Правила нумерации (file_count до инкремента):
        0 -> track.mp3
        1 -> track_1.mp3
        2 -> track_2.mp3
        ...

    При недоступной БД отдаём имя без суффикса (нумерация не соврёт, конвертация не упадёт).
    """
    clean_name = clean_stem(original_filename, fallback)
    ext = (extension or "").lstrip(".")

    if aiosqlite is None:
        stem = clean_name
        return (f"{stem}.{ext}" if ext else stem), stem

    try:
        async with _DB_LOCK:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT file_count FROM users_files WHERE user_id = ?", (user_id,)
                ) as cursor:
                    row = await cursor.fetchone()

                if row is None:
                    count = 0
                    await db.execute(
                        "INSERT INTO users_files (user_id, file_count) VALUES (?, 1)",
                        (user_id,),
                    )
                else:
                    count = int(row[0])
                    await db.execute(
                        "UPDATE users_files SET file_count = file_count + 1 WHERE user_id = ?",
                        (user_id,),
                    )
                await db.commit()
    except Exception as exc:  # сеть/диск/блокировка — не роняем конвертацию
        print(f"⚠️ БД недоступна ({type(exc).__name__}: {exc}) — имя без нумерации")
        stem = clean_name
        return (f"{stem}.{ext}" if ext else stem), stem

    suffix = "" if count == 0 else f"_{count}"
    stem = f"{clean_name}{suffix}"
    return (f"{stem}.{ext}" if ext else stem), stem
