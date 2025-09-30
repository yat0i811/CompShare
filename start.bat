chcp 932
@echo off
echo ==== Cloudflare 起動 ====
start "" cloudflared tunnel --config "C:\Users\yat0i\.cloudflared\config-backend.yml" run backend-tunnel
start "" cloudflared tunnel --config "C:\Users\yat0i\.cloudflared\config-frontend.yml" run frontend-tunnel

timeout /t 5 >nul
echo ==== Docker 起動 ====
docker-compose down
docker-compose up -d

echo ==== すべてのサービスが開始しました ====
pause
