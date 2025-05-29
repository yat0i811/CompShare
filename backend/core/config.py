from pydantic_settings import BaseSettings
from typing import List
import os

class Settings(BaseSettings):
    SECRET_KEY: str
    CORRECT_PASSWORD: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    R2_ACCESS_KEY_ID: str
    R2_SECRET_ACCESS_KEY: str
    R2_BUCKET_NAME: str
    R2_ENDPOINT_URL: str
    
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