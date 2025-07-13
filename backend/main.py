from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect, HTTPException, Response, Request, Depends, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse
import os, uuid, shutil, subprocess, asyncio, magic, tempfile
from jose import jwt, JWTError
from datetime import datetime, timedelta, timezone
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
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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

@app.get("/favicon.ico")
async def favicon():
    """Favicon要求に対する空のレスポンス"""
    return Response(status_code=204)

@app.options("/favicon.ico")
async def favicon_options():
    """Favicon要求のOPTIONSに対するレスポンス"""
    return Response(status_code=204)

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

# R2クライアントの作成
r2_client = boto3.client(
    's3',
    endpoint_url=R2_ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=boto3.session.Config(signature_version="s3v4"),
    region_name="auto"
)

# video_routerにR2クライアントを設定
video_router.init_r2_client(r2_client)

# 期限切れ動画のクリーンアップタスク
async def cleanup_expired_videos():
    """期限切れの共有動画をデータベースとR2から削除する"""
    try:
        print("期限切れ動画のクリーンアップを開始...")
        
        # データベースから期限切れの動画を取得して削除
        expired_videos = await crud.delete_expired_shared_videos()
        
        if not expired_videos:
            print("期限切れの動画はありません。")
            return
            
        print(f"期限切れの動画 {len(expired_videos)} 個を処理中...")
        
        # R2から対応するファイルを削除
        for video in expired_videos:
            try:
                if r2_client:
                    r2_client.delete_object(Bucket=R2_BUCKET_NAME, Key=video["r2_key"])
                    print(f"R2から削除: {video['r2_key']}")
                else:
                    print("R2クライアントが初期化されていません")
            except Exception as e:
                if hasattr(e, 'response') and e.response.get('Error', {}).get('Code') == '404':
                    print(f"R2にファイルが存在しません: {video['r2_key']}")
                else:
                    print(f"R2削除エラー: {video['r2_key']}, {e}")
                    
        print(f"期限切れ動画のクリーンアップ完了: {len(expired_videos)} 個のファイルを処理")
        
    except Exception as e:
        print(f"クリーンアップタスクでエラーが発生: {e}")

# 共有リンク未作成の圧縮動画を1日後に自動削除するバッチ
async def cleanup_unshared_compressed_videos():
    """共有リンク未作成の圧縮動画を1日後に自動削除"""
    try:
        print("未共有圧縮動画のクリーンアップを開始...")
        now = datetime.now(timezone.utc)
        deleted_count = 0
        # R2のcompressed/ディレクトリ内のファイル一覧を取得
        paginator = r2_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix="compressed/"):
            for obj in page.get('Contents', []):
                key = obj['Key']
                last_modified = obj['LastModified']
                # 3時間以上前か判定
                if (now - last_modified).total_seconds() < 10800:
                    continue
                # DBにr2_keyが存在するかチェック
                async with aiosqlite.connect(settings.DB_PATH) as db:
                    cursor = await db.execute("SELECT 1 FROM shared_videos WHERE r2_key = ?", (key,))
                    exists = await cursor.fetchone()
                if exists:
                    continue  # 共有リンク作成済み
                # 削除実行
                try:
                    r2_client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
                    print(f"未共有・1日経過ファイル削除: {key}")
                    deleted_count += 1
                except Exception as e:
                    print(f"削除失敗: {key}, {e}")
        print(f"未共有圧縮動画のクリーンアップ完了: {deleted_count} 個のファイルを削除")
    except Exception as e:
        print(f"未共有圧縮動画クリーンアップでエラー: {e}")

# スケジューラーの設定
scheduler = AsyncIOScheduler()

@app.on_event("startup")
async def startup_event():
    """アプリケーション開始時の処理"""
    print("アプリケーションを開始しています...")
    
    # 期限切れ動画のクリーンアップを毎日午前0時に実行（日本時間）
    scheduler.add_job(
        cleanup_expired_videos,
        trigger=CronTrigger(hour=0, minute=0, timezone="Asia/Tokyo"),
        id="cleanup_expired_videos",
        replace_existing=True
    )
    
    # 開始時に一度クリーンアップを実行
    await cleanup_expired_videos()
    
    # APSchedulerで1時間ごとに未共有圧縮動画のクリーンアップも実行
    scheduler.add_job(cleanup_unshared_compressed_videos, CronTrigger(minute=0))
    
    scheduler.start()
    print("スケジューラーを開始しました。")

@app.on_event("shutdown")
async def shutdown_event():
    """アプリケーション終了時の処理"""
    print("アプリケーションを終了しています...")
    scheduler.shutdown()
    print("スケジューラーを停止しました。")