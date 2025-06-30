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

# CORS設定を強化
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=3600,  # プリフライトリクエストのキャッシュ時間
)

app.add_middleware(ConditionalUploadLimitMiddleware)
app.add_middleware(RateLimitMiddleware)

# グローバルエラーハンドラーでCORSヘッダーを追加
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    response = JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )
    
    # CORSヘッダーを追加
    origin = request.headers.get("origin")
    if origin and origin in settings.CORS_ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
    
    return response

# OPTIONSリクエスト用のハンドラー
@app.options("/{full_path:path}")
async def options_handler(request: Request, full_path: str):
    origin = request.headers.get("origin")
    if origin and origin in settings.CORS_ALLOWED_ORIGINS:
        return Response(
            status_code=200,
            headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Max-Age": "3600",
            }
        )
    return Response(status_code=200)

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