from fastapi import APIRouter, Depends, HTTPException, Path, Body, Request

from db import crud
from .auth_router import get_current_admin_user_from_dependency
from utils.security import log_security_event, log_security_violation, get_client_ip

router = APIRouter()

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