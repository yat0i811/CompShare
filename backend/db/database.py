import aiosqlite
from core.config import settings
from passlib.hash import bcrypt

async def get_db_connection():
    return await aiosqlite.connect(settings.DB_PATH)

async def init_db():
    db_path_dir = settings.DB_PATH.rpartition('/')[0]
    if db_path_dir:
        import os
        os.makedirs(db_path_dir, exist_ok=True);
        
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                hashed_password TEXT NOT NULL,
                is_approved BOOLEAN DEFAULT FALSE,
                is_admin BOOLEAN DEFAULT FALSE,
                upload_capacity_bytes INTEGER DEFAULT 104857600 -- Default to 100MB
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shared_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_filename TEXT NOT NULL,
                compressed_filename TEXT NOT NULL,
                r2_key TEXT NOT NULL,
                share_token TEXT UNIQUE NOT NULL,
                expiry_date TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """)
        await db.commit()

async def init_admin_user():
    await init_db();
    async with aiosqlite.connect(settings.DB_PATH) as db:
        async with db.execute("SELECT 1 FROM users WHERE username = ? AND is_admin = 1", (settings.ADMIN_USERNAME,)) as cursor:
            admin_exists = await cursor.fetchone()

        if not admin_exists:
            hashed_pw = bcrypt.hash(settings.CORRECT_PASSWORD)
            async with db.execute("SELECT 1 FROM users WHERE username = ?", (settings.ADMIN_USERNAME,)) as cursor:
                user_exists = await cursor.fetchone()
            
            if user_exists:
                await db.execute(
                    "UPDATE users SET hashed_password = ?, is_approved = ?, is_admin = ? WHERE username = ?",
                    (hashed_pw, True, True, settings.ADMIN_USERNAME)
                )
            else:
                await db.execute(
                    "INSERT INTO users (username, hashed_password, is_approved, is_admin) VALUES (?, ?, ?, ?)",
                    (settings.ADMIN_USERNAME, hashed_pw, True, True)
                )
            await db.commit()

async def lifespan(app):
    await init_admin_user()
    yield
    print("Application shutdown.") 