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
from core import r2_transfer
from middlewares import ConditionalUploadLimitMiddleware, RateLimitMiddleware
from db.database import lifespan as db_lifespan
from db import crud
from db.crud import UserCreate
import asyncio
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# override=Falseを明示する（python-dotenvの既定値でもあるが、テスト時にconftest.pyが
# 先にos.environへダミー値を設定している前提を壊さないよう意図的に固定する）。
# これによりtest_lifespan.pyがmainをimportしても、.envの値でos.environが上書きされず
# 実R2の設定に触れない。
load_dotenv(override=False)
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

@asynccontextmanager
async def lifespan(app):
    """DB 初期化とスケジューラの起動・停止をまとめて担う。

    【重要】以前は DB 初期化だけを lifespan に渡し、スケジューラは
    @app.on_event("startup") で起動していたが、**これは一度も実行されていなかった**。
    Starlette は lifespan が None のときだけ on_startup を実行する _DefaultLifespan を
    使うため、lifespan を渡した時点で on_event 系のハンドラは無視される。
    その結果「期限切れ共有の削除」「未共有圧縮動画の削除」が黙って停止していた。
    スケジューラの起動は必ずこの lifespan の内側に置くこと。
    """
    # db.database.lifespan は素の async generator 関数なので、
    # async with で使うために asynccontextmanager で包む。
    async with asynccontextmanager(db_lifespan)(app):
        print("アプリケーションを開始しています...")

        # クリーンアップの起動に失敗しても、動画圧縮などの本来機能まで巻き添えで
        # 落とさない。ただし「黙って停止していた」過去があるため、失敗は必ず目立つ形で残す。
        # 例: tzdata が無いと CronTrigger(timezone="Asia/Tokyo") が
        # ZoneInfoNotFoundError を投げ、以前はこれで起動不能になった。
        scheduler_started = False
        try:
            # 期限切れ動画のクリーンアップを毎日午前0時に実行（日本時間）
            scheduler.add_job(
                cleanup_expired_videos,
                trigger=CronTrigger(hour=0, minute=0, timezone="Asia/Tokyo"),
                id="cleanup_expired_videos",
                replace_existing=True
            )

            # APSchedulerで1時間ごとに未共有圧縮動画のクリーンアップも実行
            scheduler.add_job(
                cleanup_unshared_compressed_videos,
                trigger=CronTrigger(minute=0),
                id="cleanup_unshared_compressed_videos",
                replace_existing=True
            )

            scheduler.start()
            scheduler_started = True
            print("スケジューラーを開始しました。")

            # 開始時に一度クリーンアップを実行
            await cleanup_expired_videos()
        except Exception as e:
            import traceback
            print(f"[CRITICAL] スケジューラーの起動に失敗しました: {e!r}")
            print("[CRITICAL] 自動クリーンアップは動作しません。R2にゴミが溜まり続けます。")
            traceback.print_exc()

        try:
            yield
        finally:
            print("アプリケーションを終了しています...")
            # スケジューラの停止とR2転送の停止はそれぞれ独立にtry/exceptで囲む。
            # 片方の失敗がもう片方の停止処理を妨げないようにするため
            # （scheduler_startedがFalseでもr2_transfer.shutdown()は必ず実行する）。
            if scheduler_started:
                try:
                    scheduler.shutdown()
                    print("スケジューラーを停止しました。")
                except Exception as e:
                    print(f"[WARNING] スケジューラーの停止に失敗しました: {e!r}")

            try:
                # 進行中のR2転送をキャンセルしてスレッドプール/TransferManagerを停止する。
                # 詳細な停止シーケンスはcore/r2_transfer.pyのモジュールdocstring参照。
                await r2_transfer.shutdown()
                print("R2転送の停止処理が完了しました。")
            except Exception as e:
                print(f"[WARNING] R2転送の停止処理に失敗しました: {e!r}")


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

@app.get("/healthz")
async def healthz():
    """プラットフォーム標準の公開ヘルスチェック（APP_STANDARD.md 参照）。

    リバースプロキシ配下のバックエンドは <prefix>/healthz で公開する規約のため、
    外部からは https://compshare.yat0i.com/be/healthz で到達する。
    """
    return {"status": "ok"}

@app.get("/health")
async def health_check():
    """旧パス。デプロイ途中の窓で監視が 404 を踏まないよう1リリースだけ残す。

    StatusBoard と compose の healthcheck が /healthz へ移行し切ったら削除すること。
    """
    return {"status": "ok"}

@app.get("/favicon.ico")
async def favicon():
    """Favicon要求に対する空のレスポンス"""
    return Response(status_code=204)

@app.options("/favicon.ico")
async def favicon_options():
    """Favicon要求のOPTIONSに対するレスポンス"""
    return Response(status_code=204)

# 旧 @app.post("/login") はここにあったが削除した。
# auth_router の /auth/login と重複しており、以下の問題があった:
#   - ユーザー不在とパスワード不一致で異なるメッセージを返すためユーザー列挙が可能
#   - log_authentication_event を呼ばないため認証失敗が記録に残らない
# フロントは constants.js:26 のとおり /auth/login のみを使用している。

if not os.path.exists(settings.UPLOAD_DIR):
    os.makedirs(settings.UPLOAD_DIR)

# R2クライアントの作成
# max_pool_connectionsの根拠はcore/r2_transfer.pyのモジュールdocstring参照
# （4:転送request + 2:転送submission + 4:R2 executor上のhead/get/delete + 6:ストリーミング余裕 = 16）。
r2_client = boto3.client(
    's3',
    endpoint_url=R2_ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(
        signature_version="s3v4",
        max_pool_connections=settings.R2_MAX_POOL_CONNECTIONS,
        # botocoreの既定はconnect_timeout=60 / read_timeout=60 / retries=legacy(最大5試行)。
        # run_r2に載せたhead/get/deleteはキャンセルできないため（core/r2_transfer.py参照）、
        # R2が無応答だと既定のままではワーカー1本が分単位で塞がり、非daemonワーカーを
        # atexitでjoinする都合上プロセス終了までブロックし得る。
        # 値を絞ることでワーカー占有の最悪値が有界になる（read_timeout × 合計試行回数のオーダー）。
        # クリーンアップ系は毎時/毎日のcronが再試行してくれるので、リトライを厚くするより
        # 早く諦めて枠を返すほうが全体の応答性に効く。
        connect_timeout=10,
        # ソケットの1回のread単位に効く上限であり、転送全体の制限時間ではない。
        # 大容量転送でもチャンクが届き続ける限りタイムアウトしない（＝無応答時間の上限）。
        read_timeout=30,
        # max_attemptsは"追加リトライ回数"であり、合計試行はN+1になる（=1なら2試行）。
        # botocoreの実装がそう解釈する（botocore/args.pyに
        # "max_attempts means total retries so we have to add one"とある。実測確認済み）。
        # 以前の2は「2試行」のつもりだったが実際は3試行で、占有の最悪値が想定より5割長かった。
        retries={"max_attempts": 1, "mode": "standard"},
    ),
    region_name="auto"
)

# video_routerにR2クライアントを設定
video_router.init_r2_client(r2_client)
# admin_routerにR2クライアントを設定
admin_router.init_r2_client(r2_client)
# R2転送専用のスレッドプール/TransferManagerを初期化する（core/r2_transfer.py参照）
r2_transfer.init(r2_client)

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

        if not r2_client:
            print("R2クライアントが初期化されていません")
            return

        # boto3のdelete_objectは同期I/O。AsyncIOSchedulerはアプリと同じイベントループで
        # ジョブを実行するため、async関数内で直接呼ぶと件数に比例してループが止まり、
        # 全リクエストが応答不能になる。削除ループ全体をひとつの同期関数にまとめて
        # スレッドプールで実行する（1件ずつto_threadを挟むより往復が少なく確実）。
        def _delete_expired_objects_from_r2(videos):
            for video in videos:
                try:
                    r2_client.delete_object(Bucket=R2_BUCKET_NAME, Key=video["r2_key"])
                    print(f"R2から削除: {video['r2_key']}")
                except Exception as e:
                    if hasattr(e, 'response') and e.response.get('Error', {}).get('Code') == '404':
                        print(f"R2にファイルが存在しません: {video['r2_key']}")
                    else:
                        print(f"R2削除エラー: {video['r2_key']}, {e}")

        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
        await r2_transfer.run_r2(_delete_expired_objects_from_r2, expired_videos)

        print(f"期限切れ動画のクリーンアップ完了: {len(expired_videos)} 個のファイルを処理")
        
    except Exception as e:
        print(f"クリーンアップタスクでエラーが発生: {e}")

# 共有リンク未作成の圧縮動画を3時間後に自動削除するバッチ
async def cleanup_unshared_compressed_videos():
    """共有リンク未作成の圧縮動画を3時間後に自動削除"""
    try:
        print("未共有圧縮動画のクリーンアップを開始...")
        now = datetime.now(timezone.utc)

        if not r2_client:
            print("R2クライアントが初期化されていません")
            return

        # botocoreのpaginatorは遅延評価で、1ページ進むごとに同期のネットワーク呼び出しを行う。
        # このジョブはAsyncIOScheduler（アプリと同じイベントループ）で毎時実行されるため、
        # async関数内でそのまま回すとオブジェクト数に比例してループが凍結する。
        # 走査ループ全体をひとつの同期関数にまとめてスレッドプールで実行する。
        def _list_stale_compressed_keys():
            stale_keys = []
            paginator = r2_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix="compressed/"):
                for obj in page.get('Contents', []):
                    # 3時間以上前か判定
                    if (now - obj['LastModified']).total_seconds() < 10800:
                        continue
                    stale_keys.append(obj['Key'])
            return stale_keys

        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
        stale_keys = await r2_transfer.run_r2(_list_stale_compressed_keys)

        if not stale_keys:
            print("未共有圧縮動画のクリーンアップ完了: 0 個のファイルを削除")
            return

        # aiosqliteの呼び出しはイベントループ上で完結させる必要があるため、
        # R2の走査（スレッド側）とDB参照（ループ側）を分離する。
        # キーごとのクエリではなく共有済みキーを1クエリでまとめて取得する。
        async with aiosqlite.connect(settings.DB_PATH) as db:
            cursor = await db.execute("SELECT r2_key FROM shared_videos")
            shared_keys = {row[0] for row in await cursor.fetchall()}

        target_keys = [key for key in stale_keys if key not in shared_keys]

        # 削除も同期I/Oのため、ループ全体をまとめてスレッドプールで実行する
        def _delete_unshared_objects(keys):
            deleted = 0
            for key in keys:
                try:
                    r2_client.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
                    print(f"未共有・3時間経過ファイル削除: {key}")
                    deleted += 1
                except Exception as e:
                    print(f"削除失敗: {key}, {e}")
            return deleted

        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
        deleted_count = await r2_transfer.run_r2(_delete_unshared_objects, target_keys)
        print(f"未共有圧縮動画のクリーンアップ完了: {deleted_count} 個のファイルを削除")
    except Exception as e:
        print(f"未共有圧縮動画クリーンアップでエラー: {e}")

# スケジューラーの設定
# 起動・停止は上部の lifespan が行う。
# @app.on_event("startup") は lifespan と併用すると実行されないため使用しないこと。
scheduler = AsyncIOScheduler()