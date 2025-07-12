from fastapi import APIRouter, Depends, HTTPException, Form, Request, Response
from fastapi.responses import JSONResponse
from jose import jwt, JWTError
from datetime import datetime, timedelta, timezone
from passlib.hash import bcrypt

from core.config import settings
from db import crud
from db.crud import UserCreate
from utils.security import log_authentication_event, log_security_violation

router = APIRouter()

def create_access_token(data: dict):
    to_encode = data.copy()
    # 日本時間でトークンの有効期限を設定
    jst = timezone(timedelta(hours=9))
    expire = datetime.now(jst) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

async def get_current_user_from_token(request: Request):
    token = request.headers.get("Authorization")
    
    if not token or not token.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="認証トークンが必要です (Bearer)")
    
    token_value = token.split(" ")[1]
    try:
        payload = jwt.decode(token_value, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="無効なトークンまたは期限切れのトークンです")
    except Exception as e:
        raise HTTPException(status_code=500, detail="トークンの検証中に予期しないエラーが発生しました")

async def get_current_admin_user_from_dependency(user: dict = Depends(get_current_user_from_token)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="管理者権限が必要です")
    return user

@router.post("/register", summary="ユーザー登録")
async def register_user(request: Request, username: str = Form(...), password: str = Form(...)):
    # ユーザー名の検証
    if not username or len(username) < 3 or len(username) > 50:
        log_security_violation(
            request=request,
            user=None,
            violation_type="INVALID_USERNAME_REGISTER",
            details=f"Invalid username length: {len(username) if username else 0}"
        )
        raise HTTPException(status_code=400, detail="ユーザー名は3文字以上50文字以下である必要があります")
    
    # パスワードの検証
    if not password or len(password) < 6:
        log_security_violation(
            request=request,
            user=None,
            violation_type="INVALID_PASSWORD_REGISTER",
            details=f"Invalid password length: {len(password) if password else 0}"
        )
        raise HTTPException(status_code=400, detail="パスワードは6文字以上である必要があります")
    
    # ユーザー名に危険な文字が含まれていないかチェック
    import re
    if re.search(r'[^a-zA-Z0-9_-]', username):
        log_security_violation(
            request=request,
            user=None,
            violation_type="INVALID_USERNAME_CHARS",
            details=f"Invalid characters in username: {username}"
        )
        raise HTTPException(status_code=400, detail="ユーザー名には英数字、アンダースコア、ハイフンのみ使用できます")
    
    user_in = UserCreate(username=username, password=password)
    try:
        await crud.create_user(user_in)
        log_authentication_event(
            request=request,
            username=username,
            success=True,
            details="User registration successful"
        )
    except HTTPException as e:
        log_authentication_event(
            request=request,
            username=username,
            success=False,
            details=f"Registration failed: {e.detail}"
        )
        raise e
    return {"message": "登録が完了しました。管理者による承認をお待ちください。"}

@router.post("/login", summary="ユーザーログイン")
async def login_for_access_token(request: Request, username: str = Form(...), password: str = Form(...)):
    # ユーザー名の検証
    if not username or len(username) < 3 or len(username) > 50:
        log_security_violation(
            request=request,
            user=None,
            violation_type="INVALID_USERNAME_LOGIN",
            details=f"Invalid username length: {len(username) if username else 0}"
        )
        raise HTTPException(status_code=400, detail="ユーザー名は3文字以上50文字以下である必要があります")
    
    # パスワードの検証
    if not password:
        log_security_violation(
            request=request,
            user=None,
            violation_type="EMPTY_PASSWORD_LOGIN",
            details="Empty password provided"
        )
        raise HTTPException(status_code=400, detail="パスワードを入力してください")
    
    user = await crud.get_user_by_username(username)
    if not user or not user.get("hashed_password") or not bcrypt.verify(password, user["hashed_password"]):
        log_authentication_event(
            request=request,
            username=username,
            success=False,
            details="Invalid username or password"
        )
        raise HTTPException(
            status_code=401,
            detail="ユーザー名またはパスワードが正しくありません",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if not user["is_approved"] and not user["is_admin"]:
        log_authentication_event(
            request=request,
            username=username,
            success=False,
            details="Account not approved"
        )
        raise HTTPException(status_code=403, detail="アカウントが承認されていません")

    access_token = create_access_token(
        data={"sub": user["username"], "is_admin": user["is_admin"]}
    )
    
    log_authentication_event(
        request=request,
        username=username,
        success=True,
        details="Login successful"
    )
    
    return JSONResponse(content={"token": access_token, "token_type": "bearer"})

@router.get("/me", summary="現在のログインユーザー情報取得")
async def read_users_me(request: Request, current_user: dict = Depends(get_current_user_from_token)):
    # current_user dict contains 'sub' (username) and 'is_admin'
    user_from_db = await crud.get_user_by_username(current_user["sub"])
    if not user_from_db:
        log_security_violation(
            request=request,
            user=current_user.get("sub"),
            violation_type="USER_NOT_FOUND_ME",
            details=f"User {current_user.get('sub')} not found in database"
        )
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
    
    return {
        "username": user_from_db["username"],
        "is_admin": user_from_db["is_admin"],
        "upload_capacity_bytes": user_from_db.get("upload_capacity_bytes", 104857600) # Default to 100MB if not set
    } 