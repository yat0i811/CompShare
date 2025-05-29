from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect, HTTPException, Response, Request, Depends, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse
import os, uuid, shutil, subprocess, asyncio, magic, tempfile
from jose import jwt, JWTError
from datetime import datetime, timedelta
from dotenv import load_dotenv
import boto3
from botocore.client import Config
from typing import Dict
import threading, time, json
import aiosqlite
from passlib.hash import bcrypt
from routers import auth_router, admin_router, video_router
from core.config import settings
from middlewares import ConditionalUploadLimitMiddleware, RateLimitMiddleware
from db.database import lifespan
from db import crud
from db.crud import UserCreate

load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY")
CORRECT_PASSWORD = os.getenv("CORRECT_PASSWORD")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL")

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is not set in .env")
if not CORRECT_PASSWORD:
    raise RuntimeError("CORRECT_PASSWORD is not set in .env")
if not R2_ENDPOINT_URL:
    raise RuntimeError("R2_ENDPOINT_URL is not set in .env")
if not R2_ACCESS_KEY_ID:
    raise RuntimeError("R2_ACCESS_KEY_ID is not set in .env")
if not R2_SECRET_ACCESS_KEY:
    raise RuntimeError("R2_SECRET_ACCESS_KEY is not set in .env")

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(ConditionalUploadLimitMiddleware)
app.add_middleware(RateLimitMiddleware)

app.include_router(auth_router.router, prefix="/auth", tags=["auth"])
app.include_router(admin_router.router, prefix="/admin", tags=["admin"])
app.include_router(video_router.router, tags=["video"])

@app.get("/")
async def read_root():
    return {"message": "Video Compression Service API"}

@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    user = await crud.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=401, detail="ユーザーが見つかりません")
    
    if not bcrypt.verify(password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="パスワードが正しくありません")

    if not user["is_approved"] and not user["is_admin"]:
        raise HTTPException(status_code=403, detail="アカウントが承認されていません")

    token_data = {"sub": user["username"], "is_admin": user["is_admin"]}
    token = auth_router.create_access_token(token_data)
    return JSONResponse(content={"token": token})

if not os.path.exists(settings.UPLOAD_DIR):
    os.makedirs(settings.UPLOAD_DIR)