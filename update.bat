chcp 932
@echo off
echo -------------------------------
echo �o�b�N�G���h�ƃt�����g�G���h�̍X�V���J�n���܂�...
echo -------------------------------

cd /d %~dp0

docker-compose down
docker builder prune --all --force
docker-compose build --no-cache

echo -------------------------------
echo �X�V���������܂����B
echo -------------------------------
pause