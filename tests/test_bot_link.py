"""Unit-тесты интеграции ссылок в bot.py: фильтр URL, порядок хендлеров, on_link.

Реальные Telegram/yt-dlp/ffmpeg не используются: подменяются download_audio_by_url,
safe_get_file_suffix и send_result, поэтому проверяется логика самого хендлера —
URL из текста, переименование под имя из БД, лимиты, очистка папки задачи.
"""

from pathlib import Path

import bot
from strings import UI


class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeStatus:
    """Замена сообщения-статуса: пишем исходный текст, правки и факт удаления."""

    def __init__(self, text):
        self.initial = text
        self.text = text
        self.edits = []
        self.deleted = False

    async def edit_text(self, text, **kwargs):
        self.text = text
        self.edits.append(text)

    async def delete(self):
        self.deleted = True


class FakeMessage:
    def __init__(self, text, chat_id=555, user_id=555, **media):
        self.text = text
        self.chat = FakeChat(chat_id)
        self.from_user = FakeUser(user_id)
        self.id = 42
        self.statuses = []
        for name in ("audio", "voice", "video", "video_note", "animation", "document"):
            setattr(self, name, media.get(name))

    async def reply_text(self, text, **kwargs):
        status = FakeStatus(text)
        self.statuses.append(status)
        return status


class FakeClient:
    def __init__(self):
        self.actions = []

    async def send_chat_action(self, chat_id, action):
        self.actions.append((chat_id, action))


# ============================== URL: ФИЛЬТР И РАЗБОР ==============================


def test_extract_url_picks_first_link_and_strips_punctuation():
    assert bot.extract_url("качай https://youtu.be/abc?t=1, спасибо") == "https://youtu.be/abc?t=1"
    assert bot.extract_url("https://example.com/a.") == "https://example.com/a"
    assert bot.extract_url("нет ссылки") is None
    assert bot.extract_url(None) is None


def test_url_filter_matches_plain_text_with_url():
    assert bot.URL_IN_TEXT_FILTER(None, FakeMessage("смотри https://youtu.be/x")) is True
    assert bot.URL_IN_TEXT_FILTER(None, FakeMessage("HTTP://Example.com/x")) is True


def test_url_filter_ignores_plain_text():
    assert bot.URL_IN_TEXT_FILTER(None, FakeMessage("просто текст")) is False
    assert bot.URL_IN_TEXT_FILTER(None, FakeMessage(None)) is False


def test_url_filter_ignores_media_caption_so_files_win():
    """У медиа text=None, ссылка живёт в caption — такой апдейт уходит файловому хендлеру."""
    media_message = FakeMessage(None, audio=object())

    assert bot.URL_IN_TEXT_FILTER(None, media_message) is False
    assert bot.pick_media(media_message) is not None  # on_media его обработает


def test_url_text_message_is_not_treated_as_media():
    assert bot.pick_media(FakeMessage("https://youtu.be/x")) is None


# ============================== ПОРЯДОК ХЕНДЛЕРОВ ==============================


def test_link_handler_registered_after_start_and_before_other():
    names = [callback.__name__ for callback, _ in bot.message_handler_specs()]

    assert names == ["on_start", "on_start_button", "on_link", "on_media", "on_other"]
    assert names.index("on_link") > names.index("on_start_button")
    assert names.index("on_link") < names.index("on_other")


# ============================== ХЕНДЛЕР on_link ==============================


async def test_on_link_success_renames_to_db_name_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DOWNLOAD_DIR", tmp_path)
    recorded: dict = {}

    async def fake_download(url, out_dir):
        job_dir = Path(out_dir)
        job_dir.mkdir(parents=True, exist_ok=True)
        temp_mp3 = job_dir / "vid.mp3"
        temp_mp3.write_bytes(b"fake-mp3")
        recorded["url"] = url
        recorded["job_dir"] = job_dir
        return temp_mp3, "  YouTube Title  "

    async def fake_get_name(user_id, title, *args, **kwargs):
        recorded["user_id"] = user_id
        recorded["title_from_meta"] = title
        return "Track.mp3", "Track"

    async def fake_send(task, fmt, path, file_name, title, progress):
        recorded["fmt"] = fmt
        recorded["sent_path"] = Path(path)
        recorded["sent_name"] = file_name
        recorded["sent_title"] = title
        recorded["sent_exists"] = Path(path).is_file()

    monkeypatch.setattr(bot, "download_audio_by_url", fake_download)
    monkeypatch.setattr(bot, "safe_get_file_suffix", fake_get_name)
    monkeypatch.setattr(bot, "send_result", fake_send)

    client = FakeClient()
    message = FakeMessage("скачай https://youtu.be/abc , пожалуйста")
    await bot.on_link(client, message)

    assert recorded["url"] == "https://youtu.be/abc"  # из текста, без хвостовой запятой
    assert recorded["user_id"] == 555
    assert recorded["title_from_meta"] == "YouTube Title"  # title из метаданных yt-dlp
    assert recorded["fmt"] == "mp3"
    assert recorded["sent_name"] == "Track.mp3"
    assert recorded["sent_title"] == "Track"
    assert recorded["sent_path"].name == "Track.mp3"  # временный файл переименован в имя из БД
    assert recorded["sent_exists"] is True
    # Статус: сначала «извлекаю», затем «отправляю», после отправки сообщение удалено.
    assert message.statuses[0].initial == UI.LINK_PROCESSING
    assert message.statuses[0].edits == [UI.SENDING]
    assert message.statuses[0].deleted is True
    assert client.actions  # показали ChatAction при отправке
    # Папка задачи подчищена в finally.
    assert not recorded["job_dir"].exists()
    assert list(tmp_path.iterdir()) == []


async def test_on_link_reports_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DOWNLOAD_DIR", tmp_path)
    sent: dict = {}

    async def fake_download(url, out_dir):
        raise bot.LinkTooLargeError("3000 > 2000")

    async def fake_send(*args, **kwargs):
        sent["called"] = True

    monkeypatch.setattr(bot, "download_audio_by_url", fake_download)
    monkeypatch.setattr(bot, "send_result", fake_send)

    message = FakeMessage("https://youtu.be/big")
    await bot.on_link(FakeClient(), message)

    assert message.statuses[0].edits == [UI.ERR_LINK_TOO_LARGE]
    assert "called" not in sent
    assert list(tmp_path.iterdir()) == []  # папка задачи всё равно удалена


async def test_on_link_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DOWNLOAD_DIR", tmp_path)

    async def fake_download(url, out_dir):
        raise bot.LinkError("HTTP Error 404")

    monkeypatch.setattr(bot, "download_audio_by_url", fake_download)

    message = FakeMessage("https://youtu.be/missing")
    await bot.on_link(FakeClient(), message)

    assert message.statuses[0].edits == [UI.ERR_LINK_FAILED]
    assert list(tmp_path.iterdir()) == []


async def test_on_link_cleans_up_when_forwarding_fails(tmp_path, monkeypatch):
    """Даже неожиданная ошибка отправки не должна оставлять мусор на диске."""
    monkeypatch.setattr(bot, "DOWNLOAD_DIR", tmp_path)
    recorded: dict = {}

    async def fake_download(url, out_dir):
        job_dir = Path(out_dir)
        job_dir.mkdir(parents=True, exist_ok=True)
        temp_mp3 = job_dir / "vid.mp3"
        temp_mp3.write_bytes(b"fake-mp3")
        recorded["job_dir"] = job_dir
        return temp_mp3, "Title"

    async def fake_get_name(user_id, title, *args, **kwargs):
        return "Title.mp3", "Title"

    async def fake_send(*args, **kwargs):
        raise RuntimeError("upload exploded")

    monkeypatch.setattr(bot, "download_audio_by_url", fake_download)
    monkeypatch.setattr(bot, "safe_get_file_suffix", fake_get_name)
    monkeypatch.setattr(bot, "send_result", fake_send)

    message = FakeMessage("https://youtu.be/x")
    await bot.on_link(FakeClient(), message)

    assert message.statuses[0].edits == [UI.SENDING, UI.ERR_LINK_FAILED]
    assert not recorded["job_dir"].exists()
    assert list(tmp_path.iterdir()) == []

