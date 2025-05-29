# CompShare

このWebアプリケーションは、ユーザーが動画ファイルをアップロードし、サーバー側で圧縮処理を行った後、結果をダウンロードできるサービスを提供します。ユーザー認証と管理者向けの機能も含まれています。

---

## 目次

* [特徴](#特徴)
* [技術スタック](#技術スタック)
* [前提条件](#前提条件)
* [インストール](#インストール)
* [設定](#設定)
* [使い方](#使い方)
* [停止方法](#停止方法)
* [ライセンス](#ライセンス)

---

## 特徴

* 動画ファイルのアップロードと圧縮処理
* 圧縮済み動画のダウンロード
* ユーザー登録およびログイン認証
* 管理者によるユーザー管理機能
* Cloudflare R2 を利用した動画ファイルの保存

---

## 技術スタック

*   **フロントエンド:** Next.js, React, styled-components
*   **バックエンド:** FastAPI (Python)
*   **データベース:** SQLite (aiosqlite)
*   **認証方式:** JWT, bcrypt (パスワードハッシュ)
*   **ストレージ:** Cloudflare R2 (boto3)
*   **コンテナ:** Docker, Docker Compose
*   **ネットワーク:** Cloudflare Tunnel

---

## 前提条件

ローカル環境で実行するには、以下のソフトウェアが必要です。

*   Docker と Docker Compose
*   Cloudflare Tunnel のセットアップと、backend および frontend 向けのトンネル設定ファイル

---

## インストール

1.  リポジトリをクローンします。

    ```bash
    git clone <リポジトリのURL>
    cd <クローンしたディレクトリ>
    ```

2.  Cloudflare Tunnel の設定ファイル (`config-backend.yml`, `config-frontend.yml`) を適切に配置します。これらのファイルについては、Cloudflare Tunnel のドキュメントを参照してください。

3.  `backend` ディレクトリに `.env` ファイルを作成し、以下の環境変数を設定します。

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
    CORS_ALLOWED_ORIGINS=["http://localhost:3001", "<あなたのCloudflare TunnelフロントエンドURL>"]

    # ファイルアップロードディレクトリ (Dockerコンテナ内のパス)
    UPLOAD_DIR=/app/uploads
    ```

4.  Docker イメージをビルドします。

    ```bash
    .\update_all.bat
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

アプリケーションを起動するには、プロジェクトのルートディレクトリで以下のスクリプトを実行します。

```bash
.\start_all.bat
```

これにより、Docker Compose によってバックエンドとフロントエンドのコンテナが起動し、設定済みの Cloudflare Tunnel が開始されます。アプリケーションには、設定した Cloudflare Tunnel のURL経由でアクセスできます。

---

## 停止方法

アプリケーションを停止するには、プロジェクトのルートディレクトリで以下のスクリプトを実行します。

```bash
.\stop_all.bat
```

これにより、起動しているDockerコンテナとCloudflare Tunnelプロセスが停止します。

---

## ライセンス

MITライセンスの下で公開されています。詳細はLICENSEファイルを参照してください。

---

* アプリケーションURL:[https://compshare.yat0i.com/](https://compshare.yat0i.com/)