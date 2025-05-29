from fastapi import APIRouter, Depends, HTTPException, Path, Body

from db import crud
from .auth_router import get_current_admin_user_from_dependency

router = APIRouter()

@router.get("/users", summary="全ユーザー一覧取得 (管理者用)")
async def get_all_users(current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    users = await crud.get_all_users()
    return [{"id": user["id"], "username": user["username"], "is_approved": user["is_approved"], "is_admin": user["is_admin"], "upload_capacity_bytes": user.get("upload_capacity_bytes", 1073741824)} for user in users]

@router.get("/pending_users", summary="未承認ユーザー一覧取得 (管理者用)")
async def get_pending_users(current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    pending_users = await crud.get_pending_users()
    return [user["username"] for user in pending_users]

@router.post("/users/{username}/approve", summary="ユーザー承認 (管理者用)")
async def approve_user(username: str = Path(...), current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    if username == current_admin_user.get("sub"):
        raise HTTPException(status_code=400, detail="自分自身を承認することはできません")
    success = await crud.approve_user_in_db(username)
    if not success:
        raise HTTPException(status_code=404, detail="ユーザーが見つからないか、既に承認されています")
    return {"message": f"User {username} approved successfully"}

@router.post("/users/{username}/reject", summary="ユーザー拒否 (管理者用)")
async def reject_user(username: str = Path(...), current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    if username == current_admin_user.get("sub"):
        raise HTTPException(status_code=400, detail="自分自身を拒否することはできません")
    user_to_reject = await crud.get_user_by_username(username)
    if user_to_reject and user_to_reject.get("is_admin"):
        raise HTTPException(status_code=403, detail="管理者アカウントは拒否できません")

    success = await crud.reject_user_in_db(username)
    if not success:
        raise HTTPException(status_code=404, detail="ユーザーが見つからないか、拒否できません")
    return {"message": f"User {username} rejected and deleted successfully"}

@router.delete("/users/{username}", summary="ユーザー削除 (管理者用)")
async def delete_user(username: str = Path(...), current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    if username == current_admin_user.get("sub"):
        raise HTTPException(status_code=400, detail="自分自身を削除することはできません")

    success = await crud.delete_user_by_username(username)

    if not success:
        raise HTTPException(status_code=404, detail="ユーザーが見つからないか、削除できませんでした")

    return {"message": f"ユーザー {username} が正常に削除されました"}

@router.put("/users/{username}/capacity", summary="ユーザーのアップロード容量更新 (管理者用)")
async def update_user_upload_capacity(username: str = Path(...), capacity_bytes: int = Body(..., embed=True), current_admin_user: dict = Depends(get_current_admin_user_from_dependency)):
    success = await crud.update_user_capacity(username, capacity_bytes)
    if not success:
        raise HTTPException(status_code=404, detail="ユーザーが見つからないか、容量の更新に失敗しました")
    return {"message": f"ユーザー {username} のアップロード容量が {capacity_bytes} バイトに更新されました"}