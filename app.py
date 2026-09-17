import os
import re
import io
import smtplib
import secrets
import threading
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from groq import Groq
import psycopg2
from psycopg2.extras import RealDictCursor
from supabase import create_client, Client
from PIL import Image
import urllib.parse
from flask import Flask, flash, redirect, render_template, request, session, url_for, g
from flask_socketio import SocketIO, emit, join_room, leave_room
from authlib.integrations.flask_client import OAuth
from psycopg2 import Error
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
# The project is often run with its .env in the parent folder, so load it as a
# fallback when no project-local .env exists. Existing values are not overridden.
load_dotenv(os.path.join(os.path.dirname(BASE_DIR), ".env"))

# Create the Flask application.
# Flask uses this object to handle routes, templates, sessions, and static files.
app = Flask(__name__)

# A secret key allows Flask sessions and flash messages to work.
# In deployment, set SECRET_KEY in the server environment.
app.secret_key = os.environ.get("SECRET_KEY", "campus-connect-ai-beginner-secret")

# SocketIO adds real-time messaging support to the Flask app.
socketio = SocketIO(app, cors_allowed_origins="*")

# Google OAuth Sign-In. The client is only registered when the credentials are
# present in the environment, so the app keeps working without Google set up.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

oauth = OAuth(app)
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

# Uploaded files are saved inside static/uploads.
app.config["UPLOAD_FOLDER"] = os.path.join(app.root_path, "static", "uploads")
app.config["ALLOWED_EXTENSIONS"] = {"png", "jpg", "jpeg", "gif", "pdf", "ppt", "pptx"}
app.config["IMAGE_EXTENSIONS"] = {"png", "jpg", "jpeg", "gif"}

# MySQL connection settings.
# You can change these values or set environment variables before running the app.

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_DB_URL = os.environ.get("SUPABASE_DB_URL")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

STORAGE_BUCKETS = {
    "profile": "profile-images",
    "post": "post-files",
    "resource": "resources",
    "chat": "chat-files"
}

@app.context_processor
def inject_media_url():
    def media_url(path):
        if not path:
            return ""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if path.startswith("uploads/"):
            return url_for("static", filename=path)
        return path
    return dict(media_url=media_url)


# IVY receives enough local chat history to resolve follow-up questions, without
# sending an entire conversation (or the entire database) to the AI provider.
IVY_CONTEXT_MESSAGE_LIMIT = 16
MAX_CHAT_MESSAGE_LENGTH = 4000


def socket_conversation_id(data):
    """Return a positive conversation id from untrusted Socket.IO event data."""
    if not isinstance(data, dict):
        return None
    try:
        conversation_id = int(data.get("conversation_id"))
        return conversation_id if conversation_id > 0 else None
    except (TypeError, ValueError):
        return None


def get_db_connection():
    """Create and return a new PostgreSQL database connection."""
    return psycopg2.connect(SUPABASE_DB_URL)

def db_cursor(connection, dictionary=False):
    if dictionary:
        return connection.cursor(cursor_factory=RealDictCursor)
    return connection.cursor()


def allowed_file(filename):
    """Check whether an uploaded file has an allowed extension."""
    if "." not in filename:
        return False

    extension = filename.rsplit(".", 1)[1].lower()
    return extension in app.config["ALLOWED_EXTENSIONS"]


def allowed_image(filename):
    """Check whether an uploaded profile file is an image."""
    if "." not in filename:
        return False

    extension = filename.rsplit(".", 1)[1].lower()
    return extension in app.config["IMAGE_EXTENSIONS"]


def save_uploaded_file(uploaded_file, file_prefix, bucket_name):
    """Save an uploaded file and return the path stored in Supabase."""
    if supabase is None:
        return ""

    safe_name = secure_filename(uploaded_file.filename)
    original_name = (safe_name or "file").lower()
    is_image = original_name.rsplit(".", 1)[-1] in app.config["IMAGE_EXTENSIONS"] if "." in original_name else False

    if is_image:
        # Compress / downscale images before upload to keep the feed fast.
        data, content_type = compress_image_bytes(uploaded_file, max_dimension=1600)
        safe_name = safe_name.rsplit(".", 1)[0] + ".jpg" if safe_name and "." in safe_name else (safe_name or "image.jpg")
    else:
        data = uploaded_file.read()
        content_type = uploaded_file.mimetype or "application/octet-stream"

    upload_time = datetime.now().strftime("%Y%m%d%H%M%S")
    object_path = f"{file_prefix}_{upload_time}_{safe_name}"
    supabase.storage.from_(bucket_name).upload(
        object_path,
        data,
        {"content-type": content_type},
    )
    return supabase.storage.from_(bucket_name).get_public_url(object_path)


def get_logged_in_user():
    """Return basic details for the currently logged-in student."""
    if "user_id" not in session:
        return None

    return {
        "id": session["user_id"],
        "full_name": session["full_name"],
        "email": session["email"],
    }


def login_required():
    """Return True if a student is logged in."""
    return "user_id" in session


def redirect_back(default_endpoint, **values):
    """Redirect back to the page the request came from when it is internal."""
    referrer = request.referrer
    if referrer:
        try:
            referrer_host = urllib.parse.urlsplit(referrer).netloc
        except ValueError:
            referrer_host = ""
        if referrer_host == request.host:
            return redirect(referrer)
    return redirect(url_for(default_endpoint, **values))


def create_notification(user_id, actor_id, notification_type, message):
    """Save a notification for another student."""
    if user_id == actor_id:
        return

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        # This INSERT query stores one notification row.
        cursor.execute(
            """
            INSERT INTO notifications (user_id, actor_id, notification_type, message)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, actor_id, notification_type, message),
        )

        connection.commit()

    except Error:
        pass

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()


# ── CSRF Protection ─────────────────────────────────────────────────────────
def generate_csrf_token():
    """Return the session's CSRF token, creating one on first use."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def csrf_valid():
    """Compare a submitted token against the session token."""
    expected = session.get("csrf_token")
    if not expected:
        return False
    submitted = request.headers.get("X-CSRFToken") or request.form.get("csrf_token")
    return submitted == expected


@app.before_request
def protect_from_csrf():
    """Reject state-changing requests that do not carry a valid CSRF token."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # SocketIO long-polling transport uses HTTP POST to /socket.io/ without
        # a page-level CSRF token; it performs its own auth, so exempt it.
        if request.path.startswith("/socket.io/"):
            return
        if not csrf_valid():
            if request.headers.get("X-Requested-With") == "fetch" or request.is_json:
                return {"ok": False, "error": "Security token missing or expired."}, 400
            flash("Your session expired. Please try again.")
            return redirect(request.referrer or url_for("index"))


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": generate_csrf_token}


# ── Email delivery (SMTP with a development fallback) ───────────────────────
def send_email(to_address, subject, html_body):
    """Send an HTML email. Without SMTP configured, print it to the console."""
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    from_address = os.environ.get("SMTP_FROM", smtp_user or "noreply@campusconnect.local")

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = to_address
    message.attach(MIMEText(html_body, "html"))

    if not (smtp_host and smtp_user):
        # Development mode: never crash, just show the message in the console.
        print("\n===== EMAIL (not sent, SMTP not configured) =====")
        print(f"To:      {to_address}")
        print(f"Subject: {subject}")
        print(html_body)
        print("==================================================\n")
        return True

    try:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(from_address, [to_address], message.as_string())
        server.quit()
        return True
    except Exception as error:
        print(f"Email send failed: {error}")
        return False


def send_verification_email(user_id, email, full_name):
    """Create a verify-email token and email the link to the student."""
    token = create_auth_token(user_id, "verify_email")
    verify_url = url_for("verify_email", token=token, _external=True)
    html = (
        f"<h2>Welcome to IVY, {full_name}!</h2>"
        "<p>Please confirm your email address to unlock posting, commenting, "
        "friending, and messaging.</p>"
        f'<p><a href="{verify_url}">Confirm my email</a></p>'
        f"<p style='color:#64748b'>If the button does not work, paste this link: "
        f"<br>{verify_url}</p>"
    )
    return send_email(email, "IVY — Confirm your email", html)


def send_password_reset_email(user_id, email):
    """Create a password-reset token and email the reset link to the student."""
    token = create_auth_token(user_id, "password_reset")
    reset_url = url_for("reset_password", token=token, _external=True)
    html = (
        "<h2>Reset your IVY password</h2>"
        "<p>Click the link below to choose a new password. This link expires "
        "in one hour.</p>"
        f'<p><a href="{reset_url}">Reset my password</a></p>'
        f"<p style='color:#64748b'>If the button does not work, paste this link: "
        f"<br>{reset_url}</p>"
    )
    return send_email(email, "IVY — Reset your password", html)


# ── One-time auth tokens ────────────────────────────────────────────────────
def create_auth_token(user_id, token_type, ttl_hours=24):
    """Store and return a fresh single-use token for a user."""
    token = secrets.token_urlsafe(48)
    expires_at = datetime.now() + timedelta(hours=ttl_hours)
    connection = get_db_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO auth_tokens (user_id, token, token_type, expires_at)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, token, token_type, expires_at),
        )
        connection.commit()
    finally:
        if "cursor" in locals():
            cursor.close()
        connection.close()
    return token


def consume_auth_token(token, token_type):
    """Return the user id for a valid, unused, unexpired token, or None."""
    connection = get_db_connection()
    try:
        cursor = db_cursor(connection, dictionary=True)
        cursor.execute(
            """
            SELECT auth_tokens.user_id, auth_tokens.expires_at, auth_tokens.used_at,
                   users.is_verified, users.is_admin
            FROM auth_tokens
            INNER JOIN users ON users.id = auth_tokens.user_id
            WHERE auth_tokens.token = %s AND auth_tokens.token_type = %s
            """,
            (token, token_type),
        )
        row = cursor.fetchone()
        if not row:
            return None
        if row["used_at"] or row["expires_at"] < datetime.now():
            return None
        cursor.execute("UPDATE auth_tokens SET used_at = CURRENT_TIMESTAMP WHERE token = %s", (token,))
        connection.commit()
        return row
    except Error:
        return None
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()


# ── Image compression (downscale before upload) ─────────────────────────────
def compress_image_bytes(file_storage, max_dimension=1600):
    """Downscale and re-encode an uploaded image, returning new bytes + mime."""
    try:
        file_storage.stream.seek(0)
        image = Image.open(file_storage.stream)
        format_name = (image.format or "JPEG").upper()
        image = image.convert("RGB") if format_name in ("PNG", "JPEG", "JPG", "GIF") else image
        image = image.convert("RGBA") if image.mode in ("P", "LA") else image
        image.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
        buffer = io.BytesIO()
        save_format = "PNG" if image.mode == "RGBA" else "JPEG"
        if save_format == "JPEG":
            image = image.convert("RGB")
            image.save(buffer, format="JPEG", quality=82, optimize=True)
        else:
            image.save(buffer, format="PNG", optimize=True)
        buffer.seek(0)
        return buffer.read(), ("image/png" if save_format == "PNG" else "image/jpeg")
    except Exception:
        file_storage.stream.seek(0)
        return file_storage.read(), (file_storage.mimetype or "application/octet-stream")


# ── Live presence ───────────────────────────────────────────────────────────
ONLINE_USERS = set()
ONLINE_LOCK = threading.Lock()
SOCKET_USER = {}


def mark_user_online(user_id):
    with ONLINE_LOCK:
        ONLINE_USERS.add(user_id)


def mark_user_offline(user_id):
    with ONLINE_LOCK:
        ONLINE_USERS.discard(user_id)


def is_user_online(user_id):
    return user_id in ONLINE_USERS


@app.context_processor
def inject_presence():
    def is_online(user_id):
        return is_user_online(user_id)
    return {"is_online": is_online}


# ── Verification / admin helpers ────────────────────────────────────────────
def update_last_seen(user_id):
    """Persist the last-seen timestamp for a user."""
    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE users SET last_seen = CURRENT_TIMESTAMP WHERE id = %s",
            (user_id,),
        )
        connection.commit()
    except Error:
        pass
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()


def verification_required():
    """Return True if the logged-in user may create content (email verified)."""
    if "is_verified" in session and session["is_verified"]:
        return True
    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        cursor.execute("SELECT is_verified, is_admin FROM users WHERE id = %s", (session.get("user_id"),))
        row = cursor.fetchone()
        if row:
            session["is_verified"] = row["is_verified"]
            session["is_admin"] = row["is_admin"]
            return bool(row["is_verified"])
        return False
    except Error:
        return False
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()


@app.context_processor
def inject_user_flags():
    def flag_is_admin():
        if "is_admin" in session:
            return session["is_admin"]
        verification_required()
        return session.get("is_admin", False)
    return {"is_admin_user": flag_is_admin}


@app.context_processor
def inject_verification_state():
    def user_needs_verification():
        if "is_verified" in session:
            return not session["is_verified"]
        return verification_required() is False
    return {"needs_verification": user_needs_verification}


@app.route("/")
def index():
    """Show the landing page for Campus Connect AI."""
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    """Show the registration form and save a new student account."""
    if request.method == "POST":
        full_name = request.form.get("full_name")
        email = request.form.get("email")
        department = request.form.get("department")
        year = request.form.get("year")
        password = request.form.get("password")
        bio = request.form.get("bio")
        hashed_password = generate_password_hash(password)

        try:
            connection = get_db_connection()
            cursor = db_cursor(connection, dictionary=True)

            # This SELECT query checks if the email is already registered.
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            existing_user = cursor.fetchone()

            if existing_user:
                flash("This email is already registered. Please login instead.")
                return redirect(url_for("register"))

            # This INSERT query saves the new student account.
            cursor.execute(
                """
                INSERT INTO users (full_name, email, password, department, study_year, bio)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (full_name, email, hashed_password, department, year, bio),
            )
            new_user = cursor.fetchone()

            connection.commit()

            send_verification_email(new_user["id"], email, full_name)
            flash("Account created successfully. A confirmation email has been sent to verify your account. Please check your inbox.")
            return redirect(url_for("login"))

        except Error as error:
            flash(f"Database error: {error}")
            return redirect(url_for("register"))

        finally:
            if "connection" in locals() and not connection.closed:
                if "cursor" in locals():
                    cursor.close()
                connection.close()

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    """Show the login form and check student credentials."""
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        try:
            connection = get_db_connection()
            cursor = db_cursor(connection, dictionary=True)

            # This SELECT query finds the account with the submitted email.
            cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()

            if not user:
                flash("No account found with this email. Please register first.")
                return redirect(url_for("login"))

            if not user["password"]:
                flash("This account uses Google Sign-In. Please use the Continue with Google button.")
                return redirect(url_for("login"))

            if check_password_hash(user["password"], password):
                session["user_id"] = user["id"]
                session["full_name"] = user["full_name"]
                session["email"] = user["email"]
                session["is_verified"] = user["is_verified"]
                session["is_admin"] = user["is_admin"]
                update_last_seen(user["id"])

                flash("Login successful. Welcome back!")
                return redirect(url_for("feed"))

            flash("Invalid email or password. Please try again.")
            return redirect(url_for("login"))

        except Error as error:
            flash(f"Database error: {error}")
            return redirect(url_for("login"))

        finally:
            if "connection" in locals() and not connection.closed:
                if "cursor" in locals():
                    cursor.close()
                connection.close()

    return render_template("login.html")


@app.route("/logout")
def logout():
    """Log the current student out by clearing their session."""
    mark_user_offline(session.get("user_id"))
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for("login"))


# ── Email verification ──────────────────────────────────────────────────────
@app.route("/verify-email/<token>")
def verify_email(token):
    """Confirm a student's email address using a one-time link."""
    record = consume_auth_token(token, "verify_email")
    if not record:
        flash("This verification link is invalid or has expired. Please request a new one after logging in.")
        return redirect(url_for("login"))

    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE users SET is_verified = TRUE WHERE id = %s",
            (record["user_id"],),
        )
        connection.commit()
    except Error as error:
        flash(f"Database error: {error}")
        return redirect(url_for("login"))
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    session["is_verified"] = True
    flash("Your email has been verified. You can now post, comment, and connect with peers.")
    return redirect(url_for("feed"))


@app.route("/resend-verification", methods=["POST"])
def resend_verification():
    """Send a fresh verification link to the logged-in student."""
    if not login_required():
        flash("Please login to resend the verification email.")
        return redirect(url_for("login"))

    email = session.get("email")
    full_name = session.get("full_name")
    send_verification_email(session["user_id"], email, full_name)
    flash("A new verification email has been sent. Check your inbox and spam folder.")
    return redirect(request.referrer or url_for("feed"))


# ── Password reset ──────────────────────────────────────────────────────────
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Show the reset-request form and email a reset link."""
    if request.method == "POST":
        email = request.form.get("email")
        try:
            connection = get_db_connection()
            cursor = db_cursor(connection, dictionary=True)
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
        except Error:
            user = None
        finally:
            if "connection" in locals() and not connection.closed:
                if "cursor" in locals():
                    cursor.close()
                connection.close()

        # Always show the same message to avoid revealing whether an email exists.
        if user:
            send_password_reset_email(user["id"], email)
        flash("If that email is registered, a password reset link has been sent.")
        return redirect(url_for("login"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Let a student choose a new password with a valid reset token."""
    record = consume_auth_token(token, "password_reset")
    if not record:
        flash("This reset link is invalid or has expired. Please request a new one.")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password")
        confirm = request.form.get("confirm_password")
        if not password:
            flash("Please enter a new password.")
            return render_template("reset_password.html", token=token)
        if password != confirm:
            flash("Passwords do not match.")
            return render_template("reset_password.html", token=token)

        try:
            connection = get_db_connection()
            cursor = connection.cursor()
            cursor.execute(
                "UPDATE users SET password = %s WHERE id = %s",
                (generate_password_hash(password), record["user_id"]),
            )
            connection.commit()
            flash("Your password has been updated. Please sign in with your new password.")
            return redirect(url_for("login"))
        except Error as error:
            flash(f"Database error: {error}")
            return render_template("reset_password.html", token=token)
        finally:
            if "connection" in locals() and not connection.closed:
                if "cursor" in locals():
                    cursor.close()
                connection.close()

    return render_template("reset_password.html", token=token)


# ── Account deletion ────────────────────────────────────────────────────────
@app.route("/delete-account", methods=["POST"])
def delete_account():
    """Permanently delete the logged-in student's account and content."""
    if not login_required():
        flash("Please login first.")
        return redirect(url_for("login"))

    confirmation = request.form.get("confirmation")
    if confirmation != "DELETE":
        flash("Please type DELETE to confirm you want to remove your account.")
        return redirect(url_for("profile"))

    user_id = session["user_id"]
    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        connection.commit()
    except Error as error:
        flash(f"Database error: {error}")
        return redirect(url_for("profile"))
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    mark_user_offline(user_id)
    session.clear()
    flash("Your account and all associated data have been deleted.")
    return redirect(url_for("index"))


# ── Reporting ───────────────────────────────────────────────────────────────
@app.route("/report", methods=["POST"])
def report_content():
    """File a report against a post, comment, resource, or user."""
    if not login_required():
        flash("Please login to report content.")
        return redirect(url_for("login"))

    target_type = request.form.get("target_type")
    reason = request.form.get("reason")
    details = request.form.get("details")

    valid_types = {"post", "comment", "user", "resource"}
    if target_type not in valid_types or not reason:
        flash("A valid report type and reason are required.")
        return redirect(request.referrer or url_for("feed"))

    try:
        target_id = int(request.form.get("target_id"))
    except (TypeError, ValueError):
        flash("Invalid report target.")
        return redirect(request.referrer or url_for("feed"))

    if target_type in ("post", "comment", "user", "resource"):
        try:
            table = {"post": "posts", "comment": "comments", "user": "users", "resource": "resources"}[target_type]
            connection = get_db_connection()
            cursor = connection.cursor()
            cursor.execute(f"SELECT 1 FROM {table} WHERE id = %s", (target_id,))
            if not cursor.fetchone():
                flash("The content you reported no longer exists.")
                return redirect(request.referrer or url_for("feed"))
            cursor.execute(
                """
                INSERT INTO reports (reporter_id, target_type, target_id, reason, details)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (session["user_id"], target_type, target_id, reason, details),
            )
            connection.commit()
            flash("Thanks for reporting. Our moderators will review it.")
        except Error as error:
            flash(f"Database error: {error}")
        finally:
            if "connection" in locals() and not connection.closed:
                if "cursor" in locals():
                    cursor.close()
                connection.close()

    return redirect(request.referrer or url_for("feed"))


def admin_required():
    """Require an authenticated admin; otherwise redirect to the feed."""
    if not login_required():
        return False
    if "is_admin" in session:
        return session["is_admin"]
    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        cursor.execute("SELECT is_admin FROM users WHERE id = %s", (session["user_id"],))
        row = cursor.fetchone()
        admin = bool(row and row["is_admin"])
        session["is_admin"] = admin
        return admin
    except Error:
        return False
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()


@app.route("/admin/reports")
def admin_reports():
    """Admin moderation queue of open reports."""
    if not admin_required():
        flash("You do not have permission to view reports.")
        return redirect(url_for("feed"))

    report_list = []
    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        cursor.execute(
            """
            SELECT reports.*, reporter.full_name AS reporter_name, users.full_name AS target_name
            FROM reports
            LEFT JOIN users AS reporter ON reporter.id = reports.reporter_id
            LEFT JOIN users AS users ON users.id =
                CASE WHEN reports.target_type = 'user' THEN reports.target_id ELSE NULL END
            WHERE reports.status = 'open'
            ORDER BY reports.created_at DESC
            """
        )
        report_list = cursor.fetchall()
    except Error as error:
        flash(f"Database error: {error}")
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return render_template("admin_reports.html", reports=report_list, user=get_logged_in_user())


@app.route("/admin/resolve/<int:report_id>", methods=["POST"])
def admin_resolve_report(report_id):
    """Mark a report resolved or dismissed."""
    if not admin_required():
        flash("You do not have permission to do that.")
        return redirect(url_for("feed"))

    status = request.form.get("status")
    if status not in ("resolved", "dismissed"):
        status = "resolved"

    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE reports SET status = %s, resolved_at = CURRENT_TIMESTAMP, resolver_id = %s WHERE id = %s",
            (status, session["user_id"], report_id),
        )
        connection.commit()
        flash("Report updated.")
    except Error as error:
        flash(f"Database error: {error}")
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect(url_for("admin_reports"))


@app.route("/admin/delete-target/<string:target_type>/<int:target_id>", methods=["POST"])
def admin_delete_target(target_type, target_id):
    """Remove a reported post or comment and resolve the associated reports."""
    if not admin_required():
        flash("You do not have permission to do that.")
        return redirect(url_for("feed"))

    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        if target_type == "post":
            cursor.execute("DELETE FROM posts WHERE id = %s", (target_id,))
        elif target_type == "comment":
            cursor.execute("DELETE FROM comments WHERE id = %s", (target_id,))
        elif target_type == "resource":
            cursor.execute("DELETE FROM resources WHERE id = %s", (target_id,))
        elif target_type == "user":
            cursor.execute("DELETE FROM users WHERE id = %s", (target_id,))
        else:
            flash("Invalid target type.")
            return redirect(url_for("admin_reports"))
        cursor.execute(
            "UPDATE reports SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP, resolver_id = %s "
            "WHERE target_type = %s AND target_id = %s AND status = 'open'",
            (session["user_id"], target_type, target_id),
        )
        connection.commit()
        flash("Reported content removed and reports marked resolved.")
    except Error as error:
        flash(f"Database error: {error}")
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect(url_for("admin_reports"))


# ── AI study planner ────────────────────────────────────────────────────────
@app.route("/ai/study-plan", methods=["POST"])
def ai_study_plan():
    """Build a personalised study plan with IVY and save it as a new chat."""
    if not login_required():
        return {"ok": False, "error": "Please log in again."}, 401

    payload = request.get_json(silent=True) or {}
    subject = str(payload.get("subject") or "").strip()
    weeks = str(payload.get("weeks") or "").strip()
    hours = str(payload.get("hours") or "").strip()

    if len(subject) > 200 or len(weeks) > 10 or len(hours) > 10:
        return {"ok": False, "error": "Please keep your details short."}, 400

    user_request = f"Subject: {subject}, weeks until exam: {weeks}, hours available: {hours}"
    system_prompt = (
        "You are IVY, a study planner for university students. Create a clear, "
        "practical weekly study plan in markdown. Break the subject into topics, "
        "order them by difficulty, suggest weekly goals, and include how to split "
        "the available hours each week. Keep it concrete and motivating."
    )

    api_key = os.environ.get("GROQ_API_KEY")
    prompt = build_ai_chat_prompt([], user_request)
    try:
        client = Groq(api_key=api_key)
        configured_model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
        reply_text = generate_ai_response(client, system_prompt + "\n\n" + user_request, configured_model)
    except Exception as error:
        print(f"Study planner error: {error}")
        return {"ok": False, "error": "The study planner is temporarily unavailable."}, 502

    if not reply_text:
        return {"ok": False, "error": "The study planner could not generate a plan."}, 502

    connection = get_db_connection()
    try:
        cursor = connection.cursor()
        title = f"Study plan: {subject[:48]}" if subject else "Study plan"
        cursor.execute(
            "INSERT INTO ai_conversations (user_id, title) VALUES (%s, %s) RETURNING id",
            (session["user_id"], title),
        )
        conversation_id = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO ai_messages (conversation_id, role, content) VALUES (%s, 'user', %s)",
            (conversation_id, user_request),
        )
        cursor.execute(
            "INSERT INTO ai_messages (conversation_id, role, content) VALUES (%s, 'assistant', %s)",
            (conversation_id, reply_text),
        )
        connection.commit()
        return {"ok": True, "conversation_id": conversation_id, "title": title, "plan": reply_text}
    except Error as error:
        print(f"Study planner persistence error: {error}")
        return {"ok": False, "error": "Your plan could not be saved."}, 500
    finally:
        if connection and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()


@app.route("/login/google")
def login_google():
    """Send the student to Google's consent screen to sign in."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        flash("Google Sign-In is not configured. Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")
        return redirect(url_for("login"))

    redirect_uri = url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/login/google/callback")
def google_callback():
    """Handle Google's response and log the student in."""
    try:
        token = oauth.google.authorize_access_token()
        userinfo = oauth.google.userinfo(token=token)
    except Exception:
        flash("Google Sign-In could not be completed. Please try again.")
        return redirect(url_for("login"))

    if not userinfo or not userinfo.get("email"):
        flash("Google Sign-In failed. Please try again.")
        return redirect(url_for("login"))

    google_id = str(userinfo.get("sub") or "")
    email = userinfo["email"]
    full_name = userinfo.get("name") or email.split("@")[0]
    picture = userinfo.get("picture") or None

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        cursor.execute(
            """
            SELECT id, full_name, email FROM users
            WHERE email = %s OR (google_id IS NOT NULL AND google_id = %s)
            LIMIT 1
            """,
            (email, google_id),
        )
        user = cursor.fetchone()

        if user:
            # Link the Google id so future sign-ins always find this account.
            cursor.execute(
                "UPDATE users SET google_id = %s, is_verified = TRUE WHERE id = %s",
                (google_id, user["id"]),
            )
        else:
            # First-time Google student: create their account automatically.
            # Google already verified their email address, so the account is
            # marked verified from the start.
            cursor.execute(
                """
                INSERT INTO users
                    (full_name, email, password, auth_provider, google_id,
                     department, study_year, profile_picture, is_verified)
                VALUES (%s, %s, NULL, 'google', %s, NULL, NULL, %s, TRUE)
                RETURNING id, full_name, email
                """,
                (full_name, email, google_id, picture),
            )
            user = cursor.fetchone()

        connection.commit()

        session["user_id"] = user["id"]
        session["full_name"] = user["full_name"]
        session["email"] = user["email"]
        session["is_verified"] = True
        cursor.execute("SELECT is_admin FROM users WHERE id = %s", (user["id"],))
        admin_row = cursor.fetchone()
        session["is_admin"] = admin_row["is_admin"] if admin_row else False
        update_last_seen(user["id"])

        flash("Signed in with Google. Welcome back!")
        return redirect(url_for("feed"))

    except Error as error:
        flash(f"Database error: {error}")
        return redirect(url_for("login"))

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()


@app.route("/feed")
def feed():
    """Show the learning feed for logged-in students."""
    if not login_required():
        flash("Please login to view the learning feed.")
        return redirect(url_for("login"))

    posts = []
    comments_by_post = {}

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        # This SELECT query loads feed posts with counts for likes and comments.
        cursor.execute(
            """
            SELECT
                posts.id,
                posts.user_id,
                posts.content,
                posts.post_type,
                posts.file_path,
                posts.file_type,
                posts.created_at,
                users.full_name,
                users.department,
                users.study_year,
                (SELECT COUNT(*) FROM likes WHERE likes.post_id = posts.id) AS like_count,
                (SELECT COUNT(*) FROM comments WHERE comments.post_id = posts.id) AS comment_count,
                (
                    SELECT COUNT(*)
                    FROM likes
                    WHERE likes.post_id = posts.id
                    AND likes.user_id = %s
                ) AS liked_by_user
            FROM posts
            INNER JOIN users ON posts.user_id = users.id
            ORDER BY posts.created_at DESC
            """,
            (session["user_id"],),
        )
        posts = cursor.fetchall()

        # This SELECT query loads comments with each commenter's name.
        cursor.execute(
            """
            SELECT
                comments.id,
                comments.post_id,
                comments.user_id,
                comments.comment_text,
                comments.created_at,
                users.full_name
            FROM comments
            INNER JOIN users ON comments.user_id = users.id
            ORDER BY comments.created_at ASC
            """
        )
        comments = cursor.fetchall()

        for comment in comments:
            comments_by_post.setdefault(comment["post_id"], []).append(comment)

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return render_template(
        "feed.html",
        posts=posts,
        comments_by_post=comments_by_post,
        user=get_logged_in_user(),
    )


@app.route("/create-post", methods=["POST"])
def create_post():
    """Create a new learning feed post for the logged-in student."""
    if not login_required():
        flash("Please login before creating a post.")
        return redirect(url_for("login"))
    if not verification_required():
        flash("Please verify your email before creating posts.")
        return redirect(url_for("feed"))

    content = request.form.get("content")
    post_type = request.form.get("post_type")
    uploaded_file = request.files.get("post_file")
    file_path = None
    file_type = None

    if not content:
        flash("Please write something before posting.")
        return redirect(url_for("feed"))

    if uploaded_file and uploaded_file.filename:
        if allowed_file(uploaded_file.filename):
            file_path = save_uploaded_file(uploaded_file, f"user_{session['user_id']}_post", STORAGE_BUCKETS["post"])
            file_type = uploaded_file.filename.rsplit(".", 1)[1].lower()
        else:
            flash("Only image, PDF, and PPT files are allowed.")
            return redirect(url_for("feed"))

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        # This INSERT query saves a new post.
        cursor.execute(
            """
            INSERT INTO posts (user_id, content, post_type, file_path, file_type)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (session["user_id"], content, post_type, file_path, file_type),
        )

        connection.commit()
        flash("Your post was shared.")

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect(url_for("feed"))


@app.route("/edit-post/<int:post_id>", methods=["GET", "POST"])
def edit_post(post_id):
    """Edit a post that belongs to the logged-in student."""
    if not login_required():
        flash("Please login before editing posts.")
        return redirect(url_for("login"))

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        # This SELECT query gets one post owned by the logged-in student.
        cursor.execute(
            "SELECT * FROM posts WHERE id = %s AND user_id = %s",
            (post_id, session["user_id"]),
        )
        post = cursor.fetchone()

        if not post:
            flash("Post not found.")
            return redirect(url_for("feed"))

        if request.method == "POST":
            content = request.form.get("content")
            post_type = request.form.get("post_type")

            # This UPDATE query edits the post text and type.
            cursor.execute(
                """
                UPDATE posts
                SET content = %s, post_type = %s
                WHERE id = %s AND user_id = %s
                """,
                (content, post_type, post_id, session["user_id"]),
            )

            connection.commit()
            flash("Post updated.")
            return redirect(url_for("feed"))

    except Error as error:
        flash(f"Database error: {error}")
        return redirect(url_for("feed"))

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return render_template("edit_post.html", post=post, user=get_logged_in_user())


@app.route("/delete-post/<int:post_id>", methods=["POST"])
def delete_post(post_id):
    """Delete a post that belongs to the logged-in student."""
    if not login_required():
        flash("Please login before deleting posts.")
        return redirect(url_for("login"))

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        # This DELETE query removes only the logged-in student's own post.
        cursor.execute(
            "DELETE FROM posts WHERE id = %s AND user_id = %s",
            (post_id, session["user_id"]),
        )

        connection.commit()
        flash("Post deleted.")

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect(url_for("feed"))


@app.route("/like-post/<int:post_id>", methods=["POST"])
def like_post(post_id):
    """Like a post if it is not liked, or remove the like if it is already liked."""
    if not login_required():
        flash("Please login before liking posts.")
        return redirect(url_for("login"))

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        # This SELECT query checks whether this student already liked the post.
        cursor.execute(
            "SELECT id FROM likes WHERE post_id = %s AND user_id = %s",
            (post_id, session["user_id"]),
        )
        existing_like = cursor.fetchone()

        if existing_like:
            # This DELETE query removes the like.
            cursor.execute("DELETE FROM likes WHERE id = %s", (existing_like["id"],))
            flash("Like removed.")
        else:
            # This INSERT query adds a like to the post.
            cursor.execute(
                "INSERT INTO likes (post_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (post_id, session["user_id"]),
            )

            # This SELECT query finds the post owner for a notification.
            cursor.execute("SELECT user_id FROM posts WHERE id = %s", (post_id,))
            post_owner = cursor.fetchone()

            if post_owner:
                create_notification(
                    post_owner["user_id"],
                    session["user_id"],
                    "like",
                    f"{session['full_name']} liked your post.",
                )

            flash("Post liked.")

        connection.commit()

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect(url_for("feed"))


@app.route("/add-comment/<int:post_id>", methods=["POST"])
def add_comment(post_id):
    """Add a comment to a post."""
    if not login_required():
        flash("Please login before commenting.")
        return redirect(url_for("login"))
    if not verification_required():
        flash("Please verify your email before commenting.")
        return redirect(url_for("feed"))

    comment_text = request.form.get("comment_text")

    if not comment_text:
        flash("Please write a comment before submitting.")
        return redirect(url_for("feed"))

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        # This INSERT query saves the comment under the selected post.
        cursor.execute(
            """
            INSERT INTO comments (post_id, user_id, comment_text)
            VALUES (%s, %s, %s)
            """,
            (post_id, session["user_id"], comment_text),
        )

        # This SELECT query finds the post owner for a notification.
        cursor.execute("SELECT user_id FROM posts WHERE id = %s", (post_id,))
        post_owner = cursor.fetchone()

        if post_owner:
            create_notification(
                post_owner["user_id"],
                session["user_id"],
                "comment",
                f"{session['full_name']} commented on your post.",
            )

        connection.commit()
        flash("Comment added.")

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect(url_for("feed"))


@app.route("/delete-comment/<int:comment_id>", methods=["POST"])
def delete_comment(comment_id):
    """Delete a comment only if it belongs to the logged-in student."""
    if not login_required():
        flash("Please login before deleting comments.")
        return redirect(url_for("login"))

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        # This DELETE query removes only the logged-in student's own comment.
        cursor.execute(
            "DELETE FROM comments WHERE id = %s AND user_id = %s",
            (comment_id, session["user_id"]),
        )

        connection.commit()
        flash("Comment deleted.")

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect(url_for("feed"))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    """Show and update the logged-in student's profile."""
    if not login_required():
        flash("Please login to view your profile.")
        return redirect(url_for("login"))

    stats = {"followers": 0, "following": 0, "friends": 0}

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        # This SELECT query gets the current student profile.
        cursor.execute("SELECT * FROM users WHERE id = %s", (session["user_id"],))
        profile_user = cursor.fetchone()

        if not profile_user:
            session.clear()
            flash("Account not found. Please login again.")
            return redirect(url_for("login"))

        if request.method == "POST":
            full_name = request.form.get("full_name")
            department = request.form.get("department")
            study_year = request.form.get("study_year")
            bio = request.form.get("bio")
            skills = request.form.get("skills")
            interests = request.form.get("interests")
            profile_picture = profile_user["profile_picture"]
            cover_photo = profile_user["cover_photo"]
            profile_picture_file = request.files.get("profile_picture")
            cover_photo_file = request.files.get("cover_photo")

            if profile_picture_file and profile_picture_file.filename:
                if allowed_image(profile_picture_file.filename):
                    profile_picture = save_uploaded_file(profile_picture_file, f"user_{session['user_id']}_profile", STORAGE_BUCKETS["profile"])
                else:
                    flash("Profile picture must be PNG, JPG, JPEG, or GIF.")
                    return redirect(url_for("profile"))

            if cover_photo_file and cover_photo_file.filename:
                if allowed_image(cover_photo_file.filename):
                    cover_photo = save_uploaded_file(cover_photo_file, f"user_{session['user_id']}_cover", STORAGE_BUCKETS["profile"])
                else:
                    flash("Cover photo must be PNG, JPG, JPEG, or GIF.")
                    return redirect(url_for("profile"))

            # This UPDATE query saves profile changes for the logged-in student.
            cursor.execute(
                """
                UPDATE users
                SET full_name = %s,
                    department = %s,
                    study_year = %s,
                    bio = %s,
                    skills = %s,
                    interests = %s,
                    profile_picture = %s,
                    cover_photo = %s
                WHERE id = %s
                """,
                (
                    full_name,
                    department,
                    study_year,
                    bio,
                    skills,
                    interests,
                    profile_picture,
                    cover_photo,
                    session["user_id"],
                ),
            )

            connection.commit()
            session["full_name"] = full_name
            flash("Profile updated.")
            return redirect(url_for("profile"))

        # These SELECT queries count followers, following, and accepted friends.
        cursor.execute("SELECT COUNT(*) AS total FROM followers WHERE following_id = %s", (session["user_id"],))
        stats["followers"] = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) AS total FROM followers WHERE follower_id = %s", (session["user_id"],))
        stats["following"] = cursor.fetchone()["total"]

        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM friends
            WHERE status = 'accepted'
            AND (sender_id = %s OR receiver_id = %s)
            """,
            (session["user_id"], session["user_id"]),
        )
        stats["friends"] = cursor.fetchone()["total"]

    except Error as error:
        flash(f"Database error: {error}")
        profile_user = None

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return render_template("profile.html", profile_user=profile_user, stats=stats, user=get_logged_in_user())


@app.route("/profile/<int:user_id>")
def public_profile(user_id):
    """Show another student's public profile."""
    if not login_required():
        flash("Please login to view profiles.")
        return redirect(url_for("login"))

    if user_id == session["user_id"]:
        return redirect(url_for("profile"))

    profile_user = None
    stats = {"followers": 0, "following": 0, "friends": 0}
    relationship = {"is_following": False, "friend_status": None, "friend_request_id": None}
    posts = []
    comments_by_post = {}

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        profile_user = cursor.fetchone()
        if not profile_user:
            flash("That student could not be found.")
            return redirect(url_for("students"))

        # These SELECT queries count followers, following, and accepted friends.
        cursor.execute("SELECT COUNT(*) AS total FROM followers WHERE following_id = %s", (user_id,))
        stats["followers"] = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) AS total FROM followers WHERE follower_id = %s", (user_id,))
        stats["following"] = cursor.fetchone()["total"]

        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM friends
            WHERE status = 'accepted'
            AND (sender_id = %s OR receiver_id = %s)
            """,
            (user_id, user_id),
        )
        stats["friends"] = cursor.fetchone()["total"]

        # These queries describe how the logged-in student relates to this user.
        cursor.execute(
            "SELECT 1 FROM followers WHERE follower_id = %s AND following_id = %s",
            (session["user_id"], user_id),
        )
        relationship["is_following"] = cursor.fetchone() is not None

        cursor.execute(
            """
            SELECT id, sender_id, status
            FROM friends
            WHERE status IN ('pending', 'accepted')
            AND ((sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s))
            """,
            (session["user_id"], user_id, user_id, session["user_id"]),
        )
        friend_row = cursor.fetchone()
        if friend_row:
            if friend_row["status"] == "accepted":
                relationship["friend_status"] = "accepted"
            elif friend_row["sender_id"] == session["user_id"]:
                relationship["friend_status"] = "request_sent"
            else:
                relationship["friend_status"] = "request_received"
            relationship["friend_request_id"] = friend_row["id"]

        # This SELECT query loads the student's recent posts with counts.
        cursor.execute(
            """
            SELECT
                posts.id,
                posts.user_id,
                posts.content,
                posts.post_type,
                posts.file_path,
                posts.file_type,
                posts.created_at,
                (SELECT COUNT(*) FROM likes WHERE likes.post_id = posts.id) AS like_count,
                (SELECT COUNT(*) FROM comments WHERE comments.post_id = posts.id) AS comment_count,
                (
                    SELECT COUNT(*)
                    FROM likes
                    WHERE likes.post_id = posts.id
                    AND likes.user_id = %s
                ) AS liked_by_user
            FROM posts
            WHERE posts.user_id = %s
            ORDER BY posts.created_at DESC
            LIMIT 20
            """,
            (session["user_id"], user_id),
        )
        posts = cursor.fetchall()

        # This SELECT query loads comments attached to those recent posts.
        if posts:
            post_ids = tuple(post["id"] for post in posts)
            cursor.execute(
                """
                SELECT
                    comments.id,
                    comments.post_id,
                    comments.user_id,
                    comments.comment_text,
                    comments.created_at,
                    users.full_name
                FROM comments
                INNER JOIN users ON comments.user_id = users.id
                WHERE comments.post_id IN %s
                ORDER BY comments.created_at ASC
                """,
                (post_ids,),
            )
            comments = cursor.fetchall()
            for comment in comments:
                comments_by_post.setdefault(comment["post_id"], []).append(comment)

    except Error as error:
        flash(f"Database error: {error}")
        profile_user = None

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    if not profile_user:
        return redirect(url_for("students"))

    return render_template(
        "public_profile.html",
        profile_user=profile_user,
        stats=stats,
        relationship=relationship,
        posts=posts,
        comments_by_post=comments_by_post,
        user=get_logged_in_user(),
    )


@app.route("/students")
def students():
    """Show students so the logged-in user can friend or follow them."""
    if not login_required():
        flash("Please login to view students.")
        return redirect(url_for("login"))

    all_students = []
    pending_requests = []

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        # This SELECT query lists every student except the logged-in student.
        cursor.execute("SELECT id, full_name, email, department, study_year FROM users WHERE id != %s", (session["user_id"],))
        all_students = cursor.fetchall()

        # This SELECT query loads friend requests waiting for the logged-in student.
        cursor.execute(
            """
            SELECT friends.id, users.id AS user_id, users.full_name, users.department, users.study_year
            FROM friends
            INNER JOIN users ON friends.sender_id = users.id
            WHERE friends.receiver_id = %s AND friends.status = 'pending'
            """,
            (session["user_id"],),
        )
        pending_requests = cursor.fetchall()

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return render_template("students.html", students=all_students, pending_requests=pending_requests, user=get_logged_in_user())


@app.route("/send-friend-request/<int:receiver_id>", methods=["POST"])
def send_friend_request(receiver_id):
    """Send a friend request to another student."""
    if not login_required():
        flash("Please login first.")
        return redirect(url_for("login"))
    if not verification_required():
        flash("Please verify your email before sending friend requests.")
        return redirect(url_for("students"))

    if receiver_id == session["user_id"]:
        flash("You cannot send a request to yourself.")
        return redirect_back("students")

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        # This INSERT query creates a pending friend request.
        cursor.execute(
            """
            INSERT INTO friends (sender_id, receiver_id, status)
            VALUES (%s, %s, 'pending')
            ON CONFLICT DO NOTHING
            """,
            (session["user_id"], receiver_id),
        )

        connection.commit()
        create_notification(receiver_id, session["user_id"], "friend_request", f"{session['full_name']} sent you a friend request.")
        flash("Friend request sent.")

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect_back("students")


@app.route("/respond-friend-request/<int:friend_id>/<status>", methods=["POST"])
def respond_friend_request(friend_id, status):
    """Accept or reject a friend request."""
    if not login_required():
        flash("Please login first.")
        return redirect(url_for("login"))

    if status not in ["accepted", "rejected"]:
        flash("Invalid friend request action.")
        return redirect_back("students")

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        # This UPDATE query changes a pending request into accepted or rejected.
        cursor.execute(
            """
            UPDATE friends
            SET status = %s
            WHERE id = %s AND receiver_id = %s
            """,
            (status, friend_id, session["user_id"]),
        )

        # This SELECT query finds the sender for a notification.
        cursor.execute("SELECT sender_id FROM friends WHERE id = %s", (friend_id,))
        friend_request = cursor.fetchone()

        connection.commit()

        if friend_request and status == "accepted":
            create_notification(friend_request["sender_id"], session["user_id"], "friend_accept", f"{session['full_name']} accepted your friend request.")

        flash(f"Friend request {status}.")

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect_back("students")


@app.route("/remove-friend/<int:friend_id>", methods=["POST"])
def remove_friend(friend_id):
    """Remove a friend connection."""
    if not login_required():
        flash("Please login first.")
        return redirect(url_for("login"))

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        # This DELETE query removes an accepted friendship involving the logged-in student.
        cursor.execute(
            """
            DELETE FROM friends
            WHERE id = %s
            AND status = 'accepted'
            AND (sender_id = %s OR receiver_id = %s)
            """,
            (friend_id, session["user_id"], session["user_id"]),
        )

        connection.commit()
        flash("Friend removed.")

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect_back("students")


@app.route("/follow/<int:following_id>", methods=["POST"])
def follow(following_id):
    """Follow another student."""
    if not login_required():
        flash("Please login first.")
        return redirect(url_for("login"))
    if not verification_required():
        flash("Please verify your email before following students.")
        return redirect(url_for("students"))

    if following_id == session["user_id"]:
        flash("You cannot follow yourself.")
        return redirect_back("students")

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        # This INSERT query creates a follow relationship.
        cursor.execute(
            """
            INSERT INTO followers (follower_id, following_id)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
            """,
            (session["user_id"], following_id),
        )

        connection.commit()
        create_notification(following_id, session["user_id"], "follow", f"{session['full_name']} followed you.")
        flash("You are now following this student.")

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect_back("students")


@app.route("/unfollow/<int:following_id>", methods=["POST"])
def unfollow(following_id):
    """Unfollow another student."""
    if not login_required():
        flash("Please login first.")
        return redirect(url_for("login"))

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        # This DELETE query removes a follow relationship.
        cursor.execute(
            "DELETE FROM followers WHERE follower_id = %s AND following_id = %s",
            (session["user_id"], following_id),
        )

        connection.commit()
        flash("You unfollowed this student.")

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect_back("students")


@app.route("/messages")
@app.route("/messages/<int:conversation_id>")
def messages(conversation_id=None):
    """Show the authenticated student's conversation inbox and one chat thread."""
    if not login_required():
        flash("Please login to view messages.")
        return redirect(url_for("login"))

    conversations = []
    chat_messages = []
    selected_conversation = None
    selected_members = []
    available_group_members = []
    users = []

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        if conversation_id:
            # A conversation is never visible unless the current session belongs to it.
            cursor.execute(
                "SELECT 1 FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
                (conversation_id, session["user_id"]),
            )
            if not cursor.fetchone():
                flash("You do not have access to this conversation.")
                return redirect(url_for("messages"))

            # Read state belongs to each participant, so one group member opening a
            # conversation never clears unread counts for every other member.
            cursor.execute(
                "UPDATE conversation_participants SET last_read_at = CURRENT_TIMESTAMP "
                "WHERE conversation_id = %s AND user_id = %s",
                (conversation_id, session["user_id"]),
            )
            cursor.execute(
                "UPDATE messages SET is_read = TRUE WHERE conversation_id = %s "
                "AND (sender_id != %s OR sender_id IS NULL)",
                (conversation_id, session["user_id"]),
            )
            connection.commit()

            cursor.execute(
                """
                SELECT c.id, c.title, c.is_group, c.created_by, c.avatar_path,
                       c.created_at, c.updated_at,
                       CASE
                           WHEN c.is_group THEN COALESCE(c.title, 'Study group')
                           ELSE COALESCE((
                               SELECT u.full_name
                               FROM users u
                               JOIN conversation_participants cp2 ON cp2.user_id = u.id
                               WHERE cp2.conversation_id = c.id AND u.id != %s
                               LIMIT 1
                           ), 'Private conversation')
                       END AS display_name,
                       (
                           SELECT u.department
                           FROM users u
                           JOIN conversation_participants cp2 ON cp2.user_id = u.id
                           WHERE cp2.conversation_id = c.id AND u.id != %s
                           LIMIT 1
                       ) AS other_user_department,
                       (
                           SELECT u.id
                           FROM users u
                           JOIN conversation_participants cp2 ON cp2.user_id = u.id
                           WHERE cp2.conversation_id = c.id AND u.id != %s
                           LIMIT 1
                       ) AS other_user_id,
                       (SELECT u.full_name FROM users u WHERE u.id = c.created_by) AS creator_name
                FROM conversations c
                WHERE c.id = %s
                """,
                (session["user_id"], session["user_id"], session["user_id"], conversation_id),
            )
            selected_conversation = cursor.fetchone()

            cursor.execute(
                """
                SELECT m.*, CASE WHEN m.message_type = 'ai' OR m.sender_id IS NULL
                    THEN 'IVY' ELSE u.full_name END AS sender_name
                FROM messages m
                LEFT JOIN users u ON m.sender_id = u.id
                WHERE m.conversation_id = %s
                ORDER BY m.created_at ASC, m.id ASC
                """,
                (conversation_id,),
            )
            chat_messages = cursor.fetchall()

            cursor.execute(
                """
                SELECT u.id, u.full_name, u.department, u.study_year, u.profile_picture,
                       cp.joined_at, (c.created_by = u.id) AS is_admin
                FROM conversation_participants cp
                JOIN users u ON u.id = cp.user_id
                JOIN conversations c ON c.id = cp.conversation_id
                WHERE cp.conversation_id = %s
                ORDER BY is_admin DESC, u.full_name ASC
                """,
                (conversation_id,),
            )
            selected_members = cursor.fetchall()

            # Group admins need the student picker for the Add-members dialog.
            if selected_conversation["is_group"] and selected_conversation["created_by"] == session["user_id"]:
                cursor.execute(
                    """
                    SELECT id, full_name, department, study_year FROM users
                    WHERE id != %s
                      AND id NOT IN (
                          SELECT user_id FROM conversation_participants WHERE conversation_id = %s
                      )
                    ORDER BY full_name ASC
                    """,
                    (session["user_id"], conversation_id),
                )
                available_group_members = cursor.fetchall()

        # Students are available to the new-chat and group-creation dialogs. Existing
        # permissions allowed messaging any student, so that behavior is retained.
        cursor.execute(
            "SELECT id, full_name, department, study_year, profile_picture FROM users "
            "WHERE id != %s ORDER BY full_name ASC",
            (session["user_id"],),
        )
        users = cursor.fetchall()

        # The sidebar is deliberately fetched after marking the open conversation read.
        cursor.execute(
            """
            SELECT c.id, c.title, c.is_group, c.avatar_path, c.created_at, c.updated_at,
                   CASE
                       WHEN c.is_group THEN COALESCE(c.title, 'Study group')
                       ELSE COALESCE((
                           SELECT u.full_name
                           FROM users u
                           JOIN conversation_participants cp2 ON cp2.user_id = u.id
                           WHERE cp2.conversation_id = c.id AND u.id != %s
                           LIMIT 1
                       ), 'Private conversation')
                   END AS display_name,
                   (
                       SELECT u.profile_picture
                       FROM users u
                       JOIN conversation_participants cp2 ON cp2.user_id = u.id
                       WHERE cp2.conversation_id = c.id AND u.id != %s
                       LIMIT 1
                   ) AS other_user_picture,
                   (
                       SELECT u.id
                       FROM users u
                       JOIN conversation_participants cp2 ON cp2.user_id = u.id
                       WHERE cp2.conversation_id = c.id AND u.id != %s
                       LIMIT 1
                   ) AS other_user_id,
                   (SELECT m.message_text FROM messages m WHERE m.conversation_id = c.id
                    ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_message,
                   (SELECT m.message_type FROM messages m WHERE m.conversation_id = c.id
                    ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_message_type,
                   (SELECT COUNT(*) FROM messages m
                    WHERE m.conversation_id = c.id
                      AND (m.sender_id IS NULL OR m.sender_id != %s)
                      AND m.created_at > COALESCE(cp.last_read_at, '1970-01-01')) AS unread_count
            FROM conversations c
            JOIN conversation_participants cp ON cp.conversation_id = c.id
            WHERE cp.user_id = %s
            ORDER BY c.updated_at DESC, c.id DESC
            """,
            (session["user_id"], session["user_id"], session["user_id"], session["user_id"], session["user_id"]),
        )
        conversations = cursor.fetchall()

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return render_template(
        "messages.html",
        conversations=conversations,
        selected_conversation=selected_conversation,
        selected_members=selected_members,
        available_group_members=available_group_members,
        chat_messages=chat_messages,
        users=users,
        user=get_logged_in_user(),
    )


@app.route("/conversations/new", methods=["POST"])
def new_conversation():
    """Start a new 1-on-1 chat or open existing one."""
    if not login_required():
        flash("Please login to send messages.")
        return redirect(url_for("login"))
    if not verification_required():
        flash("Please verify your email before starting chats.")
        return redirect(url_for("messages"))

    target_user_id = request.form.get("user_id", type=int)
    if not target_user_id or target_user_id == session["user_id"]:
        flash("Choose another student to start a chat.")
        return redirect(url_for("messages"))

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        cursor.execute("SELECT id FROM users WHERE id = %s", (target_user_id,))
        if not cursor.fetchone():
            flash("That student is no longer available.")
            return redirect(url_for("messages"))

        # A private thread must have exactly these two participants. This prevents
        # duplicate conversations during normal repeated New Chat submissions.
        cursor.execute(
            """
            SELECT c.id
            FROM conversations c
            WHERE c.is_group = FALSE
              AND EXISTS (SELECT 1 FROM conversation_participants cp1
                          WHERE cp1.conversation_id = c.id AND cp1.user_id = %s)
              AND EXISTS (SELECT 1 FROM conversation_participants cp2
                          WHERE cp2.conversation_id = c.id AND cp2.user_id = %s)
              AND (SELECT COUNT(*) FROM conversation_participants cp3
                   WHERE cp3.conversation_id = c.id) = 2
            LIMIT 1
            """,
            (session["user_id"], target_user_id),
        )
        existing_conv = cursor.fetchone()

        if existing_conv:
            return redirect(url_for("messages", conversation_id=existing_conv['id']))

        cursor.execute(
            "INSERT INTO conversations (is_group, created_by) VALUES (FALSE, %s) RETURNING id",
            (session["user_id"],),
        )
        conv_id = cursor.fetchone()["id"]
        cursor.execute("INSERT INTO conversation_participants (conversation_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (conv_id, session["user_id"]))
        cursor.execute("INSERT INTO conversation_participants (conversation_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (conv_id, target_user_id))
        connection.commit()
        return redirect(url_for("messages", conversation_id=conv_id))

    except Error as error:
        flash(f"Database error: {error}")
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect(url_for("messages"))


@app.route("/conversations/group", methods=["POST"])
def create_group_conversation():
    """Create a group and make the creator its first administrator."""
    if not login_required():
        flash("Please login to create a group.")
        return redirect(url_for("login"))
    if not verification_required():
        flash("Please verify your email before creating groups.")
        return redirect(url_for("messages"))

    title = (request.form.get("title") or "").strip()
    member_ids = {member_id for member_id in request.form.getlist("member_ids", type=int)
                  if member_id and member_id != session["user_id"]}
    group_image = request.files.get("group_image")

    if not title or len(title) > 100:
        flash("Give the group a name between 1 and 100 characters.")
        return redirect(url_for("messages"))
    if not member_ids:
        flash("Choose at least one student for the group.")
        return redirect(url_for("messages"))
    if group_image and group_image.filename and not allowed_image(group_image.filename):
        flash("Group images must be PNG, JPG, JPEG, or GIF files.")
        return redirect(url_for("messages"))

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        placeholders = ", ".join(["%s"] * len(member_ids))
        cursor.execute(f"SELECT id FROM users WHERE id IN ({placeholders})", tuple(member_ids))
        confirmed_ids = {student["id"] for student in cursor.fetchall()}
        if confirmed_ids != member_ids:
            flash("One or more selected students are unavailable.")
            return redirect(url_for("messages"))

        avatar_path = None
        if group_image and group_image.filename:
            avatar_path = save_uploaded_file(group_image, f"group_{session['user_id']}", STORAGE_BUCKETS["chat"])

        cursor.execute(
            "INSERT INTO conversations (title, is_group, created_by, avatar_path) "
            "VALUES (%s, TRUE, %s, %s) RETURNING id",
            (title, session["user_id"], avatar_path),
        )
        new_conversation_id = cursor.fetchone()["id"]
        participant_ids = [session["user_id"], *sorted(confirmed_ids)]
        cursor.executemany(
            "INSERT INTO conversation_participants (conversation_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            [(new_conversation_id, user_id) for user_id in participant_ids],
        )
        connection.commit()
        flash("Group created. Start the conversation whenever you are ready.")
        return redirect(url_for("messages", conversation_id=new_conversation_id))
    except Error as error:
        flash(f"Database error: {error}")
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect(url_for("messages"))


@app.route("/conversations/<int:conversation_id>/members", methods=["POST"])
def add_group_members(conversation_id):
    """Allow only the group creator to add available students to their group."""
    if not login_required():
        flash("Please login to manage a group.")
        return redirect(url_for("login"))

    member_ids = {member_id for member_id in request.form.getlist("member_ids", type=int)
                  if member_id and member_id != session["user_id"]}
    if not member_ids:
        flash("Choose at least one student to add.")
        return redirect(url_for("messages", conversation_id=conversation_id))

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        cursor.execute(
            "SELECT id FROM conversations WHERE id = %s AND is_group = TRUE AND created_by = %s",
            (conversation_id, session["user_id"]),
        )
        if not cursor.fetchone():
            flash("Only this group's creator can add members.")
            return redirect(url_for("messages", conversation_id=conversation_id))

        placeholders = ", ".join(["%s"] * len(member_ids))
        cursor.execute(f"SELECT id FROM users WHERE id IN ({placeholders})", tuple(member_ids))
        valid_member_ids = [student["id"] for student in cursor.fetchall()]
        cursor.executemany(
            "INSERT INTO conversation_participants (conversation_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            [(conversation_id, user_id) for user_id in valid_member_ids],
        )
        connection.commit()
        flash("Selected students were added to the group.")
    except Error as error:
        flash(f"Database error: {error}")
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect(url_for("messages", conversation_id=conversation_id))


def emit_conversation_update(conversation_id, message_text, message_type, sender_id, participant_ids):
    """Refresh just the affected sidebar entry for every participant's inbox."""
    for participant_id in participant_ids:
        socketio.emit(
            "conversation_updated",
            {
                "conversation_id": conversation_id,
                "message_text": message_text,
                "message_type": message_type,
                "sender_id": sender_id,
                "is_unread": sender_id is None or sender_id != participant_id,
                "updated_at": datetime.now().isoformat(),
            },
            room=f"user_{participant_id}",
        )


def save_and_emit_ivy_message(conversation_id, message_text):
    """Store IVY as a normal AI message and broadcast it to the authorised room."""
    connection = None
    cursor = None
    participant_ids = []
    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        cursor.execute(
            "INSERT INTO messages (conversation_id, sender_id, message_text, message_type) "
            "VALUES (%s, NULL, %s, 'ai')",
            (conversation_id, message_text),
        )
        cursor.execute("UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s", (conversation_id,))
        cursor.execute(
            "SELECT user_id FROM conversation_participants WHERE conversation_id = %s",
            (conversation_id,),
        )
        participant_ids = [participant["user_id"] for participant in cursor.fetchall()]
        connection.commit()
    except Error as error:
        print(f"IVY message persistence error: {error}")
        return
    finally:
        if cursor:
            cursor.close()
        if connection and not connection.closed:
            connection.close()

    payload = {
        "sender_id": None,
        "sender_name": "IVY",
        "message_text": message_text,
        "message_type": "ai",
        "conversation_id": conversation_id,
        "created_at": datetime.now().isoformat(),
    }
    socketio.emit("receive_chat_message", payload, room=f"conversation_{conversation_id}")
    emit_conversation_update(conversation_id, message_text, "ai", None, participant_ids)


def recent_conversation_transcript(conversation_id):
    """Return a small ordered window of recent messages for IVY context."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        cursor.execute(
            """
            SELECT m.message_text,
                   CASE WHEN m.message_type = 'ai' OR m.sender_id IS NULL
                        THEN 'IVY' ELSE u.full_name END AS sender_name
            FROM messages m
            LEFT JOIN users u ON u.id = m.sender_id
            WHERE m.conversation_id = %s
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT %s
            """,
            (conversation_id, IVY_CONTEXT_MESSAGE_LIMIT),
        )
        return list(reversed(cursor.fetchall()))
    except Error as error:
        print(f"IVY context lookup error: {error}")
        return []
    finally:
        if cursor:
            cursor.close()
        if connection and not connection.closed:
            connection.close()


def build_ivy_prompt(transcript_messages, message_text, sender_name):
    """Compose the shared IVY prompt used by chat threads and learning rooms."""
    transcript = "\n".join(
        f"{message['sender_name']}: {message['message_text']}" for message in transcript_messages
    )
    return f"""
You are IVY, the friendly AI learning assistant for PeerVerse.
Use the recent conversation only to resolve context and help the students learn.
Answer the latest @ivy request directly, accurately, and concisely. Do not pretend to be a student.

Recent conversation:
{transcript or 'No prior messages.'}

Latest request from {sender_name}:
{message_text}
""".strip()


def process_ivy_mention(conversation_id, message_text, sender_name):
    """Use the existing Groq helper with a small, ordered conversation window."""
    prompt = build_ivy_prompt(recent_conversation_transcript(conversation_id), message_text, sender_name)

    try:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("AI service is not configured")
        client = Groq(api_key=api_key)
        configured_model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
        answer = generate_ai_response(client, prompt, configured_model)
        save_and_emit_ivy_message(conversation_id, answer or "IVY is temporarily unavailable. Please try again.")
    except Exception as error:
        # Keep provider details and credentials server-side; normal chat is unaffected.
        print(f"IVY mention error: {error}")
        save_and_emit_ivy_message(conversation_id, "IVY is temporarily unavailable. Please try again.")


@socketio.on("join_chat")
def join_chat(data):
    """Join an authorised conversation room; arbitrary room names are rejected."""
    if not login_required():
        emit("chat_error", {"message": "Please sign in to use chat."})
        return

    conversation_id = socket_conversation_id(data)
    if not conversation_id:
        emit("chat_error", {"message": "Invalid conversation."})
        return

    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
            (conversation_id, session["user_id"]),
        )
        allowed = cursor.fetchone()
    except Error:
        allowed = None
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    if not allowed:
        emit("chat_error", {"message": "You do not have access to this conversation."})
        return

    join_room(f"conversation_{conversation_id}")
    emit("chat_joined", {"conversation_id": conversation_id})


@socketio.on("join_inbox")
def join_inbox():
    """Join the current user's private inbox room and mark them online."""
    if login_required():
        join_room(f"user_{session['user_id']}")
        user_id = session["user_id"]
        SOCKET_USER[request.sid] = user_id
        mark_user_online(user_id)
        update_last_seen(user_id)


@socketio.on("send_chat_message")
def send_chat_message(data):
    """Save a message from the authenticated session and broadcast only its room."""
    if not login_required():
        emit("chat_error", {"message": "Please sign in to send messages."})
        return

    conversation_id = socket_conversation_id(data)
    message_text = (data.get("message_text") or "").strip() if isinstance(data, dict) else ""
    sender_id = session["user_id"]
    sender_name = session["full_name"]

    if not conversation_id or not message_text:
        emit("chat_error", {"message": "A message cannot be empty."})
        return
    if len(message_text) > MAX_CHAT_MESSAGE_LENGTH:
        emit("chat_error", {"message": f"Messages can be up to {MAX_CHAT_MESSAGE_LENGTH} characters."})
        return

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        cursor.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
            (conversation_id, sender_id),
        )
        if not cursor.fetchone():
            emit("chat_error", {"message": "You do not have access to this conversation."})
            return

        cursor.execute(
            """
            INSERT INTO messages (conversation_id, sender_id, message_text, message_type)
            VALUES (%s, %s, %s, 'text')
            """,
            (conversation_id, sender_id, message_text),
        )
        cursor.execute("UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s", (conversation_id,))
        cursor.execute(
            "SELECT user_id FROM conversation_participants WHERE conversation_id = %s",
            (conversation_id,),
        )
        participant_ids = [participant["user_id"] for participant in cursor.fetchall()]
        connection.commit()
    except Error as error:
        print(f"Socket DB error: {error}")
        emit("chat_error", {"message": "Your message could not be sent. Please try again."})
        return

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    for participant_id in participant_ids:
        if participant_id != sender_id:
            create_notification(
                participant_id,
                sender_id,
                "message",
                f"{sender_name} sent a message in your conversation.",
            )

    payload = {
        "sender_id": sender_id,
        "sender_name": sender_name,
        "message_text": message_text,
        "message_type": "text",
        "conversation_id": conversation_id,
        "created_at": datetime.now().isoformat(),
    }
    socketio.emit("receive_chat_message", payload, room=f"conversation_{conversation_id}")
    emit_conversation_update(conversation_id, message_text, "text", sender_id, participant_ids)

    if re.search(r"(?<!\w)@ivy\b", message_text, flags=re.IGNORECASE):
        socketio.start_background_task(process_ivy_mention, conversation_id, message_text, sender_name)


@socketio.on("mark_chat_read")
def mark_chat_read(data):
    """Mark only the current participant's read position for an authorised thread."""
    if not login_required():
        return
    conversation_id = socket_conversation_id(data)
    if not conversation_id:
        return
    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE conversation_participants SET last_read_at = CURRENT_TIMESTAMP "
            "WHERE conversation_id = %s AND user_id = %s",
            (conversation_id, session["user_id"]),
        )
        cursor.execute(
            "UPDATE messages SET is_read = TRUE WHERE conversation_id = %s "
            "AND (sender_id != %s OR sender_id IS NULL)",
            (conversation_id, session["user_id"]),
        )
        connection.commit()
    except Error:
        pass
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

# ---------------------------------------------------------------------------
# Video Learning Rooms.
# Socket.IO carries signaling and room chat only; WebRTC carries all media
# directly between browsers. Room chat is stored in the existing `messages`
# table so conversation history and @ivy context stay unified.
# ---------------------------------------------------------------------------

CALL_ROOM_SOCKETS = {}  # room_id -> {user_id: set(socket sid)} for live presence


def valid_call_room_id(room_id):
    """Room ids are generated server-side; reject anything else early."""
    return isinstance(room_id, str) and 8 <= len(room_id) <= 64 and bool(re.fullmatch(r"[A-Za-z0-9_-]+", room_id))


def build_ice_servers():
    """Build RTCIceServer configuration from environment variables.

    TURN credentials are read from the server environment only, never hard-coded,
    and are handed to authenticated callers at runtime (browsers need them to use
    the TURN relay; nothing else about Supabase or Groq is exposed here).
    """
    def split_urls(raw):
        return [url.strip() for url in (raw or "").split(",") if url.strip()]

    ice_servers = []
    stun_urls = split_urls(os.environ.get("STUN_SERVER"))
    if stun_urls:
        ice_servers.append({"urls": stun_urls})

    turn_urls = split_urls(os.environ.get("TURN_SERVER"))
    if turn_urls:
        turn_entry = {"urls": turn_urls}
        turn_username = os.environ.get("TURN_USERNAME", "").strip()
        turn_password = os.environ.get("TURN_PASSWORD", "")
        if turn_username:
            turn_entry["username"] = turn_username
        if turn_password:
            turn_entry["credential"] = turn_password
        ice_servers.append(turn_entry)

    if not ice_servers:
        # Local-development fallback only; production should set STUN_SERVER/TURN_SERVER.
        ice_servers.append({"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]})
    return ice_servers


@app.route("/api/call/ice")
def call_ice_config():
    """Authenticated endpoint that returns STUN/TURN servers for WebRTC."""
    if not login_required():
        return {"error": "Please sign in to join learning rooms."}, 401
    return {"ice_servers": build_ice_servers()}


def user_is_conversation_member(user_id, conversation_id):
    """Server-side membership check; browser-supplied ids are never trusted."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
            (conversation_id, user_id),
        )
        return cursor.fetchone() is not None
    except Error:
        return False
    finally:
        if cursor:
            cursor.close()
        if connection and not connection.closed:
            connection.close()


def load_call_room(room_id):
    """Load one learning room together with a display name for this viewer."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        cursor.execute(
            """
            SELECT cr.id, cr.conversation_id, cr.created_by, cr.status,
                   cr.started_at, cr.ended_at,
                   c.is_group, c.title AS conversation_title,
                   u.full_name AS creator_name,
                   CASE WHEN c.is_group THEN COALESCE(c.title, 'Study group')
                        ELSE COALESCE((
                            SELECT u2.full_name
                            FROM users u2
                            JOIN conversation_participants cp2 ON cp2.user_id = u2.id
                            WHERE cp2.conversation_id = c.id AND u2.id != %s
                            LIMIT 1
                        ), 'Private conversation')
                   END AS display_name
            FROM call_rooms cr
            JOIN conversations c ON c.id = cr.conversation_id
            LEFT JOIN users u ON u.id = cr.created_by
            WHERE cr.id = %s
            """,
            (session["user_id"], room_id),
        )
        return cursor.fetchone()
    except Error as error:
        print(f"Call room lookup error: {error}")
        return None
    finally:
        if cursor:
            cursor.close()
        if connection and not connection.closed:
            connection.close()


@app.route("/calls/start/<int:conversation_id>", methods=["POST"])
def start_call_room(conversation_id):
    """Create (or rejoin) a learning room for an authorised conversation."""
    if not login_required():
        flash("Please login before starting a call.")
        return redirect(url_for("login"))
    if not verification_required():
        flash("Please verify your email before starting a video call.")
        return redirect(url_for("messages"))

    if not user_is_conversation_member(session["user_id"], conversation_id):
        flash("You can only start calls in your own conversations.")
        return redirect(url_for("messages"))

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        # Reuse an already-active room so double clicks never split the group.
        cursor.execute(
            "SELECT id FROM call_rooms WHERE conversation_id = %s AND status != 'ended' "
            "ORDER BY started_at DESC LIMIT 1",
            (conversation_id,),
        )
        existing_room = cursor.fetchone()

        if existing_room:
            room_id = existing_room["id"]
        else:
            room_id = secrets.token_hex(16)
            cursor.execute(
                "INSERT INTO call_rooms (id, conversation_id, created_by, status) "
                "VALUES (%s, %s, %s, 'active')",
                (room_id, conversation_id, session["user_id"]),
            )
            connection.commit()

            # Invite every other participant through their personal inbox room.
            cursor.execute(
                "SELECT user_id FROM conversation_participants WHERE conversation_id = %s AND user_id != %s",
                (conversation_id, session["user_id"]),
            )
            invitees = [row["user_id"] for row in cursor.fetchall()]
            for invitee_id in invitees:
                socketio.emit(
                    "call_invitation",
                    {
                        "room_id": room_id,
                        "conversation_id": conversation_id,
                        "caller_name": session["full_name"],
                    },
                    room=f"user_{invitee_id}",
                )

        return redirect(url_for("call_room", room_id=room_id))

    except Error as error:
        flash(f"Database error: {error}")
        return redirect(url_for("messages", conversation_id=conversation_id))
    finally:
        if cursor:
            cursor.close()
        if connection and not connection.closed:
            connection.close()


@app.route("/call/<room_id>")
def call_room(room_id):
    """Dedicated full-page Video Learning Room for authorised participants."""
    if not login_required():
        flash("Please login to join the learning room.")
        return redirect(url_for("login"))

    if not valid_call_room_id(room_id):
        flash("That learning room link is not valid.")
        return redirect(url_for("messages"))

    room = load_call_room(room_id)
    if not room:
        flash("This learning room no longer exists.")
        return redirect(url_for("messages"))

    if not user_is_conversation_member(session["user_id"], room["conversation_id"]):
        flash("Only conversation participants can join this learning room.")
        return redirect(url_for("messages"))

    recent_messages = []
    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        # Persistent room chat: last messages of the underlying conversation.
        cursor.execute(
            """
            SELECT m.*, CASE WHEN m.message_type = 'ai' OR m.sender_id IS NULL
                THEN 'IVY' ELSE u.full_name END AS sender_name
            FROM messages m
            LEFT JOIN users u ON m.sender_id = u.id
            WHERE m.conversation_id = %s
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT 50
            """,
            (room["conversation_id"],),
        )
        recent_messages = list(reversed(cursor.fetchall()))
    except Error:
        recent_messages = []
    finally:
        if "connection" in locals() and connection and not connection.closed:
            if "cursor" in locals() and cursor:
                cursor.close()
            connection.close()

    return render_template(
        "call_room.html",
        room=room,
        recent_messages=recent_messages,
        user=get_logged_in_user(),
    )


def live_call_participants(room_id):
    """Return display names for everyone currently connected to a room."""
    live_ids = sorted(CALL_ROOM_SOCKETS.get(room_id, {}).keys())
    if not live_ids:
        return []
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        placeholders = ", ".join(["%s"] * len(live_ids))
        cursor.execute(f"SELECT id, full_name FROM users WHERE id IN ({placeholders})", tuple(live_ids))
        names = {row["id"]: row["full_name"] for row in cursor.fetchall()}
        return [{"user_id": user_id, "user_name": names.get(user_id, f"Student {user_id}")} for user_id in live_ids]
    except Error:
        return [{"user_id": user_id, "user_name": f"Student {user_id}"} for user_id in live_ids]
    finally:
        if cursor:
            cursor.close()
        if connection and not connection.closed:
            connection.close()


def mark_call_presence(room_id, user_id):
    """Upsert the participant row each time they (re)connect to the room."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO call_room_participants (room_id, user_id)
            VALUES (%s, %s)
            ON CONFLICT (room_id, user_id)
            DO UPDATE SET left_at = NULL
            """,
            (room_id, user_id),
        )
        connection.commit()
    except Error:
        pass
    finally:
        if cursor:
            cursor.close()
        if connection and not connection.closed:
            connection.close()


def end_call_room(room_id):
    """Close a room for everyone: persist metadata and broadcast once."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE call_rooms SET status = 'ended', ended_at = CURRENT_TIMESTAMP "
            "WHERE id = %s AND status != 'ended'",
            (room_id,),
        )
        cursor.execute(
            "UPDATE call_room_participants SET left_at = COALESCE(left_at, CURRENT_TIMESTAMP) "
            "WHERE room_id = %s",
            (room_id,),
        )
        connection.commit()
    except Error as error:
        print(f"Call room close error: {error}")
    finally:
        if cursor:
            cursor.close()
        if connection and not connection.closed:
            connection.close()

    CALL_ROOM_SOCKETS.pop(room_id, None)
    socketio.emit("call_ended", {"room_id": room_id}, room=f"call_{room_id}")


def release_call_socket(room_id, sid):
    """Drop one socket from its room and clean up when people or rooms empty out."""
    room_sockets = CALL_ROOM_SOCKETS.get(room_id)
    if not room_sockets:
        return

    user_id = None
    for candidate_user_id, sids in list(room_sockets.items()):
        if sid in sids:
            sids.discard(sid)
            user_id = candidate_user_id
            break

    if user_id is None:
        return

    if not room_sockets.get(user_id):
        del room_sockets[user_id]
        socketio.emit(
            "call_peer_left",
            {"room_id": room_id, "user_id": user_id},
            room=f"call_{room_id}",
        )

    if not room_sockets:
        del CALL_ROOM_SOCKETS[room_id]
        end_call_room(room_id)


@socketio.on("join_call")
def join_call(data):
    """Join an authorised learning room after validating session + membership."""
    if not login_required():
        emit("call_error", {"message": "Please sign in to join learning rooms."})
        return

    room_id = data.get("room_id") if isinstance(data, dict) else None
    if not valid_call_room_id(room_id):
        emit("call_error", {"message": "Invalid learning room."})
        return

    room = load_call_room(room_id)
    if not room:
        emit("call_error", {"message": "This learning room no longer exists."})
        return
    if room["status"] == "ended":
        emit("call_error", {"message": "This call has already ended."})
        return
    if not user_is_conversation_member(session["user_id"], room["conversation_id"]):
        emit("call_error", {"message": "Only conversation participants can join this call."})
        return

    join_room(f"call_{room_id}")

    room_sockets = CALL_ROOM_SOCKETS.setdefault(room_id, {})
    is_new_participant = len(room_sockets.get(session["user_id"], ())) == 0
    room_sockets.setdefault(session["user_id"], set()).add(request.sid)

    mark_call_presence(room_id, session["user_id"])

    participants = live_call_participants(room_id)
    emit("call_joined", {
        "room_id": room_id,
        "you": session["user_id"],
        "status": room["status"],
        "participants": participants,
    })

    if is_new_participant:
        socketio.emit(
            "call_peer_joined",
            {
                "room_id": room_id,
                "user_id": session["user_id"],
                "user_name": session["full_name"],
            },
            room=f"call_{room_id}",
            skip_sid=request.sid,
        )


@socketio.on("call_signal")
def call_signal(data):
    """Relay one SDP/ICE signal between two validated room participants."""
    if not login_required() or not isinstance(data, dict):
        return

    room_id = data.get("room_id")
    kind = data.get("kind")
    target_user_id = data.get("target_user_id")

    if not valid_call_room_id(room_id) or kind not in ("offer", "answer", "candidate"):
        return
    if not isinstance(target_user_id, int):
        return

    room = load_call_room(room_id)
    if not room or room["status"] == "ended":
        return
    sender_id = session["user_id"]
    if not user_is_conversation_member(sender_id, room["conversation_id"]):
        return
    if not user_is_conversation_member(target_user_id, room["conversation_id"]):
        return

    payload = {
        "room_id": room_id,
        "kind": kind,
        "sender_id": sender_id,
        "sender_name": session["full_name"],
    }

    if kind == "candidate":
        candidate = data.get("candidate")
        if not isinstance(candidate, dict):
            return
        payload["candidate"] = candidate
    else:
        sdp = data.get("sdp")
        sdp_type = data.get("type")
        if not isinstance(sdp, str) or sdp_type not in ("offer", "answer"):
            return
        payload["description"] = {"type": sdp_type, "sdp": sdp}

    for target_sid in list(CALL_ROOM_SOCKETS.get(room_id, {}).get(target_user_id, ())):
        if target_sid != request.sid:
            socketio.emit("call_signal", payload, room=target_sid)


@socketio.on("call_media_state")
def call_media_state(data):
    """Share mic/camera/screen flags so tiles show accurate state badges."""
    if not login_required() or not isinstance(data, dict):
        return

    room_id = data.get("room_id")
    media = data.get("media")
    if not valid_call_room_id(room_id) or not isinstance(media, dict):
        return
    if room_id not in CALL_ROOM_SOCKETS or request.sid not in {
        sid for sids in CALL_ROOM_SOCKETS[room_id].values() for sid in sids
    }:
        return

    socketio.emit(
        "call_media_state",
        {
            "room_id": room_id,
            "user_id": session["user_id"],
            "media": {
                "mic": bool(media.get("mic", True)),
                "cam": bool(media.get("cam", True)),
                "screen": bool(media.get("screen", False)),
            },
        },
        room=f"call_{room_id}",
        skip_sid=request.sid,
    )


@socketio.on("leave_call")
def leave_call(data):
    """Leave the room without ending it for everyone else."""
    room_id = data.get("room_id") if isinstance(data, dict) else None
    if valid_call_room_id(room_id):
        release_call_socket(room_id, request.sid)
        leave_room(f"call_{room_id}")


@socketio.on("end_call")
def end_call(data):
    """End the whole room. Any participant may hang up for everyone."""
    if not login_required():
        return

    room_id = data.get("room_id") if isinstance(data, dict) else None
    if not valid_call_room_id(room_id):
        return

    room = load_call_room(room_id)
    if not room or room["status"] == "ended":
        return
    if not user_is_conversation_member(session["user_id"], room["conversation_id"]):
        return

    end_call_room(room_id)


@socketio.on("disconnect")
def handle_call_disconnect():
    """Clean up presence when any socket disappears without a goodbye."""
    for room_id in list(CALL_ROOM_SOCKETS.keys()):
        release_call_socket(room_id, request.sid)
    user_id = SOCKET_USER.pop(request.sid, None)
    if user_id is not None and user_id not in SOCKET_USER.values():
        mark_user_offline(user_id)


def save_and_emit_call_ivy_message(room_id, conversation_id, message_text):
    """Store one IVY answer and broadcast the identical message to every participant."""
    connection = None
    cursor = None
    participant_ids = []
    persisted = False
    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        cursor.execute(
            "INSERT INTO messages (conversation_id, sender_id, message_text, message_type) "
            "VALUES (%s, NULL, %s, 'ai')",
            (conversation_id, message_text),
        )
        cursor.execute("UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s", (conversation_id,))
        cursor.execute(
            "SELECT user_id FROM conversation_participants WHERE conversation_id = %s",
            (conversation_id,),
        )
        participant_ids = [participant["user_id"] for participant in cursor.fetchall()]
        connection.commit()
        persisted = True
    except Error as error:
        print(f"IVY room message persistence error: {error}")
    finally:
        if cursor:
            cursor.close()
        if connection and not connection.closed:
            connection.close()

    payload = {
        "room_id": room_id,
        "sender_id": None,
        "sender_name": "IVY",
        "message_text": message_text,
        "message_type": "ai",
        "created_at": datetime.now().isoformat(),
    }
    # One insert + one fan-out per surface: everyone sees exactly the same response.
    # The room broadcast happens even if persistence failed so no client is left
    # waiting on the "IVY is thinking" indicator forever.
    socketio.emit("receive_call_message", payload, room=f"call_{room_id}")
    if persisted:
        socketio.emit("receive_chat_message", {**payload, "conversation_id": conversation_id},
                      room=f"conversation_{conversation_id}")
        emit_conversation_update(conversation_id, message_text, "ai", None, participant_ids)


def process_ivy_room_mention(room_id, conversation_id, message_text, sender_name):
    """Answer @ivy inside a learning room using the shared IVY implementation."""
    prompt = build_ivy_prompt(recent_conversation_transcript(conversation_id), message_text, sender_name)

    try:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("AI service is not configured")
        client = Groq(api_key=api_key)
        configured_model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
        answer = generate_ai_response(client, prompt, configured_model)
        save_and_emit_call_ivy_message(room_id, conversation_id, answer or "IVY is temporarily unavailable. Please try again.")
    except Exception as error:
        print(f"IVY room mention error: {error}")
        save_and_emit_call_ivy_message(room_id, conversation_id, "IVY is temporarily unavailable. Please try again.")


@socketio.on("send_call_message")
def send_call_message(data):
    """Shared room chat: persist to Supabase, broadcast live, detect @ivy."""
    if not login_required():
        emit("call_error", {"message": "Please sign in to use room chat."})
        return

    room_id = data.get("room_id") if isinstance(data, dict) else None
    message_text = (data.get("message_text") or "").strip() if isinstance(data, dict) else ""

    if not valid_call_room_id(room_id) or not message_text:
        emit("call_error", {"message": "A message cannot be empty."})
        return
    if len(message_text) > MAX_CHAT_MESSAGE_LENGTH:
        emit("call_error", {"message": f"Messages can be up to {MAX_CHAT_MESSAGE_LENGTH} characters."})
        return

    room = load_call_room(room_id)
    if not room or room["status"] == "ended":
        emit("call_error", {"message": "This call has ended."})
        return
    if not user_is_conversation_member(session["user_id"], room["conversation_id"]):
        emit("call_error", {"message": "You do not have access to this room."})
        return

    sender_id = session["user_id"]
    sender_name = session["full_name"]
    conversation_id = room["conversation_id"]

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        cursor.execute(
            """
            INSERT INTO messages (conversation_id, sender_id, message_text, message_type)
            VALUES (%s, %s, %s, 'text')
            """,
            (conversation_id, sender_id, message_text),
        )
        cursor.execute("UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s", (conversation_id,))
        cursor.execute(
            "SELECT user_id FROM conversation_participants WHERE conversation_id = %s",
            (conversation_id,),
        )
        participant_ids = [participant["user_id"] for participant in cursor.fetchall()]
        connection.commit()
    except Error as error:
        print(f"Call chat DB error: {error}")
        emit("call_error", {"message": "Your message could not be sent. Please try again."})
        return
    finally:
        if "connection" in locals() and connection and not connection.closed:
            if "cursor" in locals() and cursor:
                cursor.close()
            connection.close()

    payload = {
        "room_id": room_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "message_text": message_text,
        "message_type": "text",
        "created_at": datetime.now().isoformat(),
    }
    socketio.emit("receive_call_message", payload, room=f"call_{room_id}")
    emit_conversation_update(conversation_id, message_text, "text", sender_id, participant_ids)

    if re.search(r"(?<!\w)@ivy\b", message_text, flags=re.IGNORECASE):
        socketio.emit("ivy_thinking", {"room_id": room_id}, room=f"call_{room_id}")
        socketio.start_background_task(process_ivy_room_mention, room_id, conversation_id, message_text, sender_name)



@app.route("/notifications")
def notifications():
    """Show notifications for the logged-in student."""
    if not login_required():
        flash("Please login to view notifications.")
        return redirect(url_for("login"))

    notification_list = []

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        # This SELECT query loads notifications for the logged-in student.
        cursor.execute(
            """
            SELECT notifications.*, users.full_name AS actor_name
            FROM notifications
            LEFT JOIN users ON notifications.actor_id = users.id
            WHERE notifications.user_id = %s
            ORDER BY notifications.created_at DESC
            """,
            (session["user_id"],),
        )
        notification_list = cursor.fetchall()

        # This UPDATE query marks notifications as read after viewing.
        cursor.execute("UPDATE notifications SET is_read = TRUE WHERE user_id = %s", (session["user_id"],))
        connection.commit()

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return render_template("notifications.html", notifications=notification_list, user=get_logged_in_user())


@app.route("/notifications/delete/<int:notification_id>", methods=["POST"])
def delete_notification(notification_id):
    """Remove a single notification belonging to the logged-in student."""
    if not login_required():
        flash("Please login first.")
        return redirect(url_for("login"))

    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute(
            "DELETE FROM notifications WHERE id = %s AND user_id = %s",
            (notification_id, session["user_id"]),
        )
        connection.commit()
    except Error as error:
        flash(f"Database error: {error}")
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect(url_for("notifications"))


@app.route("/notifications/clear-all", methods=["POST"])
def clear_all_notifications():
    """Delete every notification belonging to the logged-in student."""
    if not login_required():
        flash("Please login first.")
        return redirect(url_for("login"))

    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute("DELETE FROM notifications WHERE user_id = %s", (session["user_id"],))
        connection.commit()
        flash("All notifications cleared.")
    except Error as error:
        flash(f"Database error: {error}")
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return redirect(url_for("notifications"))


@app.route("/resources", methods=["GET", "POST"])
def resources():
    """Upload and list shared learning resources."""
    if not login_required():
        flash("Please login to view resources.")
        return redirect(url_for("login"))
    if request.method == "POST" and not verification_required():
        flash("Please verify your email before sharing resources.")
        return redirect(url_for("resources"))

    if request.method == "POST":
        title = request.form.get("title")
        description = request.form.get("description")
        resource_type = request.form.get("resource_type")
        link_url = request.form.get("link_url")
        resource_file = request.files.get("resource_file")
        file_path = None

        if resource_file and resource_file.filename:
            if allowed_file(resource_file.filename):
                file_path = save_uploaded_file(resource_file, f"user_{session['user_id']}_resource", STORAGE_BUCKETS["resource"])
            else:
                flash("Only image, PDF, and PPT files are allowed.")
                return redirect(url_for("resources"))

        try:
            connection = get_db_connection()
            cursor = connection.cursor()

            # This INSERT query saves a shared resource.
            cursor.execute(
                """
                INSERT INTO resources (user_id, title, description, resource_type, file_path, link_url)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (session["user_id"], title, description, resource_type, file_path, link_url),
            )

            connection.commit()
            flash("Resource shared.")

        except Error as error:
            flash(f"Database error: {error}")

        finally:
            if "connection" in locals() and not connection.closed:
                if "cursor" in locals():
                    cursor.close()
                connection.close()

        return redirect(url_for("resources"))

    resource_list = []

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        # This SELECT query lists resources with the uploader name.
        cursor.execute(
            """
            SELECT resources.*, users.full_name
            FROM resources
            INNER JOIN users ON resources.user_id = users.id
            ORDER BY resources.created_at DESC
            """
        )
        resource_list = cursor.fetchall()

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return render_template("resources.html", resources=resource_list, user=get_logged_in_user())


@app.route("/search")
def search():
    """Search students, posts, and resources."""
    if not login_required():
        flash("Please login to search.")
        return redirect(url_for("login"))

    query = request.args.get("q", "")
    students_result = []
    posts_result = []
    resources_result = []

    if query:
        search_text = f"%{query}%"

        try:
            connection = get_db_connection()
            cursor = db_cursor(connection, dictionary=True)

            # This SELECT query searches students by name, department, skills, and interests.
            cursor.execute(
                """
                SELECT id, full_name, department, study_year, skills, interests
                FROM users
                WHERE full_name ILIKE %s
                   OR department ILIKE %s
                   OR skills ILIKE %s
                   OR interests ILIKE %s
                """,
                (search_text, search_text, search_text, search_text),
            )
            students_result = cursor.fetchall()

            # This SELECT query searches post content.
            cursor.execute(
                """
                SELECT posts.*, users.full_name
                FROM posts
                INNER JOIN users ON posts.user_id = users.id
                WHERE posts.content ILIKE %s
                ORDER BY posts.created_at DESC
                """,
                (search_text,),
            )
            posts_result = cursor.fetchall()

            # This SELECT query searches resources by title and description.
            cursor.execute(
                """
                SELECT resources.*, users.full_name
                FROM resources
                INNER JOIN users ON resources.user_id = users.id
                WHERE resources.title ILIKE %s OR resources.description ILIKE %s
                ORDER BY resources.created_at DESC
                """,
                (search_text, search_text),
            )
            resources_result = cursor.fetchall()

        except Error as error:
            flash(f"Database error: {error}")

        finally:
            if "connection" in locals() and not connection.closed:
                if "cursor" in locals():
                    cursor.close()
                connection.close()

    return render_template(
        "search.html",
        query=query,
        students=students_result,
        posts=posts_result,
        resources=resources_result,
        user=get_logged_in_user(),
    )


DEFAULT_GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "groq/compound",
    "groq/compound-mini",
    "qwen/qwen3.6-27b",
]


def generate_ai_response(client, prompt, configured_model):
    """Generate an AI response with automatic fallback if the configured model is unavailable."""
    models_to_try = []
    if configured_model:
        models_to_try.append(configured_model)
    for model_name in DEFAULT_GROQ_MODELS:
        if model_name not in models_to_try:
            models_to_try.append(model_name)

    last_error = None
    for model_name in models_to_try:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {
                        "role": "system",
                        "content": "You are IVY, an intelligent, modern academic companion for higher education students.",
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            return response.choices[0].message.content
        except Exception as error:
            last_error = error
            continue

    raise RuntimeError(f"All Groq models failed. Last error: {last_error}")


# ---------------------------------------------------------------------------
# AI Assistant — ChatGPT-style multi-conversation support.
# Each student owns many separate IVY conversations; each conversation keeps
# its own ordered ai_messages history, and IVY only ever sees the context of
# the conversation a message is sent in. The Groq implementation above
# (generate_ai_response + model fallback) is shared with @ivy and untouched.
# ---------------------------------------------------------------------------

AI_DEFAULT_CONVERSATION_TITLE = "New Chat"
AI_MAX_TITLE_LENGTH = 80


def derive_ai_chat_title(message_text):
    """Derive a short sidebar title from the first user message."""
    cleaned = re.sub(r"\s+", " ", (message_text or "").strip())

    # Drop conversational prefixes so titles stay topic-focused.
    cleaned = re.sub(r"^(hey|hi|hello|yo)[,!\.\s]+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(please|pls|plz)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^@ivy[,:\s]+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(can|could)\s+you\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"^(what\s+is|what's|whats|what\s+are|how\s+to|how\s+do\s+i|how\s+does|"
        r"explain|describe|define|tell\s+me\s+about|teach\s+me|give\s+me|show\s+me|"
        r"help\s+me(\s+with)?|i\s+(want|need))\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.strip(" ?.!-—:;,")
    # Prefer the leading question fragment when it already names the topic.
    lead_fragment = re.split(r"[?!.]", cleaned, maxsplit=1)[0].strip()
    if len(lead_fragment) >= 8:
        cleaned = lead_fragment
    if not cleaned:
        return AI_DEFAULT_CONVERSATION_TITLE

    words = cleaned.split()
    title = ""
    for word in words:
        candidate = f"{title} {word}".strip()
        if len(candidate) > 42:
            break
        title = candidate

    if not title:
        title = cleaned[:42].strip()
    if len(title) < len(cleaned):
        title += "…"

    return title[:1].upper() + title[1:]


def fetch_owned_ai_conversation(cursor, conversation_id, user_id):
    """Return the AI conversation row only when it belongs to this student."""
    cursor.execute(
        "SELECT id, title, created_at, updated_at FROM ai_conversations "
        "WHERE id = %s AND user_id = %s",
        (conversation_id, user_id),
    )
    return cursor.fetchone()


def fetch_ai_sidebar_conversations(cursor, user_id):
    """List every AI conversation owned by one student with preview data."""
    cursor.execute(
        """
        SELECT c.id, c.title, c.created_at, c.updated_at,
               (SELECT m.content FROM ai_messages m WHERE m.conversation_id = c.id
                ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_message,
               (SELECT m.role FROM ai_messages m WHERE m.conversation_id = c.id
                ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_role,
               (SELECT COUNT(*) FROM ai_messages m WHERE m.conversation_id = c.id) AS message_count
        FROM ai_conversations c
        WHERE c.user_id = %s
        ORDER BY c.updated_at DESC, c.id DESC
        """,
        (user_id,),
    )
    return cursor.fetchall()


def gather_ai_database_context(question):
    """Reuse the original assistant behaviour: surface related posts/resources."""
    context = ""
    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        search_text = f"%{question}%"

        cursor.execute("SELECT content FROM posts WHERE content ILIKE %s LIMIT 5", (search_text,))
        for post in cursor.fetchall():
            context += f"Post: {post['content']}\n"

        cursor.execute(
            "SELECT title, description, link_url FROM resources "
            "WHERE title ILIKE %s OR description ILIKE %s LIMIT 5",
            (search_text, search_text),
        )
        for resource in cursor.fetchall():
            context += f"Resource: {resource['title']} - {resource.get('description') or ''} - {resource.get('link_url') or ''}\n"

        cursor.close()
        connection.close()
    except Error:
        # If the database query fails, continue without database context.
        pass
    return context


def build_ai_chat_prompt(transcript_messages, message_text, database_context=""):
    """Compose the standalone IVY chat prompt using only THIS conversation."""
    transcript = "\n".join(
        f"{'Student' if message['role'] == 'user' else 'IVY'}: {message['content']}"
        for message in transcript_messages
    )
    return f"""
You are IVY, an intelligent academic companion for college students.
Continue this one-to-one study chat. Use the recent turns of this conversation
to resolve follow-up references. Do not invent college-specific information.
If database context is useful, use it. Otherwise answer as a general academic tutor.
Answer directly, accurately, and concisely. Do not pretend to be a student.

Recent conversation:
{transcript or 'No prior messages.'}

Database context:
{database_context or "No matching database information found."}

Student message:
{message_text}
""".strip()


def render_ai_assistant_page(conversation_id=None):
    """Render the multi-chat workspace with one selected conversation."""
    conversations = []
    chat_messages = []
    selected_conversation = None

    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        if conversation_id:
            selected_conversation = fetch_owned_ai_conversation(cursor, conversation_id, session["user_id"])
            if not selected_conversation:
                flash("You do not have access to that AI conversation.")
                return redirect(url_for("ai_assistant"))

            cursor.execute(
                """
                SELECT id, role, content, created_at
                FROM ai_messages
                WHERE conversation_id = %s
                ORDER BY created_at ASC, id ASC
                """,
                (conversation_id,),
            )
            chat_messages = cursor.fetchall()

        conversations = fetch_ai_sidebar_conversations(cursor, session["user_id"])
        cursor.close()
        connection.close()

    except Error as error:
        flash(f"Database error: {error}")

    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    return render_template(
        "ai.html",
        conversations=conversations,
        selected_conversation=selected_conversation,
        chat_messages=chat_messages,
        user=get_logged_in_user(),
    )


@app.route("/ai")
def ai_assistant():
    """Open the most recent AI conversation, or an empty assistant state."""
    if not login_required():
        flash("Please login to use the AI assistant.")
        return redirect(url_for("login"))

    latest_conversation_id = None
    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        cursor.execute(
            "SELECT id FROM ai_conversations WHERE user_id = %s "
            "ORDER BY updated_at DESC, id DESC LIMIT 1",
            (session["user_id"],),
        )
        row = cursor.fetchone()
        latest_conversation_id = row["id"] if row else None
        cursor.close()
        connection.close()
    except Error:
        latest_conversation_id = None
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    if latest_conversation_id:
        return redirect(url_for("open_ai_conversation", conversation_id=latest_conversation_id))

    return render_ai_assistant_page()


@app.route("/ai/<int:conversation_id>")
def open_ai_conversation(conversation_id):
    """Load one owned AI conversation with its full chronological history."""
    if not login_required():
        flash("Please login to use the AI assistant.")
        return redirect(url_for("login"))

    return render_ai_assistant_page(conversation_id=conversation_id)


@app.route("/ai/conversations/new", methods=["POST"])
def create_ai_conversation():
    """Create a fresh empty AI conversation and open it."""
    if not login_required():
        flash("Please login to use the AI assistant.")
        return redirect(url_for("login"))

    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO ai_conversations (user_id, title) VALUES (%s, %s) RETURNING id",
            (session["user_id"], AI_DEFAULT_CONVERSATION_TITLE),
        )
        new_conversation_id = cursor.fetchone()[0]
        connection.commit()
        cursor.close()
        connection.close()
        return redirect(url_for("open_ai_conversation", conversation_id=new_conversation_id))
    except Error:
        flash("Could not start a new AI conversation. Please try again.")
        return redirect(url_for("ai_assistant"))
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()


@app.route("/ai/conversations/<int:conversation_id>/rename", methods=["POST"])
def rename_ai_conversation(conversation_id):
    """Rename an owned AI conversation (JSON API used by the sidebar UI)."""
    if not login_required():
        return {"ok": False, "error": "Please log in again."}, 401

    payload = request.get_json(silent=True) or {}
    new_title = re.sub(r"\s+", " ", str(payload.get("title") or "")).strip()[:AI_MAX_TITLE_LENGTH]
    if not new_title:
        return {"ok": False, "error": "Chat title cannot be empty."}, 400

    connection = None
    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)
        if not fetch_owned_ai_conversation(cursor, conversation_id, session["user_id"]):
            return {"ok": False, "error": "Conversation not found."}, 404

        cursor.execute(
            "UPDATE ai_conversations SET title = %s, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = %s AND user_id = %s RETURNING updated_at",
            (new_title, conversation_id, session["user_id"]),
        )
        updated_at = cursor.fetchone()["updated_at"]
        connection.commit()
        cursor.close()

        return {
            "ok": True,
            "conversation_id": conversation_id,
            "title": new_title,
            "updated_at": updated_at.isoformat(),
        }
    except Error as error:
        print(f"AI rename error: {error}")
        return {"ok": False, "error": "Could not rename this chat."}, 500
    finally:
        if connection and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()


@app.route("/ai/conversations/<int:conversation_id>/delete", methods=["POST"])
def delete_ai_conversation(conversation_id):
    """Delete one owned AI conversation; its messages cascade away safely."""
    if not login_required():
        flash("Please login to use the AI assistant.")
        return redirect(url_for("login"))

    was_active = request.form.get("active") == "1" or request.args.get("active") == "1"
    wants_json = request.args.get("ajax") == "1" or request.headers.get("X-Requested-With") == "fetch"

    try:
        connection = get_db_connection()
        cursor = connection.cursor()
        # The ownership filter makes it impossible to delete another student's chat.
        cursor.execute(
            "DELETE FROM ai_conversations WHERE id = %s AND user_id = %s",
            (conversation_id, session["user_id"]),
        )
        deleted_rows = cursor.rowcount
        connection.commit()
        cursor.close()
    except Error:
        deleted_rows = 0
        flash("Could not delete this AI conversation. Please try again.")
    finally:
        if "connection" in locals() and not connection.closed:
            if "cursor" in locals():
                cursor.close()
            connection.close()

    if wants_json:
        return {"ok": deleted_rows > 0}

    if was_active or deleted_rows:
        flash("AI conversation deleted.")
    return redirect(url_for("ai_assistant"))


@app.route("/ai/conversations/<int:conversation_id>/message", methods=["POST"])
def send_ai_message(conversation_id):
    """Persist a student message and answer it with IVY using only this thread."""
    if not login_required():
        return {"ok": False, "error": "Please log in again."}, 401

    payload = request.get_json(silent=True) or {}
    message_text = str(payload.get("content") or "").strip()
    if not message_text:
        return {"ok": False, "error": "Message cannot be empty."}, 400
    if len(message_text) > MAX_CHAT_MESSAGE_LENGTH:
        return {"ok": False, "error": "Message is too long."}, 400

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = db_cursor(connection, dictionary=True)

        conversation = fetch_owned_ai_conversation(cursor, conversation_id, session["user_id"])
        if not conversation:
            return {"ok": False, "error": "Conversation not found."}, 404

        cursor.execute(
            "SELECT COUNT(*) AS user_turns FROM ai_messages "
            "WHERE conversation_id = %s AND role = 'user'",
            (conversation_id,),
        )
        first_user_turn = cursor.fetchone()["user_turns"] == 0

        cursor.execute(
            """
            INSERT INTO ai_messages (conversation_id, role, content)
            VALUES (%s, 'user', %s)
            RETURNING id, created_at
            """,
            (conversation_id, message_text),
        )
        stored_user_message = cursor.fetchone()

        # A fresh chat gets its title from this first question unless renamed.
        generated_title = None
        if first_user_turn and conversation["title"] == AI_DEFAULT_CONVERSATION_TITLE:
            generated_title = derive_ai_chat_title(message_text)
            cursor.execute(
                "UPDATE ai_conversations SET title = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (generated_title, conversation_id),
            )

        # Context comes strictly from this conversation, newest window first.
        cursor.execute(
            """
            SELECT role, content FROM (
                SELECT role, content, created_at, id FROM ai_messages
                WHERE conversation_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
            ) recent_window
            ORDER BY recent_window.created_at ASC, recent_window.id ASC
            """,
            (conversation_id, IVY_CONTEXT_MESSAGE_LIMIT),
        )
        transcript_messages = cursor.fetchall()
        connection.commit()

        database_context = gather_ai_database_context(message_text)

        api_key = os.environ.get("GROQ_API_KEY")
        if api_key:
            try:
                client = Groq(api_key=api_key)
                configured_model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
                prompt = build_ai_chat_prompt(transcript_messages, message_text, database_context)
                reply_text = generate_ai_response(client, prompt, configured_model) or \
                    "IVY could not generate a response. Please try again."
            except Exception as error:
                # Keep provider details and credentials server-side.
                print(f"IVY chat error: {error}")
                reply_text = "IVY is temporarily unavailable. Please try again."
        else:
            reply_text = ("The AI service is missing its GROQ_API_KEY configuration. "
                          "Add it to your .env file and restart.")

        cursor.execute(
            """
            INSERT INTO ai_messages (conversation_id, role, content)
            VALUES (%s, 'assistant', %s)
            RETURNING id, created_at
            """,
            (conversation_id, reply_text),
        )
        stored_reply = cursor.fetchone()
        cursor.execute(
            "UPDATE ai_conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (conversation_id,),
        )
        connection.commit()

        return {
            "ok": True,
            "conversation_id": conversation_id,
            "user_message": {
                "id": stored_user_message["id"],
                "content": message_text,
                "created_at": stored_user_message["created_at"].isoformat(),
            },
            "reply": {
                "id": stored_reply["id"],
                "content": reply_text,
                "created_at": stored_reply["created_at"].isoformat(),
            },
            "title": generated_title,
        }

    except Error as error:
        print(f"AI chat persistence error: {error}")
        return {"ok": False, "error": "Your message could not be saved. Please try again."}, 500
    finally:
        if cursor:
            cursor.close()
        if connection and not connection.closed:
            connection.close()


# This block runs only when we start the file directly with:
# python app.py
if __name__ == "__main__":
    socketio.run(app, debug=True, port=int(os.environ.get("PORT", "5001")), allow_unsafe_werkzeug=True)
