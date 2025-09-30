chcp 932
@echo off
echo ==== Docker ’â~ ====
docker-compose down

echo ==== Cloudflare tunnel ’â~ ====
echo backend-tunnel‚ğ’â~‚µ‚Ä‚¢‚Ü‚·...
cloudflared tunnel stop backend-tunnel
echo frontend-tunnel‚ğ’â~‚µ‚Ä‚¢‚Ü‚·...
cloudflared tunnel stop frontend-tunnel
timeout /t 2 >nul

pause
