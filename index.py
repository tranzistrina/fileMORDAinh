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
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
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
            upload_date TEXT,
            artist TEXT NOT NULL DEFAULT '',
            album TEXT NOT NULL DEFAULT '',
            track_number INTEGER
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS file_category (
            file_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            UNIQUE(file_id, category_id),
            FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE,
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS porno_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS file_porno_category (
            file_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            UNIQUE(file_id, category_id),
            FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE,
            FOREIGN KEY(category_id) REFERENCES porno_categories(id) ON DELETE CASCADE
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")

        existing_columns = {row[1] for row in c.execute("PRAGMA table_info(files)").fetchall()}
        migrations = {
            "artist": "ALTER TABLE files ADD COLUMN artist TEXT NOT NULL DEFAULT ''",
            "album": "ALTER TABLE files ADD COLUMN album TEXT NOT NULL DEFAULT ''",
            "track_number": "ALTER TABLE files ADD COLUMN track_number INTEGER",
        }
        for column, statement in migrations.items():
            if column not in existing_columns:
                c.execute(statement)
        c.execute("INSERT OR IGNORE INTO categories(name) VALUES ('aud')")
        c.execute("INSERT OR IGNORE INTO categories(name) VALUES ('porno')")
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
    if ext in {"mp4", "avi", "mov", "mkv", "flv", "wmv", "webm", "m4v"}:
        return "video"
    if ext in {"mp3", "wav", "ogg", "flac", "aac", "m4a"}:
        return "audio"
    if ext in {"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp"}:
        return "image"
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

def get_category_id(name):
    normalized = name.strip().lower()
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            "SELECT id FROM categories WHERE LOWER(TRIM(name)) = ? LIMIT 1",
            (normalized,),
        ).fetchone()
    return row[0] if row else None


def is_category_file(file_id, category_name):
    category_id = get_category_id(category_name)
    if category_id is None:
        return False
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            "SELECT 1 FROM file_category WHERE file_id = ? AND category_id = ? LIMIT 1",
            (file_id, category_id),
        ).fetchone()
    return row is not None


def is_porno_file(file_id):
    return is_category_file(file_id, "porno")


def get_porno_category_ids(file_id):
    with sqlite3.connect(DB) as conn:
        rows = conn.execute(
            "SELECT category_id FROM file_porno_category WHERE file_id = ?",
            (file_id,),
        ).fetchall()
    return [row[0] for row in rows]


def get_porno_recommendations(file_id, limit=8):
    conn = get_db()
    try:
        base_ids = get_porno_category_ids(file_id)
        params = list(base_ids) + [file_id] if base_ids else [file_id]
        if base_ids:
            placeholders = ",".join("?" for _ in base_ids)
            overlap_expr = f"COUNT(DISTINCT CASE WHEN matched_pc.category_id IN ({placeholders}) THEN matched_pc.category_id END)"
        else:
            overlap_expr = "0"

        query = f"""SELECT f.*,
                           {overlap_expr} AS category_overlap
                    FROM files f
                    JOIN file_category porno_fc ON porno_fc.file_id = f.id
                    JOIN categories porno_c ON porno_c.id = porno_fc.category_id
                    LEFT JOIN file_porno_category matched_pc ON matched_pc.file_id = f.id
                    WHERE f.id <> ?
                      AND f.file_type IN ('image', 'video')
                      AND LOWER(TRIM(porno_c.name)) = 'porno'"""
        if not is_admin():
            query += """ AND NOT EXISTS (
                SELECT 1 FROM file_category private_fc
                JOIN categories private_c ON private_c.id = private_fc.category_id
                WHERE private_fc.file_id = f.id
                  AND LOWER(TRIM(private_c.name)) = 'privat'
            )"""
        if base_ids:
            query += f""" AND EXISTS (
                SELECT 1 FROM file_porno_category overlap_pc
                WHERE overlap_pc.file_id = f.id
                  AND overlap_pc.category_id IN ({placeholders})
            )"""
            params.extend(base_ids)

        query += """ GROUP BY f.id
                     ORDER BY category_overlap DESC,
                              f.views DESC,
                              f.upload_date DESC
                     LIMIT ?"""
        params.append(limit)

        rows = list(conn.execute(query, tuple(params)).fetchall())
        if len(rows) < limit:
            existing = {row["id"] for row in rows}
            fallback = """SELECT DISTINCT f.*
                          FROM files f
                          JOIN file_category porno_fc ON porno_fc.file_id = f.id
                          JOIN categories porno_c ON porno_c.id = porno_fc.category_id
                          WHERE f.id <> ?
                            AND f.file_type IN ('image', 'video')
                            AND LOWER(TRIM(porno_c.name)) = 'porno'"""
            fallback_params = [file_id]
            if not is_admin():
                fallback += """ AND NOT EXISTS (
                    SELECT 1 FROM file_category private_fc
                    JOIN categories private_c ON private_c.id = private_fc.category_id
                    WHERE private_fc.file_id = f.id
                      AND LOWER(TRIM(private_c.name)) = 'privat'
                )"""
            fallback += " ORDER BY f.upload_date DESC LIMIT ?"
            fallback_params.append(limit * 2)
            for row in conn.execute(fallback, tuple(fallback_params)).fetchall():
                if row["id"] not in existing:
                    rows.append(row)
                    existing.add(row["id"])
                    if len(rows) >= limit:
                        break
        return rows
    finally:
        conn.close()


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

def is_aud_file(file_id):
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            """SELECT 1
               FROM file_category fc
               JOIN categories c ON c.id = fc.category_id
               WHERE fc.file_id = ? AND LOWER(TRIM(c.name)) = 'aud'
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
    view = request.args.get("view", "library")
    if view not in {"library", "audio"}:
        view = "library"

    conn = get_db()
    c = conn.cursor()

    if category:
        query = """SELECT f.* FROM files f
                   JOIN file_category fc ON f.id = fc.file_id
                   WHERE fc.category_id = ?"""
        params = [category]
    else:
        query, params = "SELECT * FROM files f WHERE 1=1", []

    query += """ AND NOT EXISTS (
        SELECT 1
        FROM file_category porno_fc
        JOIN categories porno_c ON porno_c.id = porno_fc.category_id
        WHERE porno_fc.file_id = f.id
          AND LOWER(TRIM(porno_c.name)) = 'porno'
    )"""

    if not is_admin():
        query += """ AND NOT EXISTS (
            SELECT 1
            FROM file_category private_fc
            JOIN categories private_c ON private_c.id = private_fc.category_id
            WHERE private_fc.file_id = f.id
              AND LOWER(TRIM(private_c.name)) = 'privat'
        )"""

    if view == "audio":
        query += """ AND f.file_type = 'audio'
            AND EXISTS (
                SELECT 1
                FROM file_category aud_fc
                JOIN categories aud_c ON aud_c.id = aud_fc.category_id
                WHERE aud_fc.file_id = f.id
                  AND LOWER(TRIM(aud_c.name)) = 'aud'
            )"""
    else:
        query += """ AND NOT EXISTS (
            SELECT 1
            FROM file_category aud_fc
            JOIN categories aud_c ON aud_c.id = aud_fc.category_id
            WHERE aud_fc.file_id = f.id
              AND LOWER(TRIM(aud_c.name)) = 'aud'
        )"""

    if sort == "views":
        query += " ORDER BY views DESC"
    elif sort == "length":
        query += " ORDER BY CASE WHEN length IS NULL THEN 1 ELSE 0 END, length DESC"
    else:
        query += " ORDER BY upload_date DESC"

    files = c.execute(query, tuple(params)).fetchall()
    categories = c.execute(
        "SELECT * FROM categories WHERE LOWER(TRIM(name)) NOT IN ('privat', 'aud', 'porno') ORDER BY name ASC"
    ).fetchall()
    file_categories = c.execute("SELECT * FROM file_category").fetchall()
    conn.close()

    return render_template(
        "index.html", files=files, categories=categories,
        file_categories=file_categories, view=view,
    )


@app.route("/audio")
def audio_page():
    search = request.args.get("q", "").strip()
    conn = get_db()
    c = conn.cursor()
    query = """SELECT f.* FROM files f
               WHERE f.file_type = 'audio'
                 AND EXISTS (
                     SELECT 1 FROM file_category aud_fc
                     JOIN categories aud_c ON aud_c.id = aud_fc.category_id
                     WHERE aud_fc.file_id = f.id
                       AND LOWER(TRIM(aud_c.name)) = 'aud'
                 )"""
    params = []
    query += """ AND NOT EXISTS (
        SELECT 1
        FROM file_category porno_fc
        JOIN categories porno_c ON porno_c.id = porno_fc.category_id
        WHERE porno_fc.file_id = f.id
          AND LOWER(TRIM(porno_c.name)) = 'porno'
    )"""
    if not is_admin():
        query += """ AND NOT EXISTS (
            SELECT 1 FROM file_category private_fc
            JOIN categories private_c ON private_c.id = private_fc.category_id
            WHERE private_fc.file_id = f.id
              AND LOWER(TRIM(private_c.name)) = 'privat'
        )"""
    if search:
        query += """ AND (
            LOWER(f.title) LIKE ?
            OR LOWER(COALESCE(f.artist, '')) LIKE ?
            OR LOWER(COALESCE(f.album, '')) LIKE ?
            OR LOWER(f.filename) LIKE ?
        )"""
        needle = f"%{search.lower()}%"
        params.extend([needle, needle, needle, needle])
    query += """ ORDER BY
        LOWER(COALESCE(f.album, '')) ASC,
        CASE WHEN f.track_number IS NULL THEN 1 ELSE 0 END,
        f.track_number ASC,
        LOWER(f.title) ASC"""
    tracks = c.execute(query, tuple(params)).fetchall()
    conn.close()
    return render_template("audio.html", tracks=tracks, search=search)


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
    categories = c.execute(
        "SELECT * FROM categories WHERE LOWER(TRIM(name)) NOT IN ('privat', 'porno') ORDER BY name ASC"
    ).fetchall()
    conn.close()
    return render_template("categories.html", categories=categories)

@app.route("/porno")
def porno_page():
    search = request.args.get("q", "").strip()
    category_id = request.args.get("category", type=int)
    sort = request.args.get("sort", "date")
    if sort not in {"date", "views", "title"}:
        sort = "date"

    conn = get_db()
    query = """SELECT DISTINCT f.*
               FROM files f
               JOIN file_category fc ON fc.file_id = f.id
               JOIN categories c ON c.id = fc.category_id
               WHERE LOWER(TRIM(c.name)) = 'porno'
                 AND f.file_type IN ('image', 'video')"""
    params = []

    if not is_admin():
        query += """ AND NOT EXISTS (
            SELECT 1
            FROM file_category private_fc
            JOIN categories private_c ON private_c.id = private_fc.category_id
            WHERE private_fc.file_id = f.id
              AND LOWER(TRIM(private_c.name)) = 'privat'
        )"""

    if category_id:
        query += """ AND EXISTS (
            SELECT 1
            FROM file_porno_category fpc_filter
            WHERE fpc_filter.file_id = f.id
              AND fpc_filter.category_id = ?
        )"""
        params.append(category_id)

    if search:
        query += """ AND (
            LOWER(f.title) LIKE ?
            OR LOWER(f.filename) LIKE ?
            OR LOWER(COALESCE(f.description, '')) LIKE ?
        )"""
        needle = f"%{search.lower()}%"
        params.extend([needle, needle, needle])

    if sort == "views":
        query += " ORDER BY f.views DESC, f.upload_date DESC"
    elif sort == "title":
        query += " ORDER BY LOWER(f.title) ASC, f.upload_date DESC"
    else:
        query += " ORDER BY f.upload_date DESC"

    files = conn.execute(query, tuple(params)).fetchall()
    porno_categories = conn.execute(
        """SELECT pc.id, pc.name, COUNT(fpc.file_id) AS file_count
           FROM porno_categories pc
           LEFT JOIN file_porno_category fpc ON fpc.category_id = pc.id
           GROUP BY pc.id
           ORDER BY LOWER(pc.name) ASC"""
    ).fetchall()
    category_rows = conn.execute(
        """SELECT fpc.file_id, pc.id AS category_id, pc.name
           FROM file_porno_category fpc
           JOIN porno_categories pc ON pc.id = fpc.category_id
           ORDER BY LOWER(pc.name) ASC"""
    ).fetchall()
    file_porno_categories = {}
    for row in category_rows:
        file_porno_categories.setdefault(row["file_id"], []).append(
            {"id": row["category_id"], "name": row["name"]}
        )
    conn.close()

    return render_template(
        "porno.html",
        files=files,
        porno_categories=porno_categories,
        file_porno_categories=file_porno_categories,
        selected_category=category_id,
        search=search,
        sort=sort,
    )


@app.route("/porno/category", methods=["POST"])
@admin_required
def add_porno_category():
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify({"status": "error", "message": "Введите название категории"}), 400
    conn = get_db()
    try:
        conn.execute("INSERT INTO porno_categories(name) VALUES (?)", (name,))
        conn.commit()
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT id, name FROM porno_categories WHERE LOWER(name) = LOWER(?) LIMIT 1",
            (name,),
        ).fetchone()
        conn.close()
        return jsonify({"status": "ok", "id": row["id"], "name": row["name"]}) if row else (
            jsonify({"status": "error", "message": "Категория уже существует"}), 409
        )
    row = conn.execute(
        "SELECT id, name FROM porno_categories WHERE id = last_insert_rowid()"
    ).fetchone()
    conn.close()
    return jsonify({"status": "ok", "id": row["id"], "name": row["name"]})


@app.route("/porno/upload")
@admin_required
def porno_upload_page():
    conn = get_db()
    porno_categories = conn.execute(
        "SELECT * FROM porno_categories ORDER BY LOWER(name) ASC"
    ).fetchall()
    conn.close()
    return render_template("porno_upload.html", porno_categories=porno_categories)


@app.route("/porno/<int:file_id>", methods=["GET", "POST"])
def porno_file_page(file_id):
    if not is_porno_file(file_id):
        abort(404)
    return file_page(file_id, porno_mode=True)


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
        if ftype == "audio":
            aud_row = c.execute("SELECT id FROM categories WHERE LOWER(TRIM(name))='aud' LIMIT 1").fetchone()
            if aud_row:
                c.execute("INSERT OR IGNORE INTO file_category(file_id, category_id) VALUES (?,?)",(file_id,aud_row["id"]))
        elif ftype == "video":
            generate_thumbnail(file_path, file_id, 10)
    conn.commit()
    conn.close()
    return redirect(url_for("index"))

@app.route("/file/<int:file_id>", methods=["GET", "POST"])
def file_page(file_id, porno_mode=False):
    conn = get_db()
    c = conn.cursor()
    file = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not file:
        conn.close(); abort(404)
    if is_private_file(file_id) and not is_admin():
        conn.close(); abort(404)
    if is_porno_file(file_id) and not porno_mode and not request.path.startswith("/porno"):
        conn.close()
        return redirect(url_for("porno_file_page", file_id=file_id))
    if request.method == "POST" and not is_admin():
        conn.close(); return redirect(url_for("login", next=request.full_path))
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "")
        thumb_time = request.form.get("thumb_time")
        artist = request.form.get("artist", "").strip()
        album = request.form.get("album", "").strip()
        track_raw = request.form.get("track_number", "").strip()
        aud_tag = request.form.get("aud_tag") == "1"
        try:
            track_number = int(track_raw) if track_raw else None
            if track_number is not None and track_number < 1: raise ValueError
        except ValueError:
            conn.close(); return jsonify({"status":"error","message":"Некорректный номер трека"}),400
        c.execute(
            "UPDATE files SET title=?, description=?, artist=?, album=?, track_number=? WHERE id=?",
            (title, description,
             artist if file["file_type"] == "audio" else "",
             album if file["file_type"] == "audio" else "",
             track_number if file["file_type"] == "audio" else None, file_id),
        )
        c.execute("DELETE FROM file_category WHERE file_id=?", (file_id,))
        category_ids=[]
        for cat in request.form.getlist("categories"):
            try: category_ids.append(int(cat))
            except (TypeError,ValueError): pass
        if file["file_type"] == "audio" and aud_tag:
            aud_row=c.execute("SELECT id FROM categories WHERE LOWER(TRIM(name))='aud' LIMIT 1").fetchone()
            if aud_row: category_ids.append(aud_row["id"])
        if porno_mode:
            porno_row = c.execute(
                "SELECT id FROM categories WHERE LOWER(TRIM(name))='porno' LIMIT 1"
            ).fetchone()
            if porno_row:
                category_ids.append(porno_row["id"])

        for cat_id in dict.fromkeys(category_ids):
            c.execute("INSERT OR IGNORE INTO file_category(file_id, category_id) VALUES (?,?)",(file_id,cat_id))

        if is_porno_file(file_id):
            c.execute("DELETE FROM file_porno_category WHERE file_id=?", (file_id,))
            for cat in request.form.getlist("porno_categories"):
                try:
                    c.execute(
                        "INSERT OR IGNORE INTO file_porno_category(file_id, category_id) VALUES (?,?)",
                        (file_id, int(cat)),
                    )
                except (TypeError, ValueError):
                    pass

        if thumb_time and thumb_time.strip() and file["file_type"] == "video":
            try: generate_thumbnail(UPLOAD_FOLDER / file["filename"], file_id, max(0.0,float(thumb_time)))
            except (ValueError,TypeError): pass
        conn.commit(); conn.close()
        return redirect(url_for("porno_file_page" if porno_mode else "file_page", file_id=file_id))
    c.execute("UPDATE files SET views = views + 1 WHERE id=?", (file_id,))
    conn.commit()
    categories = c.execute("SELECT * FROM categories ORDER BY name ASC").fetchall()
    file_cats=[row["category_id"] for row in c.execute("SELECT category_id FROM file_category WHERE file_id=?",(file_id,)).fetchall()]
    porno_categories = c.execute("SELECT * FROM porno_categories ORDER BY LOWER(name) ASC").fetchall()
    porno_file_cats = get_porno_category_ids(file_id)
    recommendations = get_porno_recommendations(file_id) if is_porno_file(file_id) else []
    conn.close()
    template = "porno_file.html" if is_porno_file(file_id) else "file.html"
    return render_template(
        template,
        porno_mode=porno_mode,
        file=file,
        categories=categories,
        file_cats=file_cats,
        file_is_aud=is_aud_file(file_id),
        porno_categories=porno_categories,
        porno_file_cats=porno_file_cats,
        recommendations=recommendations,
    )

@app.route("/pac")
@admin_required
def batch_upload_page():
    conn = get_db()
    categories = conn.execute(
        "SELECT * FROM categories WHERE LOWER(TRIM(name)) NOT IN ('aud', 'porno') ORDER BY LOWER(name) ASC"
    ).fetchall()
    conn.close()
    return render_template("batch_upload.html", categories=categories)


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
    porno_mode = request.form.get("porno_mode") == "1"
    if porno_mode and get_file_type(filename) not in {"image", "video"}:
        return jsonify({"status": "error", "message": "В разделе porno разрешены только фото и видео"}), 400
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
    artist = request.form.get("artist", "").strip()
    album = request.form.get("album", "").strip()
    track_raw = request.form.get("track_number", "").strip()
    selected_categories = request.form.getlist("categories")
    selected_porno_categories = request.form.getlist("porno_categories")
    porno_mode = request.form.get("porno_mode") == "1"

    try:
        track_number = int(track_raw) if track_raw else None
        if track_number is not None and track_number < 1:
            raise ValueError
    except ValueError:
        return jsonify({"status": "error", "message": "Некорректный номер трека"}), 400

    path = UPLOAD_FOLDER / filename
    if not path.is_file():
        return jsonify({"status": "error", "message": "Загруженный файл не найден"}), 400
    ftype = get_file_type(filename)
    if porno_mode and ftype not in {"image", "video"}:
        return jsonify({"status": "error", "message": "В разделе porno разрешены только фото и видео"}), 400
    length = get_video_length(path) if ftype == "video" else None
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""INSERT INTO files (
                         filename, title, description, file_type, length, filesize,
                         upload_date, artist, album, track_number
                     )
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (
                      filename, title, desc, ftype, length, path.stat().st_size,
                      utc_now_iso(),
                      artist if ftype == "audio" else "",
                      album if ftype == "audio" else "",
                      track_number if ftype == "audio" else None,
                  ))
        file_id = c.lastrowid
        for cat in selected_categories:
            try:
                c.execute("INSERT OR IGNORE INTO file_category(file_id, category_id) VALUES (?, ?)", (file_id, int(cat)))
            except (TypeError, ValueError):
                pass

        if porno_mode:
            porno_row = c.execute(
                "SELECT id FROM categories WHERE LOWER(TRIM(name))='porno' LIMIT 1"
            ).fetchone()
            if not porno_row:
                raise sqlite3.IntegrityError("Missing porno category")
            c.execute(
                "INSERT OR IGNORE INTO file_category(file_id, category_id) VALUES (?, ?)",
                (file_id, porno_row["id"]),
            )
            for cat in selected_porno_categories:
                try:
                    c.execute(
                        "INSERT OR IGNORE INTO file_porno_category(file_id, category_id) VALUES (?, ?)",
                        (file_id, int(cat)),
                    )
                except (TypeError, ValueError):
                    pass

        if ftype == "audio":
            aud_row = c.execute("SELECT id FROM categories WHERE LOWER(TRIM(name))='aud' LIMIT 1").fetchone()
            if aud_row:
                c.execute("INSERT OR IGNORE INTO file_category(file_id, category_id) VALUES (?,?)",(file_id,aud_row["id"]))
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
    if porno_mode:
        return redirect(url_for("porno_page"))
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
