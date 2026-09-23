import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import cv2
from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from waitress import serve
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
THUMB_FOLDER = BASE_DIR / "thumbnails"
TEMP_FOLDER = BASE_DIR / "temp_chunks"
DB = BASE_DIR / "database.db"
SETUP_CODE_FILE = BASE_DIR / ".setup_code"
SESSION_SECRET_FILE = BASE_DIR / ".session_secret"

for folder in (UPLOAD_FOLDER, THUMB_FOLDER, TEMP_FOLDER):
    folder.mkdir(parents=True, exist_ok=True)

def get_or_create_secret():
    env_secret = os.environ.get("SESSION_SECRET")
    if env_secret:
        return env_secret
    if SESSION_SECRET_FILE.exists():
        return SESSION_SECRET_FILE.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    SESSION_SECRET_FILE.write_text(secret, encoding="utf-8")
    return secret

app = Flask(__name__)
app.secret_key = get_or_create_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
)

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
        c.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        conn.commit()

init_db()

def get_setting(key):
    with sqlite3.connect(DB) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None

def set_setting(key, value):
    with sqlite3.connect(DB) as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()

def admin_configured():
    return bool(get_setting("admin_password_hash"))

def get_setup_code():
    env_code = os.environ.get("ADMIN_SETUP_CODE")
    if env_code:
        return env_code
    if not SETUP_CODE_FILE.exists():
        code = secrets.token_urlsafe(18)
        SETUP_CODE_FILE.write_text(code, encoding="utf-8")
        try:
            SETUP_CODE_FILE.chmod(0o600)
        except OSError:
            pass
        print(f"\n[SECURITY] First-run setup code: {code}\n")
    return SETUP_CODE_FILE.read_text(encoding="utf-8").strip()

def consume_setup_code():
    if not os.environ.get("ADMIN_SETUP_CODE") and SETUP_CODE_FILE.exists():
        try:
            SETUP_CODE_FILE.unlink()
        except OSError:
            pass

if not admin_configured():
    get_setup_code()

def is_admin():
    return bool(session.get("is_admin"))

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not admin_configured():
            return redirect(url_for("setup"))
        if not is_admin():
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped

@app.before_request
def first_run_guard():
    if admin_configured():
        return None
    allowed = {"setup", "static"}
    if request.endpoint in allowed:
        return None
    return redirect(url_for("setup"))

@app.context_processor
def auth_context():
    return {"logged_in": is_admin(), "admin_configured": admin_configured()}

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

def is_private_file(file_id):
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            """SELECT 1
               FROM file_category fc
               JOIN categories c ON c.id = fc.category_id
               WHERE fc.file_id = ? AND LOWER(TRIM(c.name)) = 'privat'
               LIMIT 1""",
            (file_id,),
        ).fetchone()
    return row is not None

def file_id_by_filename(filename):
    with sqlite3.connect(DB) as conn:
        row = conn.execute("SELECT id FROM files WHERE filename = ?", (filename,)).fetchone()
    return row[0] if row else None

def private_file_by_filename(filename):
    file_id = file_id_by_filename(filename)
    return file_id is not None and is_private_file(file_id)

def temp_chunk_path(filename, chunk_number):
    return TEMP_FOLDER / f"{filename}.part{chunk_number}"

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if admin_configured():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        code = request.form.get("setup_code", "")
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        valid_code = hmac.compare_digest(code, get_setup_code())
        if not valid_code:
            error = "Неверный код первичной настройки."
        elif len(password) < 8:
            error = "Пароль должен содержать минимум 8 символов."
        elif password != password2:
            error = "Пароли не совпадают."
        else:
            # Some macOS Python 3.9 builds expose no hashlib.scrypt.
            # Prefer scrypt when available and fall back to PBKDF2 otherwise.
            hash_method = (
                "scrypt"
                if hasattr(hashlib, "scrypt")
                else "pbkdf2:sha256:600000"
            )
            set_setting(
                "admin_password_hash",
                generate_password_hash(password, method=hash_method),
            )
            consume_setup_code()
            session.clear()
            session["is_admin"] = True
            return redirect(url_for("index"))
    return render_template("setup.html", error=error)

@app.route("/login", methods=["GET", "POST"])
def login():
    if not admin_configured():
        return redirect(url_for("setup"))
    if is_admin():
        return redirect(url_for("index"))
    error = None
    next_url = request.args.get("next") or request.form.get("next") or url_for("index")
    if request.method == "POST":
        password = request.form.get("password", "")
        password_hash = get_setting("admin_password_hash")
        if password_hash and check_password_hash(password_hash, password):
            session["is_admin"] = True
            safe_next = next_url if next_url.startswith("/") and not next_url.startswith("//") else url_for("index")
            return redirect(safe_next)
        error = "Неверный пароль."
    return render_template("login.html", error=error, next_url=next_url)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/")
def index():
    sort = request.args.get("sort", "date")
    category = request.args.get("category")
    conn = get_db()
    c = conn.cursor()

    if category:
        query = """SELECT f.* FROM files f
                   JOIN file_category fc ON f.id = fc.file_id
                   WHERE fc.category_id = ?"""
        params = [category]
    else:
        query, params = "SELECT * FROM files WHERE 1=1", []

    if not is_admin():
        query += """ AND NOT EXISTS (
            SELECT 1
            FROM file_category private_fc
            JOIN categories private_c ON private_c.id = private_fc.category_id
            WHERE private_fc.file_id = f.id
              AND LOWER(TRIM(private_c.name)) = 'privat'
        )"""

    if sort == "views":
        query += " ORDER BY views DESC"
    elif sort == "length":
        query += " ORDER BY CASE WHEN length IS NULL THEN 1 ELSE 0 END, length DESC"
    else:
        query += " ORDER BY upload_date DESC"

    files = c.execute(query, tuple(params)).fetchall()
    if is_admin():
        categories = c.execute("SELECT * FROM categories ORDER BY name ASC").fetchall()
    else:
        categories = c.execute(
            "SELECT * FROM categories WHERE LOWER(TRIM(name)) <> 'privat' ORDER BY name ASC"
        ).fetchall()
    file_categories = c.execute("SELECT * FROM file_category").fetchall()
    conn.close()

    return render_template("index.html", files=files, categories=categories, file_categories=file_categories)

@app.route("/add_category", methods=["POST"])
@admin_required
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
    if request.method == "POST" and not is_admin():
        return redirect(url_for("login", next=request.full_path))
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
    if is_admin():
        categories = c.execute("SELECT * FROM categories ORDER BY name ASC").fetchall()
    else:
        categories = c.execute(
            "SELECT * FROM categories WHERE LOWER(TRIM(name)) <> 'privat' ORDER BY name ASC"
        ).fetchall()
    conn.close()
    return render_template("categories.html", categories=categories)

@app.route("/scan_files")
@admin_required
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
    file = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not file:
        conn.close()
        abort(404)

    if is_private_file(file_id) and not is_admin():
        conn.close()
        abort(404)

    if request.method == "POST" and not is_admin():
        conn.close()
        return redirect(url_for("login", next=request.full_path))

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
                except (ValueError, TypeError):
                    pass
        conn.commit()
        conn.close()
        return redirect(url_for("file_page", file_id=file_id))

    c.execute("UPDATE files SET views = views + 1 WHERE id=?", (file_id,))
    conn.commit()
    if is_admin():
        categories = c.execute("SELECT * FROM categories ORDER BY name ASC").fetchall()
    else:
        categories = c.execute(
            "SELECT * FROM categories WHERE LOWER(TRIM(name)) <> 'privat' ORDER BY name ASC"
        ).fetchall()
    file_cats = [row["category_id"] for row in c.execute("SELECT category_id FROM file_category WHERE file_id=?", (file_id,)).fetchall()]
    conn.close()
    return render_template("file.html", file=file, categories=categories, file_cats=file_cats)

@app.route("/upload")
@admin_required
def upload_page():
    conn = get_db()
    categories = conn.execute("SELECT * FROM categories ORDER BY name ASC").fetchall()
    conn.close()
    return render_template("upload.html", categories=categories)

@app.route("/upload_chunk", methods=["POST"])
@admin_required
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
@admin_required
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
        except (ValueError, TypeError):
            pass
    return redirect(url_for("index"))

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    if private_file_by_filename(filename) and not is_admin():
        abort(404)
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route("/download/<path:filename>")
def download_file(filename):
    if private_file_by_filename(filename) and not is_admin():
        abort(404)
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)

@app.route("/thumbnails/<path:filename>")
def thumb_file(filename):
    try:
        file_id = int(Path(filename).stem)
    except ValueError:
        abort(404)
    if is_private_file(file_id) and not is_admin():
        abort(404)
    return send_from_directory(THUMB_FOLDER, filename)

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "1313"))
    serve(app, host=host, port=port, threads=int(os.environ.get("WAITRESS_THREADS", "8")))
