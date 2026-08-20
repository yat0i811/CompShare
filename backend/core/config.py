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

    # --- R2 の料金判定（2026-08 時点の Standard ストレージクラス） ---
    # 出典: https://developers.cloudflare.com/r2/pricing/
    # 価格改定に追従できるよう、既定値付きで環境変数から上書きできるようにしている。
    #
    # 【GB の定義】ここでの GB は 10 進（1GB = 10^9 バイト）。Cloudflare の請求単位に
    # 合わせるため。2 進(GiB = 2^30)より小さい閾値になるので、警告が保守的に早く出る。
    #
    # 【注意】無料枠は Standard クラスにのみ適用され、Infrequent Access には適用されない。
    # また実際の課金は「日次ピーク値を30日で平均した GB-month」で計算されるため、
    # 現在値からの判定はあくまで概算である（UI 側に注記を出すこと）。
    R2_FREE_STORAGE_GB: float = 10.0                # 無料枠 10 GB-month
    R2_STORAGE_PRICE_PER_GB_MONTH: float = 0.015    # 超過分 $0.015 / GB-month

    # 管理者ページの R2 使用量集計をキャッシュする秒数。
    # ListObjectsV2 は 1000 件ごとに Class A オペレーションを1回消費するため、
    # 管理者ページを開くたびに全走査しない。管理者は ?refresh=true で強制再取得できる。
    R2_USAGE_CACHE_TTL_SECONDS: int = 300           # 5分

    DB_PATH: str = "db_data/users.db"
    ADMIN_USERNAME: str

    CORS_ALLOWED_ORIGINS: List[str] = [
        "http://localhost:3001",
        "http://127.0.0.1:3001",
        "https://compshare.yat0i.com"
    ]

    # 単一ドメイン統合により compshareapi.yat0i.com は廃止済み。
    # video_router.get_user_videos_for_management がこの値で共有URLを組み立てるため、
    # 旧ホスト名のままだと管理画面の共有URLが404になる。
    FRONTEND_URL: str = "https://compshare.yat0i.com"

    UPLOAD_DIR: str = "./uploads"

    # 同時に走らせる圧縮ジョブの上限。
    # -preset slow の libx264 は12コアでも2〜3ジョブでCPUを飽和させ、
    # 1ジョブあたり入力+出力でソースの約2倍の一時ファイルをコンテナの書き込み層に置く。
    MAX_CONCURRENT_COMPRESSIONS: int = 2

    # R2転送の進捗をWebSocketへ送る間隔（秒）。
    # boto3のCallbackごとに送るのではなく、この間隔でカウンタをポーリングして送る。
    PROGRESS_INTERVAL_SEC: float = 1.0

    # --- R2専用スレッドプール/TransferManager関連（core/r2_transfer.py） ---
    # R2専用スレッドプールのワーカー数。マネージド転送1本が完了待ちで1ワーカーを
    # 占有するため「同時圧縮数(MAX_CONCURRENT_COMPRESSIONS=2) + 予備2」を既定にしている。
    # 詳しい計算式はcore/r2_transfer.pyのモジュールdocstringを参照。
    # 【限界】この4本はバケット全走査・一括削除などの長時間バッチ（admin_routerの
    # cleanup/usage系、main.pyのcron）とも共有される。転送2本＋バッチ2本で埋まると
    # 共有プレビュー等のhead/getがキュー待ちになるため、頻発するならこの値を上げること
    # （その際はR2_MAX_POOL_CONNECTIONSも連動して見直す）。
    R2_EXECUTOR_MAX_WORKERS: int = 4

    # TransferManagerのリクエスト並列数（boto3のTransferConfig(max_concurrency=N)に相当）。
    # 全転送で共有される値であり、転送ごとにこの本数が増えるわけではない。
    R2_TRANSFER_MAX_CONCURRENCY: int = 4

    # botocoreのHTTPコネクションプール上限。
    # 4(転送request) + 2(転送submission) + 4(R2 executor上のhead/get/delete) +
    # 6(StreamingResponseが保持中のget_object Body用の余裕) = 16。
    # 計算式の詳細はcore/r2_transfer.pyのモジュールdocstringを参照。
    R2_MAX_POOL_CONNECTIONS: int = 16

    # .env ファイルから読み込むための設定
    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'

settings = Settings() 