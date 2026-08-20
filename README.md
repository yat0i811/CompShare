# CompShare

動画ファイルを画質を維持した状態で圧縮して、URLで人に共有のできるWebアプリケーション

---

## 目次

* [機能](#機能)
* [技術スタック](#技術スタック)
* [セキュリティ機能](#セキュリティ機能)
* [前提条件](#前提条件)
* [インストール](#インストール)
* [設定](#設定)
* [使い方](#使い方)
* [停止方法](#停止方法)
* [テスト](#テスト)
* [ライセンス](#ライセンス)

---

## 機能

* 動画圧縮機能（FFmpeg）
    - CPU圧縮 （CRF）：圧縮効率が高く、画質も担保される
    - GPU圧縮（CBR）：圧縮速度が高い
* 動画共有機能（URL）
    - URLを用いて、1日、3日、7日の期間で共有可能
    - あとから共有設定を切り替え可能

---

## 技術スタック

*   **フロントエンド:** Next.js, React, styled-components
*   **バックエンド:** FastAPI (Python)
*   **データベース:** SQLite (aiosqlite)
*   **認証方式:** JWT, bcrypt (パスワードハッシュ)
*   **ストレージ:** Cloudflare R2 (boto3)
*   **コンテナ:** Docker
*   **ネットワーク:** Cloudflare Tunnel

---

## セキュリティ機能

### ファイルアップロードセキュリティ

* **ファイルタイプ検証**: `python-magic`ライブラリを使用してファイルの実際のMIMEタイプを検証
* **ファイル名サニタイゼーション**: 危険な文字やパス区切り文字を除去・置換
* **ファイルサイズ制限**: ユーザーごとの個別容量制限（デフォルト100MB）
* **外部アクセス制限**: 外部からのアクセス時はMAX100GB制限

### セキュリティログ

* **ログ記録**: すべてのセキュリティイベントを`logs/security.log`に記録
    * **認証イベント**: ログイン成功・失敗、ユーザー登録
    * **ファイル操作**: アップロード成功・失敗、セキュリティ違反
    * **管理者操作**: ユーザー承認・拒否・削除、容量変更
    * **詳細情報**: グローバルIPアドレス、User-Agent、操作詳細

### レート制限

* **アップロード制限**: 60秒間に3回までのアップロード制限
* **IPベース制限**: クライアントIPアドレスによる制限

### 認証・認可

* **JWT認証**: セキュアなトークンベース認証
* **管理者権限分離**: 一般ユーザーと管理者の権限分離
* **ユーザー承認システム**: 管理者によるユーザー承認


---

## 前提条件

ローカル環境で実行するには、以下のソフトウェアが必要です。

*   Docker
*   Cloudflare Tunnel のセットアップと、backend および frontend 向けのトンネル設定ファイル

---

## インストール

1.  リポジトリをクローンします。

    ```bash
    git clone https://github.com/yat0i811/CompShare.git
    cd CompShare
    ```

2.  Cloudflare Tunnel の設定ファイル (`config-backend.yml`, `config-frontend.yml`) を適切に配置（これらのファイルについては、Cloudflare Tunnel のドキュメントを参照してください）

3.  `backend` ディレクトリに `.env` ファイルを作成し、以下の環境変数を設定

    ```env
    # JWT 認証用シークレットキー (安全なランダム文字列)
    SECRET_KEY=your_jwt_secret_key
    # 管理者ユーザーのパスワード (bcrypt でハッシュ化する前の平文)
    CORRECT_PASSWORD=your_admin_password

    # Cloudflare R2 設定
    R2_ACCESS_KEY_ID=your_r2_access_key_id
    R2_SECRET_ACCESS_KEY=your_r2_secret_access_key
    R2_BUCKET_NAME=your_r2_bucket_name
    R2_ENDPOINT_URL=your_r2_endpoint_url

    # 許可するオリジン (CORS 設定)
    CORS_ALLOWED_ORIGINS=["http://localhost:3001", "<Cloudflare TunnelフロントエンドURL>"]

    # ファイルアップロードディレクトリ (Dockerコンテナ内のパス)
    UPLOAD_DIR=/app/uploads
    ```

4.  Docker イメージをビルドします（基盤リポジトリのルート `R:\WebAppServer` で実行）。

    ```powershell
    .\scripts\was.ps1 build compshare
    ```

---

## 設定

`.env` ファイルに設定する環境変数:

| 環境変数                 | 説明                                           | 例                                                                 |
| :----------------------- | :--------------------------------------------- | :----------------------------------------------------------------- |
| `SECRET_KEY`             | JWT認証に使用するシークレットキー。安全なランダム文字列を使用してください。 | `aGVsbG8gd29ybGQK`                                                 |
| `CORRECT_PASSWORD`       | 管理者ユーザーのパスワード（平文）。初回起動時にハッシュ化されます。      | `admin123`                                                         |
| `R2_ACCESS_KEY_ID`       | Cloudflare R2 の Access Key ID                 | `xxxxxxxxxxxxxxxxxxxx`                                             |
| `R2_SECRET_ACCESS_KEY`   | Cloudflare R2 の Secret Access Key             | `yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy`                     |
| `R2_BUCKET_NAME`         | Cloudflare R2 のバケット名                     | `my-video-bucket`                                                  |
| `R2_ENDPOINT_URL`        | Cloudflare R2 のエンドポイントURL              | `https://<account_id>.r2.cloudflarestorage.com`                    |
| `CORS_ALLOWED_ORIGINS`   | CORSで許可するオリジン。フロントエンドのURLを含めます。             | `["http://localhost:3001", "https://frontend.example.com"]`      |
| `UPLOAD_DIR`             | 動画アップロード用の一時ディレクトリ（Dockerコンテナ内）             | `/app/uploads`                                                     |

---

## 使い方

起動・停止は WebAppServer 基盤の運用 CLI（`R:\WebAppServer\scripts\was.ps1`）から行います。基盤リポジトリのルートで実行してください。

```powershell
.\scripts\was.ps1 up compshare       # 起動（platform の cloudflared も一緒に立ち上がる）
.\scripts\was.ps1 status             # コンテナ / health の確認
.\scripts\was.ps1 logs compshare compshare-backend -Follow
```

これにより Docker Compose でバックエンド・フロントエンドのコンテナが起動し、`platform\` 側の Cloudflare Tunnel 経由で `compshare.yat0i.com` から到達できるようになります（ルーティングは `app.yml` から生成されます）。

イメージを作り直す場合は `.\scripts\was.ps1 build compshare`（`--no-cache` 可）を使います。

---

## 停止方法

```powershell
.\scripts\was.ps1 down compshare     # このアプリだけ停止（-KeepPlatform でトンネルは残す）
```

このアプリのコンテナだけを直接操作したい場合は、`apps\CompShare` ディレクトリで `docker compose` をそのまま使うこともできます（`edge` ネットワークと cloudflared は基盤側が用意している前提）。

```powershell
docker compose stop compshare-backend
docker compose down
```

---

## テスト

テスト用の依存は `backend/requirements-dev.txt` にまとめてあります。本番イメージを太らせないため `backend/requirements.txt` には含めておらず、`.dockerignore` でイメージからも除外しています（テストコード `backend/test_*.py` ・ `backend/conftest.py` ・ `backend/pyproject.toml` も同様）。したがってテストは**コンテナの外**、または隔離したディレクトリで実行します。

1.  テスト用依存のインストール（`backend` ディレクトリで実行、**この順序で**）

    ```bash
    cd backend
    pip install -r requirements.txt      # テスト対象が import するアプリ本体の依存
    pip install -r requirements-dev.txt  # pytest / pytest-asyncio / ruff
    ```

    Windows開発機では `requirements.txt` を先に入れる必要があります。`requirements-dev.txt` が追加する `python-magic-bin`（libmagicのDLLを同梱するWindows専用パッケージ）は `requirements.txt` の `python-magic` と同じ `magic` パッケージを提供する別配布物のため、後から入れたほうが優先されます。逆順だと `python-magic` が `is_safe_video` などの実行時に libmagic の DLL を見つけられずエラーになります（本番の `ubuntu:22.04` イメージでは `apt-get install libmagic1` で解決しており影響ありません）。

2.  テストの実行（`backend` ディレクトリで実行）

    ```bash
    python -m pytest -v
    ```

    `.env` は不要です。`backend/conftest.py` が収集の最初にダミーの環境変数（`SECRET_KEY` / `R2_ACCESS_KEY_ID` / `R2_ENDPOINT_URL=https://r2.invalid` など）を注入するため、`core/config.py` の `settings = Settings()` 評価に必要な必須項目が揃います。実際の `backend/.env` が存在していても、`conftest.py` が設定する `os.environ` の値で上書きされるため実際の R2 には接続しません。

    個別のテストファイルだけを実行する例:

    ```bash
    python -m pytest test_range.py -v        # 共有動画プレビューのRangeヘッダ解析(純関数)
    python -m pytest test_security.py -v     # ファイル名サニタイズ・検証、IPアドレス判定
    python -m pytest test_r2_transfer.py -v  # R2専用エグゼキュータの分離・ループ応答性・停止処理
    python -m pytest test_lifespan.py -v     # アプリ停止時にR2転送が正しく停止すること
    python -m pytest test_async_hygiene.py -v # async def 内に同期のR2呼び出しが無いこと(AST解析)
    ```

    全体でおよそ5秒程度で完了します（R2やDBへの実アクセスを行わないため）。

3.  静的解析（ruff）

    ```bash
    ruff check .
    ```

    `pyproject.toml` の `[tool.ruff.lint]` で `ASYNC` ルール群のみを有効にしています。`async def` 関数内で**組み込みの同期I/O**（`open()` / `os.*` / `time.sleep` / `subprocess` / `requests` / `httpx` / `urllib`）を行っている箇所を検知するためのものです。`ASYNC240`（`async def` 内の `os.path.*`）だけは無効化しています。該当箇所がすべてローカル一時ファイルへの `stat()`（数十μs）で、スレッドへ逃がすコストの方が高いためです（理由は `pyproject.toml` のコメントと `docs/CLOSE_ISSUES.md` §5-5 を参照）。

    **注意: ruff の `ASYNC` ルールは boto3/botocore を知りません。** 検知対象は上記の組み込み固定リストだけなので、`async def` の中に `r2_client.head_object(...)` と直書きしても ruff は素通りします。R2転送処理が丸ごとイベントループを止めていた事故（`docs/CLOSE_ISSUES.md` §4-1）そのものの再発は、ruff ではなく `test_async_hygiene.py`（`main.py` と `routers/*.py` を AST 解析し、`async def` 内の直接 `r2_client` 呼び出しを検出するテスト）が守っています。

    新しい設計の背景（専用エグゼキュータ・停止シーケンス・`--timeout-graceful-shutdown`）とデプロイ前の手動確認項目は `docs/CLOSE_ISSUES.md` §5 にまとめてあります。

---

## ライセンス

MITライセンスの下で公開されています。詳細はLICENSEファイルを参照してください。

---

* アプリケーションURL:[https://compshare.yat0i.com/](https://compshare.yat0i.com/)