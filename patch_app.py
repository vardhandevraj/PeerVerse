import re

with open("app.py", "r") as f:
    code = f.read()

# 1. Imports
code = code.replace("import mysql.connector", "import psycopg2\nfrom psycopg2.extras import RealDictCursor\nfrom supabase import create_client, Client\nimport urllib.parse")
code = code.replace("from mysql.connector import Error", "from psycopg2 import Error")

# 2. Supabase Storage Setup
db_config_pattern = r"DB_CONFIG = \{\n.*?\n.*?\n.*?\n.*?\n\}"
supabase_config = """
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
"""
code = re.sub(db_config_pattern, supabase_config, code, flags=re.DOTALL)

# 3. DB Connection
get_db_pattern = r"def get_db_connection\(\):\n\s+\"\"\"Create and return a new MySQL database connection\.\"\"\"\n\s+return mysql\.connector\.connect\(\*\*DB_CONFIG\)"
get_db_replacement = """def get_db_connection():
    \"\"\"Create and return a new PostgreSQL database connection.\"\"\"
    return psycopg2.connect(SUPABASE_DB_URL)

def db_cursor(connection, dictionary=False):
    if dictionary:
        return connection.cursor(cursor_factory=RealDictCursor)
    return connection.cursor()"""
code = re.sub(get_db_pattern, get_db_replacement, code)

# Fix connection checking
code = code.replace("connection.is_connected()", "not connection.closed")

# Fix dictionary cursor calls
code = code.replace("connection.cursor(dictionary=True)", "db_cursor(connection, dictionary=True)")

# 4. Storage Upload
upload_pattern = r"def save_uploaded_file\(uploaded_file, file_prefix\):.*?return f\"uploads/\{saved_name\}\""
upload_replacement = """def save_uploaded_file(uploaded_file, file_prefix, bucket_name):
    \"\"\"Save an uploaded file and return the path stored in Supabase.\"\"\"
    if supabase is None:
        return ""
    safe_name = secure_filename(uploaded_file.filename)
    upload_time = datetime.now().strftime("%Y%m%d%H%M%S")
    object_path = f"{file_prefix}_{upload_time}_{safe_name}"
    uploaded_file.stream.seek(0)
    supabase.storage.from_(bucket_name).upload(
        object_path,
        uploaded_file.read(),
        {"content-type": uploaded_file.mimetype or "application/octet-stream"},
    )
    return supabase.storage.from_(bucket_name).get_public_url(object_path)"""
code = re.sub(upload_pattern, upload_replacement, code, flags=re.DOTALL)

# Update upload calls
code = code.replace('save_uploaded_file(profile_picture, f"user_{session[\'user_id\']}")', 'save_uploaded_file(profile_picture, f"user_{session[\'user_id\']}", STORAGE_BUCKETS["profile"])')
code = code.replace('save_uploaded_file(cover_photo, f"cover_{session[\'user_id\']}")', 'save_uploaded_file(cover_photo, f"cover_{session[\'user_id\']}", STORAGE_BUCKETS["profile"])')
code = code.replace('save_uploaded_file(post_file, f"user_{session[\'user_id\']}_post")', 'save_uploaded_file(post_file, f"user_{session[\'user_id\']}_post", STORAGE_BUCKETS["post"])')
code = code.replace('save_uploaded_file(resource_file, f"user_{session[\'user_id\']}_resource")', 'save_uploaded_file(resource_file, f"user_{session[\'user_id\']}_resource", STORAGE_BUCKETS["resource"])')
code = code.replace('save_uploaded_file(group_image, f"group_{session[\'user_id\']}")', 'save_uploaded_file(group_image, f"group_{session[\'user_id\']}", STORAGE_BUCKETS["chat"])')

# 5. Queries
# lastrowid -> RETURNING id
code = re.sub(r'cursor\.execute\((.*?)\n\s+new_post_id = cursor\.lastrowid', r'cursor.execute(\1 RETURNING id")\n        new_post_id = cursor.fetchone()["id"]', code, flags=re.DOTALL)
code = code.replace('cursor.execute(\n            "INSERT INTO conversations (is_group, created_by) VALUES (FALSE, %s)",\n            (session["user_id"],),\n        )\n        conv_id = cursor.lastrowid', 'cursor.execute(\n            "INSERT INTO conversations (is_group, created_by) VALUES (FALSE, %s) RETURNING id",\n            (session["user_id"],),\n        )\n        conv_id = cursor.fetchone()["id"]')
code = code.replace('cursor.execute(\n            "INSERT INTO conversations (title, is_group, created_by, avatar_path) "\n            "VALUES (%s, TRUE, %s, %s)",\n            (title, session["user_id"], avatar_path),\n        )\n        new_conversation_id = cursor.lastrowid', 'cursor.execute(\n            "INSERT INTO conversations (title, is_group, created_by, avatar_path) "\n            "VALUES (%s, TRUE, %s, %s) RETURNING id",\n            (title, session["user_id"], avatar_path),\n        )\n        new_conversation_id = cursor.fetchone()["id"]')

code = code.replace("INSERT IGNORE INTO", "INSERT INTO")
code = code.replace("VALUES (%s, %s, 'pending')", "VALUES (%s, %s, 'pending')\n            ON CONFLICT DO NOTHING")
code = code.replace("VALUES (%s, %s)", "VALUES (%s, %s)\n            ON CONFLICT DO NOTHING")
code = code.replace("VALUES (%s, %s) ON CONFLICT DO NOTHING", "VALUES (%s, %s)") # cleanup if dup
code = code.replace("VALUES (%s, %s)", "VALUES (%s, %s)") # cleanup

code = code.replace("LIKE %s", "ILIKE %s")

# Fix executemany
code = code.replace('cursor.executemany(\n            "INSERT INTO conversation_participants (conversation_id, user_id) VALUES (%s, %s)",\n            [(conversation_id, user_id) for user_id in valid_member_ids],\n        )', 'cursor.executemany(\n            "INSERT INTO conversation_participants (conversation_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",\n            [(conversation_id, user_id) for user_id in valid_member_ids],\n        )')

# Ensure ON CONFLICT DO NOTHING is set for followers insert
code = code.replace('cursor.execute(\n            """\n            INSERT INTO followers (follower_id, following_id)\n            VALUES (%s, %s)\n            """,', 'cursor.execute(\n            """\n            INSERT INTO followers (follower_id, following_id)\n            VALUES (%s, %s)\n            ON CONFLICT DO NOTHING\n            """,')

# Ensure ON CONFLICT DO NOTHING is set for friends insert
code = code.replace('cursor.execute(\n            """\n            INSERT INTO friends (sender_id, receiver_id, status)\n            VALUES (%s, %s, \'pending\')\n            """,', 'cursor.execute(\n            """\n            INSERT INTO friends (sender_id, receiver_id, status)\n            VALUES (%s, %s, \'pending\')\n            ON CONFLICT DO NOTHING\n            """,')

with open("app.py", "w") as f:
    f.write(code)

