chcp 932
@echo off
echo ==== Cloudflare �N�� ====
start "" cloudflared tunnel --config "C:\Users\yat0i\.cloudflared\config-backend.yml" run backend-tunnel
start "" cloudflared tunnel --config "C:\Users\yat0i\.cloudflared\config-frontend.yml" run frontend-tunnel

timeout /t 5 >nul
echo ==== Docker �N�� ====
docker-compose down
docker-compose up -d

echo ==== ���ׂẴT�[�r�X���J�n���܂��� ====
pause
