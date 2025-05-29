chcp 932
@echo off
echo ==== Docker í‚é~ ====
docker-compose down

echo ==== Cloudflare tunnel í‚é~ ====
taskkill /F /IM cloudflared.exe

pause
