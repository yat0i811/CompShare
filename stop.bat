chcp 932
@echo off
echo ==== Docker ��~ ====
docker-compose down

echo ==== Cloudflare tunnel ��~ ====
echo backend-tunnel���~���Ă��܂�...
cloudflared tunnel stop backend-tunnel
echo frontend-tunnel���~���Ă��܂�...
cloudflared tunnel stop frontend-tunnel
timeout /t 2 >nul

pause
