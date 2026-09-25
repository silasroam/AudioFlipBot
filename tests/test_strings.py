"""Проверяем, что тексты про ссылки добавлены в UI ровно в требуемом виде."""

from strings import LogMessages, UI


def test_link_processing_text():
    assert UI.LINK_PROCESSING == "⏳ Извлекаю аудио по ссылке..."


def test_link_failed_text():
    assert UI.ERR_LINK_FAILED == (
        "❌ Не удалось скачать аудио по этой ссылке. "
        "Проверьте адрес или доступность медиа."
    )


def test_link_too_large_text():
    assert UI.ERR_LINK_TOO_LARGE == (
        "❌ Размер аудио по ссылке превышает допустимый лимит (2000 МБ)."
    )


def test_start_text_mentions_links():
    assert "Скачивание аудио по ссылке" in UI.START


def test_link_log_message_format():
    rendered = LogMessages.LINK_FAIL.format(error="boom")
    assert "boom" in rendered
