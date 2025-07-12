import aiosqlite
from core.config import settings
from passlib.hash import bcrypt
from fastapi import HTTPException
from typing import List, Dict, Optional, Any

from pydantic import BaseModel
class UserInDB(BaseModel):
    id: int
    username: str
    is_approved: bool
    is_admin: bool

class UserCreate:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

async def get_user_by_username(username: str):
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
        user = await cursor.fetchone()
        return dict(user) if user else None

async def create_user(user: UserCreate):
    hashed_password = bcrypt.hash(user.password)
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM users WHERE username = ?", (user.username,))
        exists = await cursor.fetchone()
        if exists:
            raise HTTPException(status_code=400, detail="ユーザー名は既に使用されています")

        await db.execute(
            "INSERT INTO users (username, hashed_password, is_approved, is_admin) VALUES (?, ?, ?, ?)",
            (user.username, hashed_password, False, False)
        )
        await db.commit()

async def get_all_users():
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users")
        users = await cursor.fetchall()
        return [dict(user) for user in users]

async def get_pending_users():
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT username FROM users WHERE is_approved = 0 AND is_admin = 0")
        pending_users = await cursor.fetchall()
        return [dict(user) for user in pending_users]

async def approve_user_in_db(username: str) -> bool:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cursor_check = await db.execute("SELECT is_admin FROM users WHERE username = ?", (username,))
        user_info = await cursor_check.fetchone()
        if user_info and user_info[0]:
            return False

        cursor = await db.execute(
            "UPDATE users SET is_approved = 1 WHERE username = ? AND is_approved = 0 AND is_admin = 0",
            (username,)
        )
        await db.commit()
        return cursor.rowcount > 0

async def reject_user_in_db(username: str) -> bool:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        async with db.execute("SELECT is_admin FROM users WHERE username = ?", (username,)) as cursor_check:
            user_to_reject = await cursor_check.fetchone()
            if user_to_reject and user_to_reject[0]:
                return False

        cursor = await db.execute(
            "DELETE FROM users WHERE username = ? AND is_admin = 0", (username,)
        )
        await db.commit()
        return cursor.rowcount > 0

async def delete_user_by_id(user_id: str) -> bool:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cursor_check = await db.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,))
        user_to_delete = await cursor_check.fetchone()

        if not user_to_delete:
            return False

        if user_to_delete[0]:
            return False

        cursor = await db.execute(
            "DELETE FROM users WHERE id = ?", (user_id,)
        )
        await db.commit()
        return cursor.rowcount > 0

async def delete_user_by_username(username: str) -> bool:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cursor_check = await db.execute("SELECT is_admin FROM users WHERE username = ?", (username,))
        user_to_delete = await cursor_check.fetchone()

        if not user_to_delete:
            return False

        if user_to_delete[0]:
            return False

        cursor = await db.execute(
            "DELETE FROM users WHERE username = ?", (username,)
        )
        await db.commit()
        return cursor.rowcount > 0

async def update_user_capacity(username: str, capacity_bytes: int) -> bool:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE users SET upload_capacity_bytes = ? WHERE username = ?",
            (capacity_bytes, username)
        )
        await db.commit()
        return cursor.rowcount > 0

# 共有動画関連の操作
async def create_shared_video(
    original_filename: str,
    compressed_filename: str,
    r2_key: str,
    share_token: str,
    expiry_date: str,
    user_id: int
) -> bool:
    from datetime import datetime, timezone, timedelta
    # 日本時間で作成日時を設定
    jst = timezone(timedelta(hours=9))
    created_at = datetime.now(jst).isoformat()
    
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO shared_videos 
               (original_filename, compressed_filename, r2_key, share_token, expiry_date, user_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (original_filename, compressed_filename, r2_key, share_token, expiry_date, user_id, created_at)
        )
        await db.commit()
        return cursor.rowcount > 0

async def get_shared_video_by_token(share_token: str):
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM shared_videos WHERE share_token = ?",
            (share_token,)
        )
        video = await cursor.fetchone()
        return dict(video) if video else None

async def get_shared_videos_by_user(user_id: int):
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM shared_videos WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        )
        videos = await cursor.fetchall()
        return [dict(video) for video in videos]

async def delete_expired_shared_videos():
    from datetime import datetime, timezone, timedelta
    # 日本時間で現在時刻を取得
    jst = timezone(timedelta(hours=9))
    current_time = datetime.now(jst).isoformat()
    
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # 期限切れの動画を取得
        cursor = await db.execute(
            "SELECT * FROM shared_videos WHERE expiry_date < ?",
            (current_time,)
        )
        expired_videos = await cursor.fetchall()
        
        # 期限切れの動画を削除
        await db.execute(
            "DELETE FROM shared_videos WHERE expiry_date < ?",
            (current_time,)
        )
        await db.commit()
        
        return [dict(video) for video in expired_videos]

async def delete_shared_video_by_token(share_token: str) -> bool:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM shared_videos WHERE share_token = ?",
            (share_token,)
        )
        await db.commit()
        return cursor.rowcount > 0