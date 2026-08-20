from fastapi import APIRouter, Depends, HTTPException, Path, Body, Request, Query
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta
import aiosqlite
import asyncio
import time

from db import crud
from core.config import settings
from core import r2_transfer
from .auth_router import get_current_admin_user_from_dependency
from utils.security import log_security_event, log_security_violation, get_client_ip

router = APIRouter()

# R2クライアントはmain.pyで一元管理
# グローバル変数として参照
r2_client = None

def init_r2_client(client):
    """main.pyから呼び出されてR2クライアントを設定する"""
    global r2_client
    r2_client = client

@router.get("/users", summary="全ユーザー一覧取得 (管理者用)")
async def get_all_users(request: Request, current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    log_security_event(
        event_type="ADMIN_VIEW_ALL_USERS",
        user=current_admin_user["sub"],
        ip_address=get_client_ip(request),
        details="Admin viewed all users list"
    )
    users = await crud.get_all_users()
    return [{"id": user["id"], "username": user["username"], "is_approved": user["is_approved"], "is_admin": user["is_admin"], "upload_capacity_bytes": user.get("upload_capacity_bytes", 104857600)} for user in users]

@router.get("/pending_users", summary="未承認ユーザー一覧取得 (管理者用)")
async def get_pending_users(request: Request, current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    log_security_event(
        event_type="ADMIN_VIEW_PENDING_USERS",
        user=current_admin_user["sub"],
        ip_address=get_client_ip(request),
        details="Admin viewed pending users list"
    )
    pending_users = await crud.get_pending_users()
    return [user["username"] for user in pending_users]

@router.post("/users/{username}/approve", summary="ユーザー承認 (管理者用)")
async def approve_user(request: Request, username: str = Path(...), current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    if username == current_admin_user.get("sub"):
        log_security_violation(
            request=request,
            user=current_admin_user["sub"],
            violation_type="SELF_APPROVAL_ATTEMPT",
            details=f"Admin attempted to approve themselves: {username}"
        )
        raise HTTPException(status_code=400, detail="自分自身を承認することはできません")
    
    success = await crud.approve_user_in_db(username)
    if not success:
        log_security_violation(
            request=request,
            user=current_admin_user["sub"],
            violation_type="USER_APPROVAL_FAILED",
            details=f"Failed to approve user: {username}"
        )
        raise HTTPException(status_code=404, detail="ユーザーが見つからないか、既に承認されています")
    
    log_security_event(
        event_type="ADMIN_APPROVED_USER",
        user=current_admin_user["sub"],
        ip_address=get_client_ip(request),
        details=f"Admin approved user: {username}"
    )
    return {"message": f"User {username} approved successfully"}

@router.post("/users/{username}/reject", summary="ユーザー拒否 (管理者用)")
async def reject_user(request: Request, username: str = Path(...), current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    if username == current_admin_user.get("sub"):
        log_security_violation(
            request=request,
            user=current_admin_user["sub"],
            violation_type="SELF_REJECTION_ATTEMPT",
            details=f"Admin attempted to reject themselves: {username}"
        )
        raise HTTPException(status_code=400, detail="自分自身を拒否することはできません")
    
    user_to_reject = await crud.get_user_by_username(username)
    if user_to_reject and user_to_reject.get("is_admin"):
        log_security_violation(
            request=request,
            user=current_admin_user["sub"],
            violation_type="ADMIN_REJECTION_ATTEMPT",
            details=f"Admin attempted to reject another admin: {username}"
        )
        raise HTTPException(status_code=403, detail="管理者アカウントは拒否できません")

    success = await crud.reject_user_in_db(username)
    if not success:
        log_security_violation(
            request=request,
            user=current_admin_user["sub"],
            violation_type="USER_REJECTION_FAILED",
            details=f"Failed to reject user: {username}"
        )
        raise HTTPException(status_code=404, detail="ユーザーが見つからないか、拒否できません")
    
    log_security_event(
        event_type="ADMIN_REJECTED_USER",
        user=current_admin_user["sub"],
        ip_address=get_client_ip(request),
        details=f"Admin rejected user: {username}"
    )
    return {"message": f"User {username} rejected and deleted successfully"}

@router.delete("/users/{username}", summary="ユーザー削除 (管理者用)")
async def delete_user(request: Request, username: str = Path(...), current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    if username == current_admin_user.get("sub"):
        log_security_violation(
            request=request,
            user=current_admin_user["sub"],
            violation_type="SELF_DELETION_ATTEMPT",
            details=f"Admin attempted to delete themselves: {username}"
        )
        raise HTTPException(status_code=400, detail="自分自身を削除することはできません")

    success = await crud.delete_user_by_username(username)

    if not success:
        log_security_violation(
            request=request,
            user=current_admin_user["sub"],
            violation_type="USER_DELETION_FAILED",
            details=f"Failed to delete user: {username}"
        )
        raise HTTPException(status_code=404, detail="ユーザーが見つからないか、削除できませんでした")

    log_security_event(
        event_type="ADMIN_DELETED_USER",
        user=current_admin_user["sub"],
        ip_address=get_client_ip(request),
        details=f"Admin deleted user: {username}"
    )
    return {"message": f"ユーザー {username} が正常に削除されました"}

@router.put("/users/{username}/capacity", summary="ユーザーのアップロード容量更新 (管理者用)")
async def update_user_upload_capacity(request: Request, username: str = Path(...), capacity_bytes: int = Body(..., embed=True), current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    # 容量値の検証
    if capacity_bytes <= 0 or capacity_bytes > 100 * 1024 * 1024 * 1024:  # 100GB制限
        log_security_violation(
            request=request,
            user=current_admin_user["sub"],
            violation_type="INVALID_CAPACITY_VALUE",
            details=f"Invalid capacity value: {capacity_bytes}"
        )
        raise HTTPException(status_code=400, detail="容量は1バイト以上100GB以下である必要があります")
    
    success = await crud.update_user_capacity(username, capacity_bytes)
    if not success:
        log_security_violation(
            request=request,
            user=current_admin_user["sub"],
            violation_type="CAPACITY_UPDATE_FAILED",
            details=f"Failed to update capacity for user: {username}"
        )
        raise HTTPException(status_code=404, detail="ユーザーが見つからないか、容量の更新に失敗しました")
    
    log_security_event(
        event_type="ADMIN_UPDATED_USER_CAPACITY",
        user=current_admin_user["sub"],
        ip_address=get_client_ip(request),
        details=f"Admin updated capacity for user {username} to {capacity_bytes} bytes"
    )
    return {"message": f"ユーザー {username} のアップロード容量が {capacity_bytes} バイトに更新されました"}

@router.get("/videos", summary="全共有動画一覧取得 (管理者用)")
async def get_all_videos(request: Request, current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    log_security_event(
        event_type="ADMIN_VIEW_ALL_VIDEOS",
        user=current_admin_user["sub"],
        ip_address=get_client_ip(request),
        details="Admin viewed all shared videos list"
    )
    videos = await crud.get_all_shared_videos_admin()
    return videos

@router.delete("/videos/{video_id}", summary="共有動画削除 (管理者用)")
async def delete_video(request: Request, video_id: int = Path(...), current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    # 1. DBから動画情報を取得
    video = await crud.get_shared_video_by_id(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="動画が見つかりません")

    # 2. R2から動画ファイルを削除
    if not r2_client:
        raise HTTPException(status_code=500, detail="ストレージサービスが利用できません")

    try:
        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
        await r2_transfer.run_r2(r2_client.delete_object, Bucket=settings.R2_BUCKET_NAME, Key=video["r2_key"])

        # 圧縮前ファイルがあれば削除を試みるが、キーの命名規則に依存するため、
        # 現状は r2_key (圧縮後ファイル) のみを削除対象とする。
        # 必要であれば original_filename や video_router のロジックを参照して削除する。
        # 今回は shared_videos テーブルの r2_key を削除することを主目的とする。

    except Exception as e:
        # 404の場合は既に削除されているとみなして続行
        if hasattr(e, 'response') and e.response.get('Error', {}).get('Code') == '404':
            pass
        else:
            log_security_violation(
                request=request,
                user=current_admin_user["sub"],
                violation_type="VIDEO_DELETION_FAILED_R2",
                details=f"Failed to delete video from R2: {video_id}, {e}"
            )
            raise HTTPException(status_code=500, detail=f"ストレージからの削除に失敗しました: {e}")

    # 3. DBからレコードを削除
    success = await crud.delete_shared_video_by_id(video_id)
    if not success:
        log_security_violation(
            request=request,
            user=current_admin_user["sub"],
            violation_type="VIDEO_DELETION_FAILED_DB",
            details=f"Failed to delete video from DB: {video_id}"
        )
        raise HTTPException(status_code=500, detail="データベースからの削除に失敗しました")
    
    log_security_event(
        event_type="ADMIN_DELETED_VIDEO",
        user=current_admin_user["sub"],
        ip_address=get_client_ip(request),
        details=f"Admin deleted video: {video_id} ({video['r2_key']})"
    )
    return {"message": f"動画 (ID: {video_id}) が削除されました"}

@router.get("/cleanup/scan", summary="未共有・期限切れファイルの検索 (管理者用)")
async def scan_unshared_videos(request: Request, current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    if not r2_client:
        raise HTTPException(status_code=500, detail="ストレージサービスが利用できません")

    found_files = []
    now = datetime.now(timezone.utc)

    try:
        # 全ての共有済み動画のr2_keyをセットで取得（パフォーマンス向上のため）
        # aiosqliteの呼び出しはイベントループ上で完結させ、R2の走査だけをスレッドへ逃がす
        async with aiosqlite.connect(settings.DB_PATH) as db:
            cursor = await db.execute("SELECT r2_key FROM shared_videos")
            shared_keys = {row[0] for row in await cursor.fetchall()}

        # botocoreのpaginatorは遅延評価で、1ページ進むごとに同期のネットワーク呼び出しを行う。
        # async関数内でそのまま回すとオブジェクト数に比例してイベントループが止まるため、
        # 走査ループ全体をひとつの同期関数にまとめてスレッドプールで実行する。
        def _scan_unshared_objects():
            files = []
            # R2のcompressed/ディレクトリ内のファイル一覧を取得
            paginator = r2_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=settings.R2_BUCKET_NAME, Prefix="compressed/"):
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    last_modified = obj['LastModified']

                    # 3時間未満の場合はスキップ
                    if (now - last_modified).total_seconds() < 10800:
                        continue

                    # DBに存在しない場合のみリストアップ
                    if key not in shared_keys:
                        files.append({
                            "key": key,
                            "size": obj['Size'],
                            "last_modified": last_modified.isoformat()
                        })
            return files

        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
        found_files = await r2_transfer.run_r2(_scan_unshared_objects)
    except Exception as e:
        log_security_violation(
            request=request,
            user=current_admin_user["sub"],
            violation_type="SCAN_ERROR",
            details=f"Error scanning for unshared videos: {e}"
        )
        raise HTTPException(status_code=500, detail=f"スキャン中にエラーが発生しました: {e}")

    log_security_event(
        event_type="ADMIN_SCANNED_UNSHARED_VIDEOS",
        user=current_admin_user["sub"],
        ip_address=get_client_ip(request),
        details=f"Admin scanned unshared videos, found {len(found_files)} files"
    )
    
    return {"files": found_files, "count": len(found_files)}

@router.post("/cleanup/execute", summary="未共有・期限切れファイルの削除実行 (管理者用)")
async def cleanup_unshared_videos_execute(request: Request, current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    if not r2_client:
        raise HTTPException(status_code=500, detail="ストレージサービスが利用できません")
        
    deleted_files = []
    errors = []
    now = datetime.now(timezone.utc)
    
    try:
        # aiosqliteの呼び出しはイベントループ上で完結させ、R2の走査・削除だけをスレッドへ逃がす
        async with aiosqlite.connect(settings.DB_PATH) as db:
            cursor = await db.execute("SELECT r2_key FROM shared_videos")
            shared_keys = {row[0] for row in await cursor.fetchall()}

        # スキャンと同様のロジックで対象を特定して削除。
        # paginatorもdelete_objectも同期I/Oのため、ループ全体をひとつの同期関数に
        # まとめてスレッドプールで実行する（1件ずつto_threadを挟むより往復が少なく確実）。
        def _delete_unshared_objects():
            deleted = []
            failed = []
            paginator = r2_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=settings.R2_BUCKET_NAME, Prefix="compressed/"):
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    last_modified = obj['LastModified']

                    if (now - last_modified).total_seconds() < 10800:
                        continue

                    if key not in shared_keys:
                        try:
                            r2_client.delete_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
                            deleted.append(key)
                        except Exception as e:
                            failed.append(f"{key}: {str(e)}")
            return deleted, failed

        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
        deleted_files, errors = await r2_transfer.run_r2(_delete_unshared_objects)

    except Exception as e:
         raise HTTPException(status_code=500, detail=f"クリーンアップ実行中にエラーが発生しました: {e}")

    log_security_event(
        event_type="ADMIN_EXECUTED_CLEANUP",
        user=current_admin_user["sub"],
        ip_address=get_client_ip(request),
        details=f"Admin executed cleanup. Deleted: {len(deleted_files)}, Errors: {len(errors)}"
    )

    return {
        "message": f"{len(deleted_files)} 個のファイルを削除しました",
        "deleted_files": deleted_files,
        "errors": errors
    }

# --- R2ストレージ使用量の集計・キャッシュ ---
# モジュール変数。uvicornは--workers指定が無く1プロセスなのでこれで十分。
# 将来ワーカーを増やす場合はワーカーごとに1回走査される点に注意。
_r2_usage_cache: Optional[Dict[str, Any]] = None
_r2_usage_cached_at: float = 0.0            # time.monotonic()
_r2_usage_lock = asyncio.Lock()             # 同時アクセス時の二重走査を防ぐ


def _classify_r2_key(key: str) -> str:
    """R2キーのプレフィックスから内訳カテゴリを判定する。"""
    if key.startswith("compressed/"):
        return "compressed/"
    if key.startswith("uploads/"):
        return "uploads/"
    return "other"


def _scan_bucket_usage() -> tuple[int, int, Dict[str, Dict[str, int]]]:
    """バケット全体をListObjectsV2で走査し、合計とプレフィックス別内訳を求める同期関数。

    botocoreのpaginatorは遅延評価で、1ページ進むごとに同期のネットワーク呼び出しを行う。
    async関数内でそのまま回すとオブジェクト数に比例してイベントループが止まるため、
    scan_unshared_videos(本ファイル)と同じ形で走査ループ全体をこの同期関数に閉じ込め、
    r2_transfer.run_r2(R2専用エグゼキュータ)で1回だけ実行する。
    """
    total_bytes = 0
    object_count = 0
    per_prefix: Dict[str, Dict[str, int]] = {
        "compressed/": {"bytes": 0, "count": 0},
        "uploads/": {"bytes": 0, "count": 0},
        "other": {"bytes": 0, "count": 0},
    }
    paginator = r2_client.get_paginator('list_objects_v2')
    # Prefix指定なし＝バケット全件を対象にする
    for page in paginator.paginate(Bucket=settings.R2_BUCKET_NAME):
        for obj in page.get('Contents', []):
            # obj['Size']はpayloadのみ（メタデータを含まない）＝概算である旨をUI側に注記する
            size = obj['Size']
            category = _classify_r2_key(obj['Key'])
            total_bytes += size
            object_count += 1
            per_prefix[category]["bytes"] += size
            per_prefix[category]["count"] += 1
    return total_bytes, object_count, per_prefix


async def _get_r2_usage(refresh: bool) -> tuple[Dict[str, Any], bool]:
    """R2使用量をキャッシュ付きで取得する。戻り値は (集計結果dict, cachedかどうか)。

    ListObjectsV2は1000件ごとにClass Aオペレーションを1回消費するため、
    管理者ページを開くたびに全走査しない。
    """
    global _r2_usage_cache, _r2_usage_cached_at

    if not refresh and _r2_usage_cache is not None and \
            (time.monotonic() - _r2_usage_cached_at) < settings.R2_USAGE_CACHE_TTL_SECONDS:
        return _r2_usage_cache, True

    async with _r2_usage_lock:
        # ロック待ちの間に他方が更新済みの可能性があるので再チェックする（二重走査防止）
        if not refresh and _r2_usage_cache is not None and \
                (time.monotonic() - _r2_usage_cached_at) < settings.R2_USAGE_CACHE_TTL_SECONDS:
            return _r2_usage_cache, True

        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
        total_bytes, object_count, per_prefix = await r2_transfer.run_r2(_scan_bucket_usage)

        jst = timezone(timedelta(hours=9))
        _r2_usage_cache = {
            "total_bytes": total_bytes,
            "object_count": object_count,
            "prefixes": [
                {"prefix": prefix, "bytes": data["bytes"], "count": data["count"]}
                for prefix, data in per_prefix.items()
            ],
            "collected_at": datetime.now(jst).isoformat(),
        }
        _r2_usage_cached_at = time.monotonic()
        return _r2_usage_cache, False


@router.get("/r2/usage", summary="R2ストレージ使用量取得 (管理者用)")
async def get_r2_usage(
    request: Request,
    refresh: bool = Query(False),
    current_admin_user: dict = Depends(get_current_admin_user_from_dependency)
):
    if not r2_client:
        raise HTTPException(status_code=500, detail="ストレージサービスが利用できません")

    try:
        usage, cached = await _get_r2_usage(refresh)
    except Exception as e:
        log_security_violation(
            request=request,
            user=current_admin_user["sub"],
            violation_type="R2_USAGE_SCAN_ERROR",
            details=f"Error scanning R2 usage: {e}"
        )
        raise HTTPException(status_code=500, detail=f"使用量の集計中にエラーが発生しました: {e}")

    total_bytes = usage["total_bytes"]
    # GBは10進（1GB = 10^9バイト）。Cloudflareの請求単位に合わせるため。
    free_tier_bytes = int(settings.R2_FREE_STORAGE_GB * 1_000_000_000)
    usage_ratio = (total_bytes / free_tier_bytes) if free_tier_bytes > 0 else None

    if usage_ratio is None:
        # free_tier_bytesが0以下（設定ミス）の場合、無料枠が無いものとして扱う
        status = "over_free"
    elif usage_ratio < 0.8:
        status = "within_free"
    elif usage_ratio <= 1.0:
        status = "near_limit"
    else:
        status = "over_free"

    billable_bytes = max(0, total_bytes - free_tier_bytes)
    estimated_monthly_cost_usd = round(billable_bytes / 1e9 * settings.R2_STORAGE_PRICE_PER_GB_MONTH, 4)

    log_security_event(
        event_type="ADMIN_VIEWED_R2_USAGE",
        user=current_admin_user["sub"],
        ip_address=get_client_ip(request),
        details=f"cached={cached}, total_bytes={total_bytes}, objects={usage['object_count']}"
    )

    return {
        "total_bytes": total_bytes,
        "object_count": usage["object_count"],
        "prefixes": usage["prefixes"],
        "free_tier_bytes": free_tier_bytes,
        "usage_ratio": usage_ratio,
        "status": status,
        "billable_bytes": billable_bytes,
        "estimated_monthly_cost_usd": estimated_monthly_cost_usd,
        "price_per_gb_month_usd": settings.R2_STORAGE_PRICE_PER_GB_MONTH,
        "collected_at": usage["collected_at"],
        "cached": cached,
        "cache_ttl_seconds": settings.R2_USAGE_CACHE_TTL_SECONDS,
    }
