from fastapi import APIRouter, Depends, HTTPException, Form, Request, Response
from fastapi.responses import JSONResponse
from jose import jwt, JWTError
from datetime import datetime, timedelta
from passlib.hash import bcrypt

from core.config import settings
from db import crud
from db.crud import UserCreate

router = APIRouter()

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
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
async def register_user(username: str = Form(...), password: str = Form(...)):
    user_in = UserCreate(username=username, password=password)
    try:
        await crud.create_user(user_in)
    except HTTPException as e:
        raise e
    return {"message": "登録が完了しました。管理者による承認をお待ちください。"}

@router.post("/login", summary="ユーザーログイン")
async def login_for_access_token(username: str = Form(...), password: str = Form(...)):
    user = await crud.get_user_by_username(username)
    if not user or not user.get("hashed_password") or not bcrypt.verify(password, user["hashed_password"]):
        raise HTTPException(
            status_code=401,
            detail="ユーザー名またはパスワードが正しくありません",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user["is_approved"] and not user["is_admin"]:
        raise HTTPException(status_code=403, detail="アカウントが承認されていません")

    access_token = create_access_token(
        data={"sub": user["username"], "is_admin": user["is_admin"]}
    )
    return JSONResponse(content={"token": access_token, "token_type": "bearer"})

@router.get("/me", summary="現在のログインユーザー情報取得")
async def read_users_me(current_user: dict = Depends(get_current_user_from_token)):
    # current_user dict contains 'sub' (username) and 'is_admin'
    user_from_db = await crud.get_user_by_username(current_user["sub"])
    if not user_from_db:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
    
    return {
        "username": user_from_db["username"],
        "is_admin": user_from_db["is_admin"],
        "upload_capacity_bytes": user_from_db.get("upload_capacity_bytes", 1073741824) # Default to 1GB if not set
    } 