import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import cv2
from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
THUMB_FOLDER = BASE_DIR / "thumbnails"
TEMP_FOLDER = BASE_DIR / "temp_chunks"
DB = BASE_DIR / "database.db"

for folder in (UPLOAD_FOLDER, THUMB_FOLDER, TEMP_FOLDER):
    folder.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with sqlite3.connect(DB) as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            file_type TEXT NOT NULL,
            views INTEGER DEFAULT 0,
            length REAL,
            filesize INTEGER,
            upload_date TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS file_category (
            file_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            UNIQUE(file_id, category_id)
        )""")
        conn.commit()

init_db()

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def get_file_type(filename):
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext in {"mp4", "avi", "mov", "mkv", "flv", "wmv", "webm"}:
        return "video"
    if ext in {"mp3", "wav", "ogg", "flac", "aac", "m4a"}:
        return "audio"
    return "other"

def get_video_length(path):
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS)
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.release()
    return round(frames / fps, 2) if fps and fps > 0 else None

def generate_thumbnail(path, file_id, time_sec=10):
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return False
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps and fps > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(fps * time_sec)))
    success, frame = cap.read()
    cap.release()
    return bool(cv2.imwrite(str(THUMB_FOLDER / f"{file_id}.jpg"), frame)) if success else False

def safe_upload_name(filename):
    return secure_filename(filename) or "uploaded_file"

def temp_chunk_path(filename, chunk_number):
    return TEMP_FOLDER / f"{filename}.part{chunk_number}"

@app.route("/")
def index():
    sort = request.args.get("sort", "date")
    category = request.args.get("category")
    conn = get_db()
    c = conn.cursor()

    if category:
        query = """SELECT f.* FROM files f JOIN file_category fc ON f.id = fc.file_id WHERE fc.category_id = ?"""
        params = (category,)
    else:
        query, params = "SELECT * FROM files", ()

    if sort == "views":
        query += " ORDER BY views DESC"
    elif sort == "length":
        query += " ORDER BY CASE WHEN length IS NULL THEN 1 ELSE 0 END, length DESC"
    else:
        query += " ORDER BY upload_date DESC"

    files = c.execute(query, params).fetchall()
    categories = c.execute("SELECT * FROM categories ORDER BY name ASC").fetchall()
    file_categories = c.execute("SELECT * FROM file_category").fetchall()
    conn.close()

    return render_template("index.html", files=files, categories=categories, file_categories=file_categories)

@app.route("/add_category", methods=["POST"])
def add_category():
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify({"id": None, "name": name}), 400
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        conn.commit()
        category_id = c.lastrowid
    except sqlite3.IntegrityError:
        row = c.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
        category_id = row["id"] if row else None
    finally:
        conn.close()
    return jsonify({"id": category_id, "name": name})

@app.route("/categories", methods=["GET", "POST"])
def categories_page():
    conn = get_db()
    c = conn.cursor()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            try:
                c.execute("INSERT INTO categories (name) VALUES (?)", (name,))
                conn.commit()
            except sqlite3.IntegrityError:
                pass
    categories = c.execute("SELECT * FROM categories ORDER BY name ASC").fetchall()
    conn.close()
    return render_template("categories.html", categories=categories)

@app.route("/scan_files")
def scan_files():
    conn = get_db()
    c = conn.cursor()
    existing_files = {row["filename"] for row in c.execute("SELECT filename FROM files").fetchall()}
    for file_path in UPLOAD_FOLDER.iterdir():
        if not file_path.is_file() or file_path.name in existing_files:
            continue
        ftype = get_file_type(file_path.name)
        length = get_video_length(file_path) if ftype == "video" else None
        c.execute("""INSERT INTO files (filename, title, description, file_type, length, filesize, upload_date)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                  (file_path.name, file_path.name, "Автоматически добавлено", ftype, length,
                   file_path.stat().st_size, utc_now_iso()))
        file_id = c.lastrowid
        if ftype == "video":
            generate_thumbnail(file_path, file_id, 10)
    conn.commit()
    conn.close()
    return redirect(url_for("index"))

@app.route("/file/<int:file_id>", methods=["GET", "POST"])
def file_page(file_id):
    conn = get_db()
    c = conn.cursor()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "")
        thumb_time = request.form.get("thumb_time")
        selected_categories = request.form.getlist("categories")
        c.execute("UPDATE files SET title=?, description=? WHERE id=?", (title, description, file_id))
        c.execute("DELETE FROM file_category WHERE file_id=?", (file_id,))
        for cat in selected_categories:
            try:
                c.execute("INSERT OR IGNORE INTO file_category(file_id, category_id) VALUES (?, ?)", (file_id, int(cat)))
            except (TypeError, ValueError):
                pass
        if thumb_time and thumb_time.strip():
            row = c.execute("SELECT filename, file_type FROM files WHERE id=?", (file_id,)).fetchone()
            if row and row["file_type"] == "video":
                try:
                    generate_thumbnail(UPLOAD_FOLDER / row["filename"], file_id, max(0.0, float(thumb_time)))
                except ValueError:
                    pass
        conn.commit()
        conn.close()
        return redirect(url_for("file_page", file_id=file_id))

    c.execute("UPDATE files SET views = views + 1 WHERE id=?", (file_id,))
    conn.commit()
    file = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not file:
        conn.close()
        abort(404)
    categories = c.execute("SELECT * FROM categories ORDER BY name ASC").fetchall()
    file_cats = [row["category_id"] for row in c.execute("SELECT category_id FROM file_category WHERE file_id=?", (file_id,)).fetchall()]
    conn.close()
    return render_template("file.html", file=file, categories=categories, file_cats=file_cats)

@app.route("/upload")
def upload_page():
    conn = get_db()
    categories = conn.execute("SELECT * FROM categories ORDER BY name ASC").fetchall()
    conn.close()
    return render_template("upload.html", categories=categories)

@app.route("/upload_chunk", methods=["POST"])
def upload_chunk():
    file = request.files.get("file")
    original_filename = request.form.get("filename", "")
    if file is None or not original_filename:
        return jsonify({"status": "error", "message": "Файл не передан"}), 400
    filename = safe_upload_name(original_filename)
    try:
        chunk_number = int(request.form["chunk"])
        total_chunks = int(request.form["total"])
    except (KeyError, ValueError):
        return jsonify({"status": "error", "message": "Некорректные параметры чанка"}), 400
    if chunk_number < 0 or total_chunks <= 0 or chunk_number >= total_chunks:
        return jsonify({"status": "error", "message": "Некорректный номер чанка"}), 400
    file.save(temp_chunk_path(filename, chunk_number))
    if chunk_number + 1 == total_chunks:
        final_path = UPLOAD_FOLDER / filename
        with final_path.open("wb") as outfile:
            for i in range(total_chunks):
                part_path = temp_chunk_path(filename, i)
                if not part_path.exists():
                    return jsonify({"status": "error", "message": f"Отсутствует чанк {i}"}), 400
                with part_path.open("rb") as infile:
                    outfile.write(infile.read())
                part_path.unlink()
    return jsonify({"status": "ok", "filename": filename})

@app.route("/finalize_upload", methods=["POST"])
def finalize_upload():
    filename = safe_upload_name(request.form.get("filename", ""))
    title = request.form.get("title", "").strip() or filename
    desc = request.form.get("description", "")
    thumb_time = request.form.get("thumb_time")
    selected_categories = request.form.getlist("categories")
    path = UPLOAD_FOLDER / filename
    if not path.is_file():
        return jsonify({"status": "error", "message": "Загруженный файл не найден"}), 400
    ftype = get_file_type(filename)
    length = get_video_length(path) if ftype == "video" else None
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""INSERT INTO files (filename, title, description, file_type, length, filesize, upload_date)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                  (filename, title, desc, ftype, length, path.stat().st_size, utc_now_iso()))
        file_id = c.lastrowid
        for cat in selected_categories:
            try:
                c.execute("INSERT OR IGNORE INTO file_category(file_id, category_id) VALUES (?, ?)", (file_id, int(cat)))
            except (TypeError, ValueError):
                pass
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"status": "error", "message": "Файл с таким именем уже есть в библиотеке"}), 409
    conn.close()
    if ftype == "video" and thumb_time:
        try:
            generate_thumbnail(path, file_id, max(0.0, float(thumb_time)))
        except ValueError:
            pass
    return redirect(url_for("index"))

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route("/download/<path:filename>")
def download_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)

@app.route("/thumbnails/<path:filename>")
def thumb_file(filename):
    return send_from_directory(THUMB_FOLDER, filename)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "1313"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug)
