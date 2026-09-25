"""Unit-тесты нумерации имён в database.py, включая новую safe_get_file_suffix.

Используется временная SQLite-база (tmp_path), реальный bot_database.db не трогается.
Важно: get_and_increment_file_name берёт сериализующую блокировку из глобального
_DB_LOCK, а Lock в Python привязывается к первому event loop. Поэтому в фикстуре
подменяем и DB_PATH, и _DB_LOCK — каждый тест получает свой свежий Lock.
"""

import asyncio

import pytest

import database


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "_DB_LOCK", asyncio.Lock())
    return db_path


async def test_safe_get_file_suffix_numbers_tracks(temp_db):
    """Title из yt-dlp нумеруется как у файлов: Track.mp3 -> Track_1.mp3 -> Track_2.mp3."""
    await database.init_db(temp_db)

    first = await database.safe_get_file_suffix(101, "Track")
    second = await database.safe_get_file_suffix(101, "Track")
    third = await database.safe_get_file_suffix(101, "Track")

    assert first == ("Track.mp3", "Track")
    assert second == ("Track_1.mp3", "Track_1")
    assert third == ("Track_2.mp3", "Track_2")


async def test_safe_get_file_suffix_counter_is_per_user(temp_db):
    await database.init_db(temp_db)

    user_one = await database.safe_get_file_suffix(1, "Track")
    user_two = await database.safe_get_file_suffix(2, "Track")
    user_one_again = await database.safe_get_file_suffix(1, "Track")

    assert user_one == ("Track.mp3", "Track")
    assert user_two == ("Track.mp3", "Track")  # у второго пользователя свой счётчик
    assert user_one_again == ("Track_1.mp3", "Track_1")


async def test_safe_get_file_suffix_sanitizes_title(temp_db):
    await database.init_db(temp_db)

    # Слэши из названий видео не должны ломать путь к файлу.
    name, title = await database.safe_get_file_suffix(7, "Artist / Song")

    assert name == "Artist _ Song.mp3"
    assert title == "Artist _ Song"


async def test_safe_get_file_suffix_uses_fallback_for_blank_title(temp_db):
    await database.init_db(temp_db)

    assert await database.safe_get_file_suffix(9, "   ") == ("audio.mp3", "audio")
    assert await database.safe_get_file_suffix(9, None) == ("audio_1.mp3", "audio_1")


async def test_safe_get_file_suffix_honours_extension(temp_db):
    await database.init_db(temp_db)

    assert await database.safe_get_file_suffix(3, "Track", "wav") == ("Track.wav", "Track")


async def test_safe_get_file_suffix_without_aiosqlite(monkeypatch):
    """Без драйвера БД имя всё равно формируется (просто без суффикса)."""
    monkeypatch.setattr(database, "aiosqlite", None)

    assert await database.safe_get_file_suffix(5, "Artist / Song") == (
        "Artist _ Song.mp3",
        "Artist _ Song",
    )


async def test_safe_get_file_suffix_never_raises(temp_db, monkeypatch):
    """Любая ошибка внутри уходит в лог, наружу — безопасное имя без суффикса."""
    await database.init_db(temp_db)

    async def boom(*args, **kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setattr(database, "get_and_increment_file_name", boom)

    assert await database.safe_get_file_suffix(11, "Track") == ("Track.mp3", "Track")
