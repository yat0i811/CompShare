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