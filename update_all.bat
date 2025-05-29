chcp 932
@echo off
echo -------------------------------
echo バックエンドとフロントエンドの更新を開始します...
echo -------------------------------

cd /d %~dp0

docker-compose down
docker builder prune --all --force
docker-compose build --no-cache

echo -------------------------------
echo 更新が完了しました。
echo -------------------------------
pause