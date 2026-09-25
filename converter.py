"""Конвертация аудио/видео через системный ffmpeg (asyncio.create_subprocess_exec).

Один модуль без классов-обёрток: словарь настроек форматов + функция конвертации.
"""

import asyncio
import os
import shutil
from pathlib import Path

FFMPEG_BIN = "ffmpeg"
# Таймаут на одну конвертацию. По умолчанию 2 часа: файлы до 2 ГБ кодируются долго.
CONVERT_TIMEOUT = int(os.getenv("FFMPEG_TIMEOUT", "7200"))

# Все форматы: расширение выходного файла + аргументы ffmpeg.
# -vn              -> выкидываем видеодорожку (из видео берём только звук)
# -map_metadata -1 -> не тащим метаданные источника
FORMATS = {
    "mp3": {
        "ext": "mp3",
        "args": ["-vn", "-map_metadata", "-1", "-c:a", "libmp3lame", "-b:a", "192k"],
    },
    "voice": {
        # OGG/Opus — именно такой файл Telegram принимает как голосовое сообщение
        "ext": "ogg",
        "args": [
            "-vn", "-map_metadata", "-1",
            "-c:a", "libopus", "-b:a", "64k", "-vbr", "on",
            "-compression_level", "10", "-application", "voip",
            "-frame_duration", "60", "-ar", "48000", "-ac", "1",
        ],
    },
    "wav": {
        "ext": "wav",
        "args": ["-vn", "-map_metadata", "-1", "-c:a", "pcm_s16le"],
    },
    "m4a": {
        "ext": "m4a",
        "args": [
            "-vn", "-map_metadata", "-1",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        ],
    },
}

# Конвертируем строго по одной задаче за раз (один поток выполнения ffmpeg).
CONVERT_SEMAPHORE = asyncio.Semaphore(1)


class ConvertError(RuntimeError):
    """Любая проблема конвертации: нет ffmpeg, битый вход, ошибка кодека."""


def ffmpeg_available() -> bool:
    """Есть ли ffmpeg в PATH."""
    return shutil.which(FFMPEG_BIN) is not None


def output_ext(fmt: str) -> str:
    """Расширение выходного файла для формата (voice -> ogg)."""
    if fmt not in FORMATS:
        raise ConvertError(f"Неизвестный формат: {fmt}")
    return FORMATS[fmt]["ext"]


def build_command(src: str | Path, dst: str | Path, fmt: str) -> list[str]:
    """Собирает argv для ffmpeg (вынесено отдельно для тестов и отладки)."""
    if fmt not in FORMATS:
        raise ConvertError(f"Неизвестный формат: {fmt}")
    return [
        FFMPEG_BIN,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        *FORMATS[fmt]["args"],
        str(dst),
    ]


async def convert_audio(input_path: str | Path, output_path: str | Path, fmt: str) -> bool:
    """Асинхронно конвертирует файл. True при успехе, ConvertError при неудаче."""
    cmd = build_command(input_path, output_path, fmt)

    async with CONVERT_SEMAPHORE:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=CONVERT_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise ConvertError(
                f"ffmpeg не успел за {CONVERT_TIMEOUT} сек (файл слишком большой?)"
            )

    if process.returncode != 0:
        details = stderr.decode("utf-8", errors="replace").strip().replace("\n", " ")
        raise ConvertError(f"ffmpeg код {process.returncode}: {details[-300:]}")

    result = Path(output_path)
    if not result.exists() or result.stat().st_size == 0:
        raise ConvertError("ffmpeg не создал выходной файл (во входе нет звуковой дорожки?)")

    return True

