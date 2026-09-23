# fileMORDAinh

Локальная медиатека на Flask для видео, аудио и обычных файлов.

## Требования

- macOS 12+ (Apple Silicon и Intel)
- Python 3.10+
- pip
- Safari, Chrome или Firefox

## Быстрый запуск на macOS

```bash
git clone https://github.com/tranzistrina/fileMORDAinh.git
cd fileMORDAinh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python index.py
```

Откройте http://127.0.0.1:1313

### Запуск после первой установки

```bash
cd fileMORDAinh && source .venv/bin/activate && python index.py
```

### Запуск через macOS-скрипт

```bash
chmod +x run_mac.sh
./run_mac.sh
```

### Другой порт

```bash
PORT=6767 python index.py
```

Приложение слушает только `127.0.0.1`.

## Возможности

- загрузка больших файлов по частям;
- каталогизация видео, аудио и других файлов;
- категории;
- сортировка по просмотрам, длительности и дате;
- автоматическое определение длительности видео;
- JPEG-превью видео через OpenCV;
- скачивание файлов;
- SQLite без отдельного сервера.

## macOS

Проект рассчитан на Apple Silicon (M1–M5) и Intel. OpenCV подключён через `opencv-python-headless`, поэтому GUI OpenCV не нужен.

Если Python не найден:

```bash
python3 --version
brew install python
```

После обновления проекта:

```bash
git pull
source .venv/bin/activate
python -m pip install -r requirements.txt
python index.py
```

`database.db`, файлы из `uploads/`, временные чанки и локальные окружения не должны попадать в Git.
