"""CompShare backend のテスト共通設定。

【重要】os.environ の設定はモジュールのトップ（import文より前）で行う。core.config は
import時にSettings()を評価するため、フィクスチャでは手遅れになる（各テストモジュールが
routers.video_router等をimportした時点でcore.configがすでにimportされてしまっている）。

os.environ は setdefault ではなく代入にする。開発者のシェルに実R2の値が入っていても、
テストが実R2を指さないことを保証するため（実.envファイル自体は変更しない）。
os.environがos.environより優先して読まれるため、実.envがあっても値は上書きされる
（main.pyのload_dotenv(override=False)もこれを前提にしている。詳細はmain.py参照）。
"""
import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BACKEND_DIR)

os.environ["SECRET_KEY"] = "test-secret-key-for-pytest-only"
os.environ["CORRECT_PASSWORD"] = "test-password"
os.environ["R2_ACCESS_KEY_ID"] = "test-access-key-id"
os.environ["R2_SECRET_ACCESS_KEY"] = "test-secret-access-key"
os.environ["R2_BUCKET_NAME"] = "test-bucket"
os.environ["R2_ENDPOINT_URL"] = "https://r2.invalid"  # 実在しないホスト。誤って本物のR2に触れないため
os.environ["ADMIN_USERNAME"] = "test-admin"

# 【注意】偽client/偽TransferManager/偽TransferFuture（r2_stubフィクスチャ含む）は
# ここではなくtest_r2_transfer.py側に定義する。共有が必要なのは上記の環境変数注入だけで、
# フィクスチャ自体をここに置く必然性が無いため（test_r2_transfer.py参照）。
