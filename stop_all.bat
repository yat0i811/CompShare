chcp 932
@echo off
echo ==== Docker ��~ ====
docker-compose down

echo ==== Cloudflare tunnel ��~ ====
taskkill /F /IM cloudflared.exe

pause
