"""Telegram-бот-конвертер на Pyrogram (MTProto): принимает файлы до 2000 МБ.

Почему Pyrogram, а не aiogram: Bot API не отдаёт боту файлы больше 20 МБ и не принимает
результаты больше 50 МБ. MTProto (клиентский API, под которым работает Pyrogram) этих
ограничений не имеет — лимит 2000 МБ.
Скачивание идёт chunk-by-chunk сразу на диск (Pyrogram пишет каждый полученный чанк в
открытый файл), поэтому оперативная память остаётся низкой — это важно для Render 512 МБ.

Запуск:
    pip install -r requirements.txt
    # секреты берутся из окружения или из скрытых файлов рядом с bot.py:
    #   .api_id, .api_hash, .bot_token  (они в .gitignore)
    # на сервере удобнее переменные окружения: API_ID, API_HASH, BOT_TOKEN
    python bot.py

Render Web Service (Free): помимо бота поднимается крошечный aiohttp-сервер на
0.0.0.0:$PORT (по умолчанию 10000) с маршрутами / и /health -> 200 OK — Render
сканирует открытые порты и без них убивает сервис с "No open ports detected".
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

from aiohttp import web
from pyrogram import Client, enums, filters, idle
from pyrogram.errors import (
    AuthKeyDuplicated,
    AuthKeyInvalid,
    AuthKeyUnregistered,
    FileReferenceExpired,
    FileReferenceInvalid,
    FloodWait,
    MessageNotModified,
    RPCError,
    SessionRevoked,
)
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from converter import ConvertError, convert_audio, ffmpeg_available, output_ext
from database import get_and_increment_file_name, init_db
from strings import LogMessages, UI

# ============================== ДОСТУПЫ ==============================
# В репозитории секретов нет вообще: значения читаются из окружения или из скрытых файлов
# рядом с bot.py (.api_id / .api_hash / .bot_token — они перечислены в .gitignore).
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ===== Логотип для команды /start =====
# Картинка кладётся рядом с bot.py. Файл ищем без учёта регистра: Linux регистрозависим,
# поэтому сработает и "Logostart.png", и "logostart.png". Нет файла — /start отдаст текст.
LOGO_NAME = "Logostart.png"
LOGO_ERR_MISSING = "⚠️ {name} не найден рядом с bot.py — /start покажет только текст"
LOGO_ERR_SEND = "⚠️ Не удалось отправить логотип ({error}) — отправляю текстом"


def find_logo() -> Path | None:
    """Путь к логотипу рядом с bot.py (регистр имени не важен) или None."""
    direct = BASE_DIR / LOGO_NAME
    if direct.is_file():
        return direct

    target = LOGO_NAME.lower()
    try:
        for candidate in BASE_DIR.iterdir():
            if candidate.is_file() and candidate.name.lower() == target:
                return candidate
    except OSError:
        pass
    return None


HIDDEN_FILES = {"API_ID": ".api_id", "API_HASH": ".api_hash", "BOT_TOKEN": ".bot_token"}


def credential(name: str) -> str:
    """Секрет из переменной окружения, иначе из скрытого файла, иначе пустая строка.

    Локально: положи значения в файлы .api_id, .api_hash, .bot_token (по одному значению).
    На Render: задай API_ID, API_HASH, BOT_TOKEN в Environment Variables — кода менять не надо.
    """
    env_value = os.getenv(name, "").strip()
    if env_value:
        return env_value

    hidden = BASE_DIR / HIDDEN_FILES[name]
    if hidden.exists():
        return hidden.read_text(encoding="utf-8").strip()

    return ""


API_ID_TEXT = credential("API_ID")
API_ID = int(API_ID_TEXT) if API_ID_TEXT.isdigit() else 0
API_HASH = credential("API_HASH")
BOT_TOKEN = credential("BOT_TOKEN")

# ============================== НАСТРОЙКИ ==============================
SESSION_NAME = "audio_converter_bot"  # имя файла сессии: <SESSION_NAME>.session
MAX_FILE_SIZE = 2000 * 1024 * 1024  # предел MTProto: больше — не скачаем, проверяем заранее
PROGRESS_STEP = 5.0  # как часто обновлять текст прогресса (секунды)

# --- Мини-HTTP-сервер для Render Web Service (Free) ---
# Render сканирует открытые порты и без них рушит сервис с "No open ports detected".
# Слушаем 0.0.0.0:$PORT (Render сам подставляет PORT; 10000 — дефолт для локального запуска).
HEALTH_HOST = "0.0.0.0"
DEFAULT_PORT = 10000

FORMATS = (
    ("mp3", UI.BUTTON_MP3),
    ("voice", UI.BUTTON_OGG),
    ("wav", UI.BUTTON_WAV),
    ("m4a", UI.BUTTON_M4A),
)
FORMAT_CODES = {code for code, _ in FORMATS}

# Запасные имена, если исходное имя файла неизвестно (или БД недоступна).
# В обычном случае имя строится из оригинального имени + счётчика пользователя (database.py):
# track.mp3 -> track_1.mp3 -> track_2.mp3...
# ВАЖНО: file_name применяется только к отправке аудио/видео/документов; у голосовых
# (send_voice / reply_voice) такого параметра нет — см. send_result().
OUTPUT_NAMES = {
    "mp3": "converted_audio.mp3",
    "m4a": "audio_track.m4a",
    "wav": "audio_track.wav",
    "voice": "voice_message.ogg",
    "mp4": "video_output.mp4",
}

# Каким методом Telegram отдаём результат. Имя из БД подставляется везде, где это возможно:
#   audio    -> reply_audio   (file_name + title)
#   video    -> reply_video   (file_name)
#   document -> reply_document(file_name)
#   voice    -> reply_voice   (file_name НЕ поддерживается — имя задаёт Telegram)
# Поэтому у голосового кнопка есть, а своего file_name нет: счётчик всё равно растёт.
SEND_KINDS = {
    "mp3": "audio",
    "m4a": "audio",
    "wav": "audio",
    "voice": "voice",
    "mp4": "video",
}
# Формат вне таблицы (например будущий «файл/документ») отдаём документом — тоже с именем из БД.
DEFAULT_SEND_KIND = "document"

# Задачи держим в памяти процесса: file_id в callback_data не влезает (лимит 64 байта), а сами
# файлы в БД не храним — только счётчики (users_files). Ключ — id чата (= id пользователя в личке).
PENDING: dict[int, dict] = {}

# Клиента создаём внутри работающего event loop функцией create_app() — см. пояснение там.


# ============================== ВСПОМОГАТЕЛЬНОЕ ==============================


def format_keyboard() -> InlineKeyboardMarkup:
    """Инлайн-кнопки выбора формата (по 2 в ряд)."""
    buttons = [
        InlineKeyboardButton(text=title, callback_data=f"fmt:{code}")
        for code, title in FORMATS
    ]
    return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])


def start_keyboard() -> ReplyKeyboardMarkup:
    """Закреплённая нижняя кнопка «Старт» — всегда видна под полем ввода.

    is_persistent=True — Telegram держит клавиатуру внизу постоянно (не сворачивается);
    resize_keyboard=True — компактная кнопка, а не на всю высоту экрана.
    """
    return ReplyKeyboardMarkup(
        [[KeyboardButton(UI.BUTTON_START)]],
        is_persistent=True,
        resize_keyboard=True,
    )


def pick_media(message: Message):
    """(имя_файла, размер_в_байтах, тип_медиа) для поддерживаемого медиа или None.

    Тип медиа задаёт базовое слово для имени результата, если у файла нет осмысленного имени
    (video / audio / voice / document — см. database.clean_stem). Работает одинаково для
    видео, аудио, документов и голосовых.
    """
    for attr, kind, fallback_ext in (
        ("audio", "audio", "mp3"),
        ("voice", "voice", "oga"),
        ("video", "video", "mp4"),
        ("video_note", "video", "mp4"),  # «кружок» — это тоже видео
        ("animation", "video", "mp4"),  # GIF-анимация — тоже видео
    ):
        media = getattr(message, attr, None)
        if media is not None:
            name = getattr(media, "file_name", None) or f"{kind}.{fallback_ext}"
            return name, getattr(media, "file_size", 0) or 0, kind

    document = getattr(message, "document", None)
    if document is not None and (document.mime_type or "").startswith(("audio/", "video/")):
        # Аудио/видео, присланное как файл: если имени нет — базовое слово "document".
        return document.file_name or "document.bin", document.file_size or 0, "document"
    return None


def human_size(num_bytes: int) -> str:
    """Байты в удобочитаемый вид: 1.5 ГБ, 240.3 МБ и т.п."""
    size = float(num_bytes or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def output_filename(fmt: str) -> str:
    """Понятное имя выходного файла для формата: mp3 -> converted_audio.mp3 и т.д.

    Для формата вне OUTPUT_NAMES подстраховываемся именем из converter (converted.<ext>).
    """
    named = OUTPUT_NAMES.get(fmt)
    return named if named else f"converted.{output_ext(fmt)}"


async def safe_edit(status: Message, text: str) -> None:
    """Правит текст статуса, глотая 'message is not modified' и флуд-лимиты."""
    try:
        await status.edit_text(text)
    except MessageNotModified:
        pass
    except FloodWait as exc:
        await asyncio.sleep(exc.value)
    except RPCError:
        pass


def make_progress(status: Message, label: str):
    """Фабрика колбэка прогресса: Pyrogram зовёт его как progress(current, total, *args)."""
    state = {"start": time.monotonic(), "last": 0.0}

    async def progress(current: int, total: int) -> None:
        now = time.monotonic()
        if total and current < total and now - state["last"] < PROGRESS_STEP:
            return
        state["last"] = now

        total = total or current
        percent = current * 100 / total if total else 100.0
        elapsed = max(now - state["start"], 0.001)
        speed = current / elapsed
        left = int((total - current) / speed) if speed > 0 else 0

        await safe_edit(
            status,
            UI.progress(
                label=label,
                percent=int(percent),
                current=human_size(current),
                total=human_size(total),
                speed=human_size(int(speed)),
                left=left,
            ),
        )

    return progress


async def chat_action(client: Client, chat_id: int, action) -> None:
    """Показывает «печатает/отправляет»; часть действий ботам недоступна — не падаем."""
    try:
        await client.send_chat_action(chat_id, action)
    except (RPCError, AttributeError):
        pass


# ============================== ХЕНДЛЕРЫ ==============================
# Хендлеры регистрируются вручную в create_app(): порядок важен, а декораторы в этом
# форке работают только при создании клиента уже внутри работающего event loop.


async def on_start(client: Client, message: Message) -> None:
    """Приветствие: логотип Logostart.png с подписью + закреплённая кнопка «Старт»."""
    logo = find_logo()
    if logo is not None:
        try:
            await message.reply_photo(
                photo=str(logo),
                caption=UI.START,
                parse_mode=enums.ParseMode.MARKDOWN,
                reply_markup=start_keyboard(),
            )
            return
        except RPCError as exc:
            print(LOGO_ERR_SEND.format(error=f"{type(exc).__name__}: {exc}"))

    await message.reply_text(
        UI.START,
        parse_mode=enums.ParseMode.MARKDOWN,
        reply_markup=start_keyboard(),
    )


# В этом форке Pyrogram `filters.text` — это «любой текст» (без аргумента), поэтому точное
# совпадение с подписью нижней кнопки делаем своим фильтром через filters.create.
START_BUTTON_FILTER = filters.create(
    lambda _, __, message: (message.text or "") == UI.BUTTON_START,
    name="StartButton",
)


async def on_start_button(client: Client, message: Message) -> None:
    """Нажатие закреплённой кнопки «Старт» внизу — то же приветствие, что и по /start."""
    await on_start(client, message)


MEDIA_FILTER = filters.incoming & (
    filters.audio
    | filters.voice
    | filters.video
    | filters.video_note
    | filters.document
    | filters.animation
)


async def on_media(client: Client, message: Message) -> None:
    """Приём медиа: кладём задачу в память и показываем кнопки форматов."""
    picked = pick_media(message)
    if picked is None:
        await message.reply_text(UI.NOT_MEDIA, reply_markup=start_keyboard())
        return

    file_name, file_size, media_kind = picked
    if file_size and file_size > MAX_FILE_SIZE:
        await message.reply_text(
            UI.file_too_large(human_size(file_size)), reply_markup=start_keyboard()
        )
        return

    PENDING[message.chat.id] = {
        "message": message,  # сам Message нужен для потокового download()
        "chat_id": message.chat.id,
        "message_id": message.id,
        "original_name": file_name,  # сырое имя (с расширением) — база для нумерации в БД
        "media_kind": media_kind,  # video/audio/voice/document — запасное базовое слово
    }
    size_str = human_size(file_size) if file_size else "размер неизвестен"
    await message.reply_text(
        UI.file_received(size_str),
        reply_markup=format_keyboard(),
    )


async def download_to_disk(client: Client, task: dict, input_path: Path, status: Message) -> None:
    """Качает файл кусками прямо в input_path, без буферизации в оперативке.

    Если file_reference успел протухнуть — перезапрашиваем сообщение и качаем снова.
    """
    progress = make_progress(status, "⏳ Скачиваю")
    try:
        await task["message"].download(file_name=str(input_path), progress=progress)
        return
    except (FileReferenceExpired, FileReferenceInvalid):
        pass

    fresh = await client.get_messages(task["chat_id"], task["message_id"])
    if fresh is None:
        raise ConvertError("сообщение больше недоступно, пришли файл заново")
    task["message"] = fresh
    await fresh.download(file_name=str(input_path), progress=progress)


async def send_result(
    task: dict, fmt: str, output_path: Path, file_name: str, title: str, progress
) -> None:
    """Отправляет готовый файл с именем из БД (file_name) и Title (для аудио).

    Метод выбирается по формату (см. SEND_KINDS), потому что Telegram принимает file_name
    не везде:
      * voice (OGG-голосовое) -> reply_voice БЕЗ file_name/title, иначе падает с
        "TypeError: Message.reply_voice() got an unexpected keyword argument 'file_name'";
      * mp4 -> reply_video с file_name;
      * документы/прочие форматы -> reply_document с file_name;
      * mp3 / m4a / wav -> reply_audio с file_name и title (Title виден в плеере).
    """
    message = task["message"]
    kind = SEND_KINDS.get(fmt, DEFAULT_SEND_KIND)

    if kind == "voice":
        await message.reply_voice(voice=str(output_path), progress=progress)
    elif kind == "video":
        await message.reply_video(
            video=str(output_path),
            file_name=file_name,
            progress=progress,
        )
    elif kind == "document":
        await message.reply_document(
            document=str(output_path),
            file_name=file_name,
            progress=progress,
        )
    else:
        await message.reply_audio(
            audio=str(output_path),
            title=title[:64],  # в плеере будет понятное название, а не "Неизвестен"
            file_name=file_name,
            progress=progress,
        )


async def on_format(client: Client, callback: CallbackQuery) -> None:
    """Скачать -> сконвертировать -> отправить -> удалить временные файлы."""
    data = callback.data or ""
    if not data.startswith("fmt:"):
        await callback.answer(UI.UNKNOWN_BUTTON)
        return

    fmt = data.split(":", 1)[1]
    task = PENDING.pop(callback.from_user.id, None) if callback.from_user else None
    if task is None or fmt not in FORMAT_CODES:
        await callback.answer(UI.BUTTON_EXPIRED, show_alert=True)
        return
    if callback.message is None:
        await callback.answer(UI.MESSAGE_UNAVAILABLE, show_alert=True)
        return
    await callback.answer()

    status = callback.message  # статус показываем прямо в сообщении с кнопками
    chat_id = task["chat_id"]
    ext = output_ext(fmt)
    # Уникальные имена => задачи разных пользователей не пересекаются.
    input_path = DOWNLOAD_DIR / f"{uuid4().hex}_in"
    output_path = DOWNLOAD_DIR / f"{uuid4().hex}_out.{ext}"
    # Имя и Title по счётчику из БД — работает для ВСЕХ типов (аудио, видео, документ,
    # голосовое): 1-й файл -> video.mp3, 2-й -> video_1.mp3, 3-й -> video_2.mp3...
    # База — исходное имя файла, а если его нет или оно авто-сгенерированное — слово по типу медиа.
    media_kind = task.get("media_kind") or "audio"
    source_name = task.get("original_name") or f"{media_kind}.{ext}"
    try:
        result_name, result_title = await get_and_increment_file_name(
            callback.from_user.id, source_name, ext, fallback=media_kind
        )
    except Exception as exc:  # подстраховка: БД ни при каких условиях не роняет конвертацию
        print(LogMessages.HANDLER_NUM_FAIL.format(error_type=type(exc).__name__, error=exc))
        result_name, result_title = output_filename(fmt), Path(output_filename(fmt)).stem

    try:
        try:
            await status.edit_reply_markup(None)  # убираем кнопки, чтобы не жали дважды
        except RPCError:
            pass

        await safe_edit(status, UI.DOWNLOADING)
        await chat_action(client, chat_id, enums.ChatAction.TYPING)
        await download_to_disk(client, task, input_path, status)

        size_in = input_path.stat().st_size if input_path.exists() else 0
        await safe_edit(status, UI.converting(ext, human_size(size_in)))
        await chat_action(client, chat_id, enums.ChatAction.TYPING)
        await convert_audio(input_path, output_path, fmt)

        await safe_edit(status, UI.SENDING)
        upload_progress = make_progress(status, "📤 Отправляю")
        if fmt == "voice":
            action = enums.ChatAction.UPLOAD_AUDIO
        elif fmt == "mp4":
            action = enums.ChatAction.UPLOAD_VIDEO
        else:
            action = enums.ChatAction.UPLOAD_DOCUMENT
        await chat_action(client, chat_id, action)
        # Раздельная отправка: voice -> без file_name, audio/video -> с именем из БД и Title.
        await send_result(task, fmt, output_path, result_name, result_title, upload_progress)

        try:
            await status.delete()
        except RPCError:
            pass

    except ConvertError as exc:
        await safe_edit(status, UI.ERR_CONVERT.format(details=str(exc)[:300]))
    except ValueError:
        # например "Can't upload files bigger than 2000 MiB"
        await safe_edit(status, UI.ERR_TOO_LARGE_TO_SEND)
    except FloodWait as exc:
        await safe_edit(status, UI.ERR_FLOOD.format(seconds=exc.value))
    except RPCError as exc:
        await safe_edit(status, UI.ERR_TELEGRAM_RPC.format(details=str(exc)[:200]))
    except Exception as exc:  # сеть, диск, лимиты — бот не должен падать
        await safe_edit(status, UI.ERR_GENERIC.format(details=str(exc)[:200]))
    finally:
        # КРИТИЧНО: временные файлы удаляем при любом исходе, чтобы не забить диск.
        for path in (input_path, output_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


async def on_other(client: Client, message: Message) -> None:
    """Всё, что не медиа и не команда: короткая подсказка."""
    try:
        await message.reply_text(UI.OTHER, reply_markup=start_keyboard())
    except RPCError:
        pass


# ============================== HTTP-СЕРВЕР ДЛЯ RENDER ==============================
# Render Web Service (Free) обязан видеть открытый порт, иначе процесс убивается
# с ошибкой "No open ports detected". Боту HTTP не нужен — это просто "заглушка",
# которая отвечает 200 OK на / и /health. На конвертацию, PENDING и ffmpeg не влияет.


def health_port() -> int:
    """Порт из $PORT (Render), иначе 10000. Некорректное значение — откат к дефолту."""
    raw = os.getenv("PORT", "").strip()
    try:
        port = int(raw)
    except ValueError:
        if raw:
            print(LogMessages.INVALID_PORT.format(value=raw, default_port=DEFAULT_PORT))
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


async def start_health_server() -> web.AppRunner:
    """Поднимает aiohttp-сервер на 0.0.0.0:$PORT с маршрутами / и /health.

    Возвращает AppRunner — его нужно закрыть (runner.cleanup()) при остановке,
    чтобы освободить порт. Запускать до старта Pyrogram-клиента.
    """
    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="OK", status=200)

    health_app = web.Application()
    health_app.router.add_get("/", health)
    health_app.router.add_get("/health", health)

    runner = web.AppRunner(health_app)
    await runner.setup()
    port = health_port()
    site = web.TCPSite(runner, host=HEALTH_HOST, port=port)
    await site.start()
    print(LogMessages.HEALTH_RUNNING.format(port=port))
    return runner


# ============================== ЗАПУСК ==============================


def create_app() -> Client:
    """Создаёт клиента и регистрирует хендлеры. ВЫЗЫВАТЬ ТОЛЬКО внутри работающего event loop.

    Почему так: форк регистрирует хендлеры асинхронно (`client.loop.create_task`), а
    `Client.loop` — ленивое свойство, кеширующее первый полученный loop. Если создать
    клиента на импорте модуля (до старта loop), он привяжется к неработающему loop:
    хендлеры не подключатся, а сессии упадут с "attached to a different loop".
    Регистрация явная: первый подходящий хендлер в группе выигрывает, поэтому order важен.
    """
    client = Client(
        name=SESSION_NAME,
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        workdir=str(BASE_DIR),
    )
    client.add_handler(MessageHandler(on_start, filters.incoming & filters.command("start")))
    client.add_handler(
        MessageHandler(on_start_button, filters.incoming & START_BUTTON_FILTER)
    )
    client.add_handler(MessageHandler(on_media, MEDIA_FILTER))
    client.add_handler(MessageHandler(on_other, filters.incoming))
    client.add_handler(CallbackQueryHandler(on_format))
    return client


SESSION_ERRORS = (SessionRevoked, AuthKeyUnregistered, AuthKeyDuplicated, AuthKeyInvalid)


async def start_client(client: Client) -> Client:
    """Стартует клиента, а если сессия мертва — пересоздаёт её автоматически.

    Сценарий: токен бота сменили у @BotFather (/revoke) — Telegram инвалидирует все сессии,
    и сохранённый <SESSION_NAME>.session перестаёт пускать (401 SESSION_REVOKED), даже если
    в коде уже новый токен. В этом случае файлы сессии удаляются, и бот логинится заново
    по BOT_TOKEN: руками чистить ничего не нужно.
    """
    try:
        await client.start()
        return client
    except SESSION_ERRORS as exc:
        print(LogMessages.SESSION_EXPIRED.format(error_type=type(exc).__name__))

    try:
        await client.stop()
    except Exception:
        pass

    for session_file in BASE_DIR.glob(f"{SESSION_NAME}.session*"):
        try:
            session_file.unlink(missing_ok=True)
        except OSError:
            pass

    fresh = create_app()
    await fresh.start()
    print(LogMessages.REAUTH_SUCCESS)
    return fresh


async def main() -> None:
    missing = [
        name
        for name, value in (("API_ID", API_ID), ("API_HASH", API_HASH), ("BOT_TOKEN", BOT_TOKEN))
        if not value
    ]
    if missing:
        sys.exit(
            "Не заданы: " + ", ".join(missing) + ". Задай их переменными окружения "
            "или файлами .api_id / .api_hash / .bot_token рядом с bot.py"
        )
    if not ffmpeg_available():
        sys.exit("ffmpeg не найден. Установи: sudo apt install ffmpeg | brew install ffmpeg")

    for stale in DOWNLOAD_DIR.iterdir():  # подчищаем хвосты прошлых запусков
        if stale.is_file():
            stale.unlink(missing_ok=True)

    # Создаём/открываем SQLite (bot_database.db) при старте: таблица users_files
    # появится автоматически. Ошибка БД не мешает запуску бота (см. database.py).
    await init_db()

    if find_logo() is None:  # подсказка: без картинки /start отдаст только текст
        print(LOGO_ERR_MISSING.format(name=LOGO_NAME))

    app = create_app()
    await asyncio.sleep(0.05)  # даём форку фактически зарегистрировать хендлеры
    registered = {
        group: [handler.callback.__name__ for handler in handlers]
        for group, handlers in app.dispatcher.groups.items()
    }
    if not registered:
        sys.exit("Хендлеры не зарегистрировались: Client создан вне работающего event loop")
    print(LogMessages.HANDLERS_LOADED.format(handlers=registered))

    # Render Web Service (Free) должен видеть открытый порт — поднимаем HTTP-заглушку
    # до старта Pyrogram-клиента. Падение веб-сервера не должно ронять бота.
    health_runner: web.AppRunner | None = None
    try:
        health_runner = await start_health_server()
    except OSError as exc:  # порт занят или недоступен
        print(LogMessages.HEALTH_FAIL.format(error=exc))

    app = await start_client(app)  # при мёртвой сессии вернёт переавторизованного клиента
    me = await app.get_me()
    print(
        LogMessages.BOT_STARTED.format(
            username=me.username, limit=human_size(MAX_FILE_SIZE)
        )
    )
    try:
        await idle()  # держим процесс живым и слушаем апдейты
    finally:
        await app.stop()
        if health_runner is not None:
            await health_runner.cleanup()  # освобождаем порт при остановке
        print(LogMessages.STOPPED_IDLE)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    logging.getLogger("pyrogram").setLevel(logging.WARNING)  # прячем INFO-шум клиента
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(LogMessages.STOPPED_CTRL_C)
