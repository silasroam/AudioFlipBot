"""Общая настройка тестов.

Проект — плоский набор модулей (bot.py, converter.py, database.py, downloader.py,
strings.py) без пакета, поэтому добавляем корень проекта в sys.path, чтобы тесты
могли делать `import bot`, `import downloader` и т.д.
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
