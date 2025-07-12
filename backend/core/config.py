from pydantic_settings import BaseSettings
from typing import List
import os

class Settings(BaseSettings):
    SECRET_KEY: str
    CORRECT_PASSWORD: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 360  # 6時間

    R2_ACCESS_KEY_ID: str
    R2_SECRET_ACCESS_KEY: str
    R2_BUCKET_NAME: str
    R2_ENDPOINT_URL: str
    
    # R2関連のタイムアウト設定（秒）
    R2_UPLOAD_URL_EXPIRE_SECONDS: int = 7200  # 2時間
    R2_DOWNLOAD_URL_EXPIRE_SECONDS: int = 7200  # 2時間
    R2_DIRECT_DOWNLOAD_URL_EXPIRE_SECONDS: int = 300  # 5分
    R2_FILE_DELETE_DELAY_SECONDS: int = 1800  # 30分
    
    DB_PATH: str = "db_data/users.db"
    ADMIN_USERNAME: str

    CORS_ALLOWED_ORIGINS: List[str] = [
        "http://localhost:3001",
        "http://127.0.0.1:3001",
        "https://compshare.yat0i.com"
    ]

    UPLOAD_DIR: str = "./uploads"

    # .env ファイルから読み込むための設定
    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'

settings = Settings() 