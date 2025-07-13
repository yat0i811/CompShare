from fastapi import (
    APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Response, 
    Request, Depends, BackgroundTasks, File, Form, UploadFile, Query
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse
import os, uuid, shutil, subprocess, asyncio, magic, tempfile, time, json
from typing import Dict, Optional, List

from core.config import settings
from .auth_router import get_current_user_from_token, get_current_admin_user_from_dependency
import boto3
from db import crud
from utils.security import (
    sanitize_filename, validate_filename, log_file_upload_attempt, 
    log_security_violation, log_security_event, get_client_ip
)

from jose import jwt, JWTError
from datetime import datetime, timedelta, timezone
import secrets

router = APIRouter()

clients: Dict[str, WebSocket] = {}

# R2クライアントはmain.pyで一元管理
# グローバル変数として参照
r2_client = None

def init_r2_client(client):
    """main.pyから呼び出されてR2クライアントを設定する"""
    global r2_client
    r2_client = client

def get_video_duration(filepath: str) -> float:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration", "-of", "json", filepath
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"ffprobe failed to get duration: {result.stderr.decode()}")
    try:
        info = json.loads(result.stdout)
        return float(info["format"]["duration"])
    except (json.JSONDecodeError, KeyError) as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse ffprobe output: {e}")

async def run_ffmpeg_process(
    input_path: str,
    output_path: str,
    ffmpeg_options: list,
    client_id: str
):
    command = ["ffmpeg", "-y", "-i", input_path] + ffmpeg_options + ["-progress", "pipe:1", "-nostats", output_path]

    # デバッグ用：コマンドをログ出力
    print(f"FFmpeg command: {' '.join(command)}")

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    duration = get_video_duration(input_path)
    percent_sent = -1

    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            line = line.decode().strip()
            if line.startswith("out_time_ms="):
                out_time_ms = int(line.split("=")[1])
                current_sec = out_time_ms / 1_000_000
                percent = int((current_sec / duration) * 100)
                percent = min(percent, 99)
                if percent != percent_sent and client_id in clients:
                    try:
                        await clients[client_id].send_text(json.dumps({"type": "progress", "value": percent}))
                        percent_sent = percent
                    except Exception as e:
                        pass
        
        return_code = await process.wait()
        if return_code != 0:
            stderr_output = await process.stderr.read()
            error_message = stderr_output.decode() if stderr_output else "Unknown FFmpeg error"
            
            # デバッグ用：エラー詳細をログ出力
            print(f"FFmpeg error: {error_message}")
            
            # GPUエンコーダーが利用できない場合のフォールバック
            if ("h264_nvenc" in error_message and 
                ("not found" in error_message or "No such encoder" in error_message or 
                 "Cannot load libcuda.so.1" in error_message or "Error initializing output stream" in error_message or
                 "Invalid Level" in error_message or "InitializeEncoder failed" in error_message)):
                
                if client_id in clients:
                    try:
                        await clients[client_id].send_text(json.dumps({
                            "type": "warning", 
                            "detail": "GPUエンコーダーが利用できません。CPUエンコーダーに切り替えて再試行します。"
                        }))
                    except Exception as e:
                        pass
                
                # CPUエンコーダーで再試行
                cpu_options = []
                for option in ffmpeg_options:
                    if option == "h264_nvenc":
                        cpu_options.append("libx264")
                    else:
                        cpu_options.append(option)
                
                command = ["ffmpeg", "-y", "-i", input_path] + cpu_options + ["-progress", "pipe:1", "-nostats", output_path]
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                percent_sent = -1
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    line = line.decode().strip()
                    if line.startswith("out_time_ms="):
                        out_time_ms = int(line.split("=")[1])
                        current_sec = out_time_ms / 1_000_000
                        percent = int((current_sec / duration) * 100)
                        percent = min(percent, 99)
                        if percent != percent_sent and client_id in clients:
                            try:
                                await clients[client_id].send_text(json.dumps({"type": "progress", "value": percent}))
                                percent_sent = percent
                            except Exception as e:
                                pass
                
                return_code = await process.wait()
                if return_code != 0:
                    stderr_output = await process.stderr.read()
                    error_message = stderr_output.decode() if stderr_output else "Unknown FFmpeg error"
                    if client_id in clients:
                        try:
                            await clients[client_id].send_text(json.dumps({"type": "error", "detail": error_message}))
                        except Exception as e:
                            pass
                    raise HTTPException(status_code=500, detail=error_message)
            else:
                if client_id in clients:
                    try:
                        await clients[client_id].send_text(json.dumps({"type": "error", "detail": error_message}))
                    except Exception as e:
                        pass
                raise HTTPException(status_code=500, detail=error_message)
        
        if client_id in clients:
            try:
                await clients[client_id].send_text(json.dumps({"type": "progress", "value": 100}))
            except Exception as e:
                pass

    except asyncio.CancelledError:
        process.terminate()
        raise

def is_gpu_encoder_available() -> bool:
    """GPUエンコーダー（h264_nvenc）が利用可能かどうかをチェック"""
    try:
        import subprocess
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10
        )
        has_nvenc = "h264_nvenc" in result.stdout
        print(f"NVENC encoder available: {has_nvenc}")
        if has_nvenc:
            print("Available encoders containing 'nvenc':")
            for line in result.stdout.split('\n'):
                if 'nvenc' in line.lower():
                    print(f"  {line.strip()}")
        
        # NVENCエンコーダーが存在する場合、実際に動作するかテスト
        if has_nvenc:
            try:
                # 簡単なテスト用のコマンドを実行
                test_result = subprocess.run(
                    ["ffmpeg", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=1", 
                     "-c:v", "h264_nvenc", "-preset", "fast", "-t", "1", "-f", "null", "-"],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                # エラーが発生した場合は利用不可とみなす
                if test_result.returncode != 0:
                    print(f"NVENC encoder test failed: {test_result.stderr}")
                    return False
                print("NVENC encoder test successful")
                return True
            except Exception as e:
                print(f"NVENC encoder test error: {e}")
                return False
        
        return has_nvenc
    except Exception as e:
        print(f"Error checking NVENC encoder: {e}")
        return False

def get_ffmpeg_version() -> str:
    """FFmpegのバージョンを取得"""
    try:
        import subprocess
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            # バージョン行を抽出
            for line in result.stdout.split('\n'):
                if line.startswith('ffmpeg version'):
                    return line.split()[2]
        return "unknown"
    except Exception:
        return "unknown"

def is_nvenc_supported() -> bool:
    """NVENCエンコーダーが実際にサポートされているかチェック"""
    try:
        import subprocess
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10
        )
        has_nvenc = "h264_nvenc" in result.stdout
        print(f"NVENC encoder supported: {has_nvenc}")
        return has_nvenc
    except Exception as e:
        print(f"Error checking NVENC support: {e}")
        return False

def get_video_resolution(filepath: str) -> tuple[int, int]:
    """動画ファイルの解像度を取得"""
    try:
        import subprocess
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0", 
             "-show_entries", "stream=width,height", "-of", "csv=p=0", filepath],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            width, height = map(int, result.stdout.strip().split(','))
            return width, height
    except Exception as e:
        print(f"Error getting video resolution: {e}")
    return 1920, 1080  # デフォルト値

def get_appropriate_level(resolution: str, width: Optional[str], height: Optional[str], input_file: Optional[str] = None) -> str:
    """解像度に応じて適切なH.264レベルを選択"""
    # 実際の動画解像度を取得
    actual_width, actual_height = 1920, 1080  # デフォルト値
    if input_file:
        actual_width, actual_height = get_video_resolution(input_file)
    
    if resolution == "custom" and width and height:
        try:
            w = int(width)
            h = int(height)
            if w >= 3840 or h >= 2160:
                return "5.1"  # 4K対応
            elif w >= 1920 or h >= 1080:
                return "4.2"  # 1080p対応
            else:
                return "4.1"  # 720p対応
        except ValueError:
            pass
    
    # プリセット解像度の場合
    if resolution in ["4320p", "2160p"]:
        return "5.1"  # 4K対応
    elif resolution in ["1440p", "1080p"]:
        return "4.2"  # 1080p対応
    elif resolution in ["720p", "480p", "360p"]:
        return "4.1"  # 720p対応
    elif resolution == "source":
        # 実際の動画解像度に基づいてレベルを選択
        if actual_width >= 3840 or actual_height >= 2160:
            return "4.1"  # 4K対応（NVENCでは5.1がサポートされていない可能性があるため4.1を使用）
        elif actual_width >= 1920 or actual_height >= 1080:
            return "4.2"  # 1080p対応
        else:
            return "4.1"  # 720p対応
    else:
        return "4.2"  # デフォルト（1080p対応）

def build_ffmpeg_options(crf: int, bitrate: float, resolution: str, width: Optional[str], height: Optional[str], use_gpu: bool = False, input_file: Optional[str] = None) -> list:
    scale_map = {
        "4320p": "7680:4320", "2160p": "3840:2160", "1440p": "2560:1440",
        "1080p": "1920:1080", "720p": "1280:720", "480p": "854:480", "360p": "640:360"
    }
    
    # FFmpegバージョンを確認
    ffmpeg_version = get_ffmpeg_version()
    is_modern_ffmpeg = ffmpeg_version != "unknown" and int(ffmpeg_version.split('.')[0]) >= 5
    
    # 適切なレベルを選択（入力ファイルの解像度を考慮）
    appropriate_level = get_appropriate_level(resolution, width, height, input_file)
    
    # GPU使用時はNVENCエンコーダーを使用、そうでなければlibx264を使用
    # GPU使用が要求されていても、実際に利用可能かチェック
    gpu_available = is_gpu_encoder_available()
    print(f"GPU use requested: {use_gpu}")
    print(f"GPU encoder available: {gpu_available}")
    
    if use_gpu and gpu_available:
        print("Using GPU encoder (h264_nvenc)")
        # NVENCエンコーダーの最適化設定
        # CRF方式ではなくビットレート制御方式を使用して確実な圧縮を実現
        # フロントエンドから送信されたビットレート値を使用
        
        target_bitrate = f"{bitrate}M"
        max_bitrate = f"{bitrate * 1.25}M"  # 最大ビットレートは25%増し
        bufsize = f"{bitrate * 2}M"  # バッファサイズはビットレートの2倍
        
        # FFmpeg 4.4.2対応のNVENCオプション（ビットレート制御）
        # NVENCエンコーダーでは-levelパラメータを指定しない（サポートされていないため）
        ffmpeg_options = [
            "-vcodec", "h264_nvenc",
            "-preset", "medium",       # 圧縮効率重視のプリセット
            "-profile:v", "main",      # メインプロファイル（圧縮効率向上）
            "-rc", "cbr",              # 固定ビットレート
            "-b:v", target_bitrate,    # フロントエンドから送信されたビットレート
            "-maxrate", max_bitrate,   # 最大ビットレート
            "-bufsize", bufsize,       # バッファサイズ
            "-g", "30",                # GOPサイズ
            "-keyint_min", "30",       # 最小キーフレーム間隔
            "-bf", "3",                # Bフレーム数（圧縮効率向上）
            "-refs", "3",              # 参照フレーム数
            "-sc_threshold", "0",      # シーンチェンジ検出無効化（圧縮効率向上）
        ]
        # 新しいFFmpegバージョンでのみ使用可能なオプション
        if is_modern_ffmpeg:
            ffmpeg_options.extend([
                "-tune", "ll",          # 低遅延チューニング（ビットレート制御に適している）
                "-spatial-aq", "0",     # 空間AQを無効化（ビットレート制御時）
                "-temporal-aq", "0",    # 時間AQを無効化（ビットレート制御時）
            ])
    else:
        print("Using CPU encoder (libx264)")
        # CPUエンコーダー（libx264）の設定
        ffmpeg_options = [
            "-vcodec", "libx264", 
            "-crf", str(crf), 
            "-preset", "slow",         # 高品質プリセット
            "-tune", "film",           # フィルム用チューニング（hqの代わり）
            "-profile:v", "high",      # 高プロファイル
            "-level", appropriate_level, # 解像度に応じたレベル
            "-g", "30",                # GOPサイズ
            "-keyint_min", "30",       # 最小キーフレーム間隔
            "-sc_threshold", "0",      # シーンチェンジ検出無効化
            "-refs", "16",             # 参照フレーム数
            "-bf", "3"                 # Bフレーム数
        ]
    
    vf_option = None
    if resolution == "custom" and width and height:
        try:
            int_width = int(width)
            int_height = int(height)
            vf_option = f"scale={int_width}:{int_height}"
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid width or height for custom resolution")
    elif resolution in scale_map:
        vf_option = f"scale={scale_map[resolution]}"
    elif resolution != "source":
        vf_option = f"scale={scale_map['1080p']}"

    if vf_option:
        ffmpeg_options.extend(["-vf", vf_option])
    return ffmpeg_options

def delete_after_delay(bucket: str, key: str, delay_seconds: int = 1800):
    def delayed():
        time.sleep(delay_seconds)
        try:
            r2_client.head_object(Bucket=bucket, Key=key)
            r2_client.delete_object(Bucket=bucket, Key=key)
        except Exception as e:
            if hasattr(e, 'response') and e.response.get('Error', {}).get('Code') == '404':
                pass
            else:
                pass
    import threading
    threading.Thread(target=delayed).start()

def is_safe_video(filepath: str) -> bool:
    mime = magic.from_file(filepath, mime=True)
    return mime in ["video/mp4", "video/webm", "video/quicktime"]

@router.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str, token: str = None):
    if not token:
        await websocket.close(code=1008)
        return
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    clients[client_id] = websocket
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.pop(client_id, None)
    except Exception as e:
        clients.pop(client_id, None)

@router.get("/get-upload-url", summary="署名付きアップロードURL取得")
async def get_upload_url_endpoint(
    request: Request,
    filename: str, 
    file_size: int = Query(...), 
    current_user: dict = Depends(get_current_user_from_token)
):
    user_from_db = await crud.get_user_by_username(current_user["sub"])
    if not user_from_db:
        log_security_violation(
            request=request,
            user=current_user.get("sub"),
            violation_type="USER_NOT_FOUND",
            details=f"User {current_user.get('sub')} not found in database"
        )
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")

    # ファイル名の検証とサニタイゼーション
    if not validate_filename(filename):
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="INVALID_FILENAME",
            details=f"Invalid filename: {filename}"
        )
        raise HTTPException(status_code=400, detail="無効なファイル名です")

    sanitized_filename = sanitize_filename(filename)
    
    user_capacity = user_from_db.get("upload_capacity_bytes", 104857600) # Default to 100MB
    if file_size > user_capacity:
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="FILE_SIZE_EXCEEDED",
            details=f"File size {file_size} exceeds user capacity {user_capacity}"
        )
        raise HTTPException(status_code=413, detail=f"ファイルサイズが大きすぎます。上限は {user_capacity // (1024*1024)} MBです。")

    key = f"uploads/{uuid.uuid4().hex}_{sanitized_filename}"
    presigned_url = r2_client.generate_presigned_url(
        'put_object',
        Params={'Bucket': settings.R2_BUCKET_NAME, 'Key': key},
        ExpiresIn=settings.R2_UPLOAD_URL_EXPIRE_SECONDS,
    )
    delete_after_delay(settings.R2_BUCKET_NAME, key, delay_seconds=settings.R2_UPLOAD_URL_EXPIRE_SECONDS + settings.R2_FILE_DELETE_DELAY_SECONDS)
    
    # 成功ログ
    log_security_event(
        event_type="UPLOAD_URL_GENERATED",
        user=current_user["sub"],
        ip_address=get_client_ip(request),
        details=f"Generated upload URL for file: {sanitized_filename}, size: {file_size}"
    )
    
    return {"upload_url": presigned_url, "key": key}

async def run_ffmpeg_job_r2(
    job_id: str, key: str, filename: str, crf: int, bitrate: float, resolution: str, width: Optional[str], height: Optional[str], use_gpu: bool, client_id: str
):
    fd_input, temp_input = tempfile.mkstemp(suffix=".mp4")
    fd_output, temp_output = tempfile.mkstemp(suffix=".mp4")
    os.close(fd_input)
    os.close(fd_output)
    
    print(f"=== GPU圧縮デバッグ情報 ===")
    print(f"Job ID: {job_id}")
    print(f"Use GPU: {use_gpu}")
    print(f"Bitrate: {bitrate}")
    print(f"Input file: {temp_input}")
    print(f"Output file: {temp_output}")

    try:
        # R2からファイルをダウンロード
        print("R2からファイルをダウンロード中...")
        r2_client.download_file(settings.R2_BUCKET_NAME, key, temp_input)
        print(f"ダウンロード完了。ファイルサイズ: {os.path.getsize(temp_input)} bytes")
        
        # 入力ファイルの解像度を取得してFFmpegオプションを構築
        # 実際の動画解像度に基づいて適切なレベルを選択
        actual_width, actual_height = get_video_resolution(temp_input)
        print(f"Actual video resolution: {actual_width}x{actual_height}")
        
        # 実際の動画解像度に基づいてFFmpegオプションを構築
        ffmpeg_options = build_ffmpeg_options(crf, bitrate, resolution, width, height, use_gpu, temp_input)
        print(f"FFmpeg options: {ffmpeg_options}")
        
        # GPU使用が要求されたが利用できない場合の通知
        if use_gpu and "h264_nvenc" not in ffmpeg_options and client_id in clients:
            try:
                await clients[client_id].send_text(json.dumps({
                    "type": "warning", 
                    "detail": "GPUエンコーダーが利用できません。CPUエンコーダーで処理を続行します。"
                }))
            except Exception as e:
                pass

        print("FFmpeg処理開始...")
        await run_ffmpeg_process(temp_input, temp_output, ffmpeg_options, client_id)
        print("FFmpeg処理完了")
        
        # 出力ファイルの確認
        if os.path.exists(temp_output):
            output_size = os.path.getsize(temp_output)
            print(f"出力ファイルサイズ: {output_size} bytes")
            if output_size == 0:
                raise Exception("FFmpeg出力ファイルが空です")
        else:
            raise Exception("FFmpeg出力ファイルが作成されませんでした")

        base, ext = os.path.splitext(filename)
        compressed_filename = f"{base}_compressed{ext}"
        compressed_key = f"compressed/{compressed_filename}"
        
        print(f"R2にアップロード中... Key: {compressed_key}")
        r2_client.upload_file(temp_output, settings.R2_BUCKET_NAME, compressed_key)
        print("R2アップロード完了")

        if client_id in clients:
            url = r2_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': settings.R2_BUCKET_NAME, 'Key': compressed_key},
                ExpiresIn=settings.R2_DOWNLOAD_URL_EXPIRE_SECONDS
            )
            file_size = os.path.getsize(temp_output)
            print(f"WebSocket通知送信中... URL: {url[:50]}...")
            await clients[client_id].send_text(json.dumps({
                "type": "done", "url": url,
                "filename": compressed_filename, "size": file_size,
                "r2_key": compressed_key  # 共有機能のためにR2キーを追加
            }))
            print("WebSocket通知送信完了")
            
            # 元ファイルの削除
            try:
                r2_client.head_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
                r2_client.delete_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
                print("元ファイル削除完了")
            except Exception as e:
                if hasattr(e, 'response') and e.response.get('Error', {}).get('Code') == '404':
                    print("元ファイルが既に削除されています")
                else:
                    print(f"元ファイル削除エラー: {e}")
    except HTTPException as e:
        print(f"HTTPException発生: {e.detail}")
        if client_id in clients:
            try: await clients[client_id].send_text(json.dumps({"type": "error", "detail": e.detail}))
            except: pass
    except Exception as e:
        print(f"Exception発生: {str(e)}")
        if client_id in clients:
            try: await clients[client_id].send_text(json.dumps({"type": "error", "detail": str(e)}))
            except: pass
    finally:
        print("一時ファイル削除中...")
        if os.path.exists(temp_input): 
            os.remove(temp_input)
            print(f"入力ファイル削除: {temp_input}")
        if os.path.exists(temp_output): 
            os.remove(temp_output)
            print(f"出力ファイル削除: {temp_output}")
        print("=== GPU圧縮デバッグ情報終了 ===")

@router.post("/compress/async/", summary="R2経由での非同期動画圧縮")
async def compress_video_async_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    key: str = Form(...),
    filename: str = Form(...),
    crf: int = Form(28),
    bitrate: float = Form(4.0),
    resolution: str = Form("source"),
    width: Optional[str] = Form(None),
    height: Optional[str] = Form(None),
    use_gpu: bool = Form(False),
    client_id: str = Form(...),
    current_user: dict = Depends(get_current_user_from_token)
):
    # ファイル名の検証
    if not validate_filename(filename):
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="INVALID_FILENAME",
            details=f"Invalid filename in async compression: {filename}"
        )
        raise HTTPException(status_code=400, detail="無効なファイル名です")
    
    # CRF値の検証
    if not (18 <= crf <= 32):
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="INVALID_CRF_VALUE",
            details=f"Invalid CRF value: {crf}"
        )
        raise HTTPException(status_code=400, detail="CRF値は18から32の間である必要があります")
    
    # 解像度パラメータの検証
    valid_resolutions = ["source", "4320p", "2160p", "1440p", "1080p", "720p", "480p", "360p", "custom"]
    if resolution not in valid_resolutions:
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="INVALID_RESOLUTION",
            details=f"Invalid resolution: {resolution}"
        )
        raise HTTPException(status_code=400, detail="無効な解像度です")
    
    # カスタム解像度の検証
    if resolution == "custom":
        try:
            if width and height:
                int_width = int(width)
                int_height = int(height)
                if int_width <= 0 or int_height <= 0 or int_width > 7680 or int_height > 4320:
                    log_security_violation(
                        request=request,
                        user=current_user["sub"],
                        violation_type="INVALID_CUSTOM_RESOLUTION",
                        details=f"Invalid custom resolution: {width}x{height}"
                    )
                    raise HTTPException(status_code=400, detail="カスタム解像度は1x1から7680x4320の間である必要があります")
        except ValueError:
            log_security_violation(
                request=request,
                user=current_user["sub"],
                violation_type="INVALID_CUSTOM_RESOLUTION",
                details=f"Non-numeric custom resolution: {width}x{height}"
            )
            raise HTTPException(status_code=400, detail="カスタム解像度は数値である必要があります")
    
    job_id = uuid.uuid4().hex
    # 実際のFFmpegオプションはrun_ffmpeg_job_r2内で構築される
    background_tasks.add_task(run_ffmpeg_job_r2, job_id, key, filename, crf, bitrate, resolution, width, height, use_gpu, client_id)
    
    # 成功ログ
    log_security_event(
        event_type="ASYNC_COMPRESSION_STARTED",
        user=current_user["sub"],
        ip_address=get_client_ip(request),
        details=f"Started async compression for file: {filename}, CRF: {crf}, Resolution: {resolution}"
    )
    
    for _ in range(10):
        if client_id in clients: break
        await asyncio.sleep(0.1)
    
    # CORSヘッダーを明示的に追加
    response = JSONResponse(content={"job_id": job_id, "status": "started"})
    origin = request.headers.get("origin")
    if origin and origin in settings.CORS_ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
    
    return response

@router.post("/upload/", summary="ローカルでの動画アップロードと圧縮")
async def upload_and_compress_local_endpoint(
    request: Request,
    file: UploadFile = File(...),
    filename: str = Form(...),
    crf: int = Form(28),
    bitrate: float = Form(4.0),
    resolution: str = Form("source"),
    width: Optional[str] = Form(None),
    height: Optional[str] = Form(None),
    use_gpu: bool = Form(False),
    client_id: str = Form(...),
    current_user: dict = Depends(get_current_user_from_token)
):
    user_from_db = await crud.get_user_by_username(current_user["sub"])
    if not user_from_db:
        log_security_violation(
            request=request,
            user=current_user.get("sub"),
            violation_type="USER_NOT_FOUND",
            details=f"User {current_user.get('sub')} not found in database"
        )
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")

    # ファイル名の検証とサニタイゼーション
    if not validate_filename(filename):
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="INVALID_FILENAME",
            details=f"Invalid filename in local upload: {filename}"
        )
        raise HTTPException(status_code=400, detail="無効なファイル名です")
    
    sanitized_filename = sanitize_filename(filename)

    user_capacity = user_from_db.get("upload_capacity_bytes", 104857600) # Default to 100MB
    
    # Check file size before reading the entire file into memory
    # Get the file size from the UploadFile object
    file.file.seek(0, os.SEEK_END)
    file_size = file.file.tell()
    file.file.seek(0) # Reset file pointer

    if file_size > user_capacity:
        log_file_upload_attempt(
            request=request,
            user=current_user["sub"],
            filename=sanitized_filename,
            file_size=file_size,
            success=False,
            error_message=f"File size {file_size} exceeds user capacity {user_capacity}"
        )
        raise HTTPException(status_code=413, detail=f"ファイルサイズが大きすぎます。上限は {user_capacity // (1024*1024)} MBです。")

    # CRF値の検証
    if not (18 <= crf <= 32):
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="INVALID_CRF_VALUE",
            details=f"Invalid CRF value in local upload: {crf}"
        )
        raise HTTPException(status_code=400, detail="CRF値は18から32の間である必要があります")
    
    # 解像度パラメータの検証
    valid_resolutions = ["source", "4320p", "2160p", "1440p", "1080p", "720p", "480p", "360p", "custom"]
    if resolution not in valid_resolutions:
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="INVALID_RESOLUTION",
            details=f"Invalid resolution in local upload: {resolution}"
        )
        raise HTTPException(status_code=400, detail="無効な解像度です")
    
    # カスタム解像度の検証
    if resolution == "custom":
        try:
            if width and height:
                int_width = int(width)
                int_height = int(height)
                if int_width <= 0 or int_height <= 0 or int_width > 7680 or int_height > 4320:
                    log_security_violation(
                        request=request,
                        user=current_user["sub"],
                        violation_type="INVALID_CUSTOM_RESOLUTION",
                        details=f"Invalid custom resolution in local upload: {width}x{height}"
                    )
                    raise HTTPException(status_code=400, detail="カスタム解像度は1x1から7680x4320の間である必要があります")
        except ValueError:
            log_security_violation(
                request=request,
                user=current_user["sub"],
                violation_type="INVALID_CUSTOM_RESOLUTION",
                details=f"Non-numeric custom resolution in local upload: {width}x{height}"
            )
            raise HTTPException(status_code=400, detail="カスタム解像度は数値である必要があります")

    temp_input = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name
    temp_output = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name

    try:
        with open(temp_input, "wb") as f:
            f.write(await file.read())

        if not is_safe_video(temp_input):
            log_security_violation(
                request=request,
                user=current_user["sub"],
                violation_type="UNSAFE_VIDEO_FILE",
                details=f"Unsafe video file detected: {sanitized_filename}"
            )
            os.remove(temp_input)
            raise HTTPException(status_code=400, detail="Invalid or unsupported video file")

        ffmpeg_options = build_ffmpeg_options(crf, bitrate, resolution, width, height, use_gpu)

        # GPU使用が要求されたが利用できない場合の通知
        if use_gpu and "h264_nvenc" not in ffmpeg_options and client_id in clients:
            try:
                await clients[client_id].send_text(json.dumps({
                    "type": "warning", 
                    "detail": "GPUエンコーダーが利用できません。CPUエンコーダーで処理を続行します。"
                }))
            except Exception as e:
                pass

        await run_ffmpeg_process(temp_input, temp_output, ffmpeg_options, client_id)
        
        # 成功ログ
        log_file_upload_attempt(
            request=request,
            user=current_user["sub"],
            filename=sanitized_filename,
            file_size=file_size,
            success=True
        )

    except HTTPException as e:
        if os.path.exists(temp_input): os.remove(temp_input)
        if os.path.exists(temp_output): os.remove(temp_output)
        log_file_upload_attempt(
            request=request,
            user=current_user["sub"],
            filename=sanitized_filename,
            file_size=file_size,
            success=False,
            error_message=str(e.detail)
        )
        raise e
    except Exception as e:
        if os.path.exists(temp_input): os.remove(temp_input)
        if os.path.exists(temp_output): os.remove(temp_output)
        if client_id in clients:
            try: await clients[client_id].send_text(json.dumps({"type": "error", "detail": str(e)}))
            except: pass
        log_file_upload_attempt(
            request=request,
            user=current_user["sub"],
            filename=sanitized_filename,
            file_size=file_size,
            success=False,
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail=f"FFmpeg processing failed: {str(e)}")

    with open(temp_output, "rb") as f:
        content = f.read()

    if os.path.exists(temp_input): os.remove(temp_input)
    if os.path.exists(temp_output): os.remove(temp_output)

    # CORSヘッダーを明示的に追加
    response = Response(content=content, media_type="video/mp4")
    origin = request.headers.get("origin")
    if origin and origin in settings.CORS_ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
    
    return response

@router.options("/upload/")
async def upload_options(request: Request):
    """ローカルアップロードエンドポイントのOPTIONSリクエストハンドラー"""
    origin = request.headers.get("origin")
    if origin and origin in settings.CORS_ALLOWED_ORIGINS:
        return Response(
            status_code=200,
            headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Max-Age": "3600",
            }
        )
    return Response(status_code=200)

@router.post("/share/create", summary="圧縮動画の共有リンクを作成")
async def create_share_link(
    request: Request,
    compressed_filename: str = Form(...),
    r2_key: str = Form(...),
    expiry_days: int = Form(...),
    current_user: dict = Depends(get_current_user_from_token)
):
    # 有効期限日数の検証
    if expiry_days not in [1, 3, 7]:
        raise HTTPException(status_code=400, detail="有効期限は1日、3日、7日のいずれかである必要があります")
    
    # ファイル名の検証
    if not validate_filename(compressed_filename):
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="INVALID_FILENAME",
            details=f"Invalid filename in share creation: {compressed_filename}"
        )
        raise HTTPException(status_code=400, detail="無効なファイル名です")
    
    # ユーザー情報の取得
    user_from_db = await crud.get_user_by_username(current_user["sub"])
    if not user_from_db:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
    
    # R2でファイルの存在確認
    try:
        r2_client.head_object(Bucket=settings.R2_BUCKET_NAME, Key=r2_key)
    except Exception as e:
        if hasattr(e, 'response') and e.response.get('Error', {}).get('Code') == '404':
            raise HTTPException(status_code=404, detail="圧縮動画が見つかりません")
        else:
            raise HTTPException(status_code=500, detail="ファイルの確認に失敗しました")
    
    # 共有トークンの生成
    share_token = secrets.token_urlsafe(32)
    
    # 有効期限の計算（日本時間）
    jst = timezone(timedelta(hours=9))
    expiry_date = (datetime.now(jst) + timedelta(days=expiry_days)).isoformat()
    
    # データベースに共有情報を保存
    success = await crud.create_shared_video(
        original_filename=compressed_filename.replace("_compressed", ""),
        compressed_filename=compressed_filename,
        r2_key=r2_key,
        share_token=share_token,
        expiry_date=expiry_date,
        user_id=user_from_db["id"]
    )
    
    if not success:
        raise HTTPException(status_code=500, detail="共有リンクの作成に失敗しました")
    
    # 共有URLの生成
    share_url = f"{request.url.scheme}://{request.url.netloc}/share/{share_token}"
    
    log_security_event(
        event_type="SHARE_LINK_CREATED",
        user=current_user["sub"],
        ip_address=get_client_ip(request),
        details=f"Created share link for file: {compressed_filename}, expires in {expiry_days} days"
    )
    
    return JSONResponse(content={
        "share_url": share_url,
        "share_token": share_token,
        "expiry_date": expiry_date,
        "expiry_days": expiry_days
    })

@router.options("/share/{share_token}")
async def share_options(share_token: str, request: Request):
    """共有エンドポイントのOPTIONSリクエストハンドラー"""
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age": "3600",
        }
    )

@router.options("/share/{share_token}/preview")
async def share_preview_options(share_token: str, request: Request):
    """共有プレビューエンドポイントのOPTIONSリクエストハンドラー"""
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age": "3600",
        }
    )

@router.options("/share/{share_token}/download")
async def share_download_options(share_token: str, request: Request):
    """共有ダウンロードエンドポイントのOPTIONSリクエストハンドラー"""
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age": "3600",
        }
    )

@router.get("/share/{share_token}", summary="共有動画のプレビューページ（認証不要）")
async def shared_video_preview_page(
    share_token: str,
    request: Request
):
    # 共有動画情報の取得
    shared_video = await crud.get_shared_video_by_token(share_token)
    if not shared_video:
        raise HTTPException(status_code=404, detail="共有リンクが見つかりません")
    
    # 有効期限の確認（日本時間）
    jst = timezone(timedelta(hours=9))
    expiry_date = datetime.fromisoformat(shared_video["expiry_date"])
    if datetime.now(jst) > expiry_date:
        # 期限切れの場合はデータベースから削除
        await crud.delete_shared_video_by_token(share_token)
        raise HTTPException(status_code=410, detail="共有リンクの有効期限が切れています")
    
    # R2でファイルサイズの取得
    try:
        response = r2_client.head_object(Bucket=settings.R2_BUCKET_NAME, Key=shared_video["r2_key"])
        file_size = response.get('ContentLength', 0)
    except Exception as e:
        if hasattr(e, 'response') and e.response.get('Error', {}).get('Code') == '404':
            await crud.delete_shared_video_by_token(share_token)
            raise HTTPException(status_code=404, detail="共有ファイルが見つかりません")
        else:
            file_size = 0
    
    # ファイルサイズを読みやすい形式に変換
    def format_file_size(size_bytes):
        if size_bytes == 0:
            return "不明"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
    
    formatted_size = format_file_size(file_size)
    
    # 有効期限を日本語形式に変換
    import locale
    try:
        expiry_str = expiry_date.strftime("%Y年%m月%d日 %H:%M")
    except:
        expiry_str = expiry_date.strftime("%Y-%m-%d %H:%M")
    
    # HTMLページの生成
    html_content = f"""
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>動画共有 - {shared_video['compressed_filename']}</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
                background-color: #f5f5f5;
                line-height: 1.6;
            }}
            .container {{
                background: white;
                border-radius: 8px;
                padding: 30px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }}
            h1 {{
                color: #333;
                text-align: center;
                margin-bottom: 30px;
                border-bottom: 2px solid #007bff;
                padding-bottom: 10px;
            }}
            .video-info {{
                background: #f8f9fa;
                border-radius: 6px;
                padding: 20px;
                margin: 20px 0;
            }}
            .info-item {{
                display: flex;
                justify-content: space-between;
                margin: 10px 0;
                padding: 8px 0;
                border-bottom: 1px solid #dee2e6;
            }}
            .info-item:last-child {{
                border-bottom: none;
            }}
            .info-label {{
                font-weight: bold;
                color: #495057;
            }}
            .info-value {{
                color: #6c757d;
            }}
            .video-container {{
                text-align: center;
                margin: 30px 0;
            }}
            video {{
                max-width: 100%;
                border-radius: 8px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.2);
            }}
            .download-section {{
                text-align: center;
                margin: 30px 0;
            }}
            .download-btn {{
                background: #007bff;
                color: white;
                padding: 12px 30px;
                border: none;
                border-radius: 6px;
                font-size: 16px;
                cursor: pointer;
                text-decoration: none;
                display: inline-block;
                transition: background-color 0.3s ease;
            }}
            .download-btn:hover {{
                background: #0056b3;
            }}
            .expiry-notice {{
                background: #fff3cd;
                border: 1px solid #ffeaa7;
                border-radius: 6px;
                padding: 15px;
                margin: 20px 0;
                color: #856404;
            }}
            .footer {{
                text-align: center;
                margin-top: 40px;
                color: #6c757d;
                font-size: 14px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>CompShare</h1>
            
            <div class="video-info">
                <div class="info-item">
                    <span class="info-label">ファイル名:</span>
                    <span class="info-value">{shared_video['compressed_filename']}</span>
                </div>
                <div class="info-item">
                    <span class="info-label">ファイルサイズ:</span>
                    <span class="info-value">{formatted_size}</span>
                </div>
                <div class="info-item">
                    <span class="info-label">有効期限:</span>
                    <span class="info-value">{expiry_str}</span>
                </div>
            </div>
            
            <div class="expiry-notice">
                ⚠️ この共有リンクは有効期限があります。期限を過ぎるとアクセスできなくなります。
            </div>
            
            <div class="video-container">
                <video controls preload="metadata">
                    <source src="{request.url.scheme}://{request.url.netloc}/share/{share_token}/preview" type="video/mp4">
                    お使いのブラウザは動画の再生をサポートしていません。
                </video>
            </div>
            
            <div class="download-section">
                <a href="{request.url.scheme}://{request.url.netloc}/share/{share_token}/download" class="download-btn">
                    ダウンロード
                </a>
            </div>
            
            <div class="footer">
                CompShare - 動画圧縮・共有サービス
            </div>
        </div>
    </body>
    </html>
    """
    
    return HTMLResponse(content=html_content)

@router.get("/share/{share_token}/preview", summary="共有動画のプレビューストリーミング（認証不要）")
async def shared_video_preview_stream(
    share_token: str,
    request: Request
):
    # R2クライアントの初期化チェック
    if r2_client is None:
        raise HTTPException(status_code=500, detail="ストレージクライアントが初期化されていません")
    
    # 共有動画情報の取得
    shared_video = await crud.get_shared_video_by_token(share_token)
    if not shared_video:
        raise HTTPException(status_code=404, detail="共有リンクが見つかりません")
    
    # 有効期限の確認（日本時間）
    jst = timezone(timedelta(hours=9))
    expiry_date = datetime.fromisoformat(shared_video["expiry_date"])
    if datetime.now(jst) > expiry_date:
        await crud.delete_shared_video_by_token(share_token)
        raise HTTPException(status_code=410, detail="共有リンクの有効期限が切れています")
    
    # R2から動画ファイルをストリーミングで取得
    try:
        response = r2_client.get_object(Bucket=settings.R2_BUCKET_NAME, Key=shared_video["r2_key"])
        
        def generate():
            try:
                for chunk in response['Body'].iter_chunks(chunk_size=8192):
                    yield chunk
            except Exception as e:
                print(f"Streaming error: {e}")
        
        return StreamingResponse(
            generate(),
            media_type="video/mp4",
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
            }
        )
    except Exception as e:
        if hasattr(e, 'response') and e.response.get('Error', {}).get('Code') == '404':
            await crud.delete_shared_video_by_token(share_token)
            raise HTTPException(status_code=404, detail="共有ファイルが見つかりません")
        else:
            print(f"R2 get_object error: {e}")
            raise HTTPException(status_code=500, detail=f"プレビューの取得に失敗しました: {str(e)}")

@router.get("/share/{share_token}/download", summary="共有動画のダウンロード（認証不要）")
async def download_shared_video(
    share_token: str,
    request: Request
):
    # R2クライアントの初期化チェック
    if r2_client is None:
        raise HTTPException(status_code=500, detail="ストレージクライアントが初期化されていません")
    
    # 共有動画情報の取得
    shared_video = await crud.get_shared_video_by_token(share_token)
    if not shared_video:
        raise HTTPException(status_code=404, detail="共有リンクが見つかりません")
    
    # 有効期限の確認（日本時間）
    jst = timezone(timedelta(hours=9))
    expiry_date = datetime.fromisoformat(shared_video["expiry_date"])
    if datetime.now(jst) > expiry_date:
        # 期限切れの場合はデータベースから削除
        await crud.delete_shared_video_by_token(share_token)
        raise HTTPException(status_code=410, detail="共有リンクの有効期限が切れています")
    
    # R2から動画ファイルの取得
    try:
        response = r2_client.get_object(Bucket=settings.R2_BUCKET_NAME, Key=shared_video["r2_key"])
        content = response['Body'].read()
        
        log_security_event(
            event_type="SHARED_VIDEO_DOWNLOADED",
            user="anonymous",
            ip_address=get_client_ip(request),
            details=f"Downloaded shared video: {shared_video['compressed_filename']}, token: {share_token}"
        )
        
        # 日本語ファイル名対応のContent-Dispositionヘッダー
        import urllib.parse
        import re
        
        filename = shared_video['compressed_filename']
        
        # ASCIIセーフなファイル名を生成
        ascii_filename = re.sub(r'[^\x00-\x7F]+', '_', filename)
        if not ascii_filename or ascii_filename.replace('_', '').replace('.', '') == '':
            # 全て非ASCII文字の場合のフォールバック
            ascii_filename = "compressed_video.mp4"
        
        # RFC 5987準拠のエンコーディング
        encoded_filename = urllib.parse.quote(filename, safe='')
        
        # Content-Dispositionヘッダーを適切に構築
        content_disposition = f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{encoded_filename}"
        
        headers = {
            "Content-Disposition": content_disposition,
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        }
        
        return Response(
            content=content,
            media_type="video/mp4",
            headers=headers
        )
    except Exception as e:
        if hasattr(e, 'response') and e.response.get('Error', {}).get('Code') == '404':
            # R2にファイルが存在しない場合は共有情報も削除
            await crud.delete_shared_video_by_token(share_token)
            raise HTTPException(status_code=404, detail="共有ファイルが見つかりません")
        else:
            print(f"R2 get_object error: {e}")
            raise HTTPException(status_code=500, detail=f"ファイルのダウンロードに失敗しました: {str(e)}")

@router.get("/shares", summary="ユーザーの共有動画一覧を取得")
async def get_user_shares(
    current_user: dict = Depends(get_current_user_from_token)
):
    user_from_db = await crud.get_user_by_username(current_user["sub"])
    if not user_from_db:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
    
    shared_videos = await crud.get_shared_videos_by_user(user_from_db["id"])
    
    # 有効期限の確認と期限切れの削除（日本時間）
    jst = timezone(timedelta(hours=9))
    current_time = datetime.now(jst)
    valid_shares = []
    
    for video in shared_videos:
        expiry_date = datetime.fromisoformat(video["expiry_date"])
        if current_time > expiry_date:
            # 期限切れの場合は削除
            await crud.delete_shared_video_by_token(video["share_token"])
        else:
            valid_shares.append(video)
    
    return JSONResponse(content={"shares": valid_shares})

@router.get("/download/{filename}", summary="圧縮された動画のダウンロード")
async def download_compressed_video_endpoint(
    request: Request,
    filename: str,
    current_user: dict = Depends(get_current_user_from_token)
):
    print(f"=== ダウンロード処理開始 ===")
    print(f"Filename: {filename}")
    print(f"User: {current_user['sub']}")
    
    # ファイル名の検証とサニタイゼーション
    if not validate_filename(filename):
        print(f"無効なファイル名: {filename}")
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="INVALID_FILENAME",
            details=f"Invalid filename in download: {filename}"
        )
        raise HTTPException(status_code=400, detail="無効なファイル名です")
    
    sanitized_filename = sanitize_filename(filename)
    compressed_key = f"compressed/{sanitized_filename}"
    print(f"Sanitized filename: {sanitized_filename}")
    print(f"R2 key: {compressed_key}")
    
    try:
        # まずファイルの存在確認
        print("R2でファイル存在確認中...")
        try:
            head_response = r2_client.head_object(Bucket=settings.R2_BUCKET_NAME, Key=compressed_key)
            print(f"ファイル存在確認成功: {head_response}")
        except Exception as head_error:
            print(f"ファイル存在確認エラー: {head_error}")
            if hasattr(head_error, 'response') and head_error.response.get('Error', {}).get('Code') == 'NoSuchKey':
                log_security_violation(
                    request=request,
                    user=current_user["sub"],
                    violation_type="FILE_NOT_FOUND",
                    details=f"File not found in download: {sanitized_filename}"
                )
                raise HTTPException(status_code=404, detail="圧縮されたファイルが見つかりません。圧縮処理が完了していない可能性があります。")
            else:
                raise head_error
        
        # R2からファイルを取得
        print("R2からファイル取得中...")
        response = r2_client.get_object(Bucket=settings.R2_BUCKET_NAME, Key=compressed_key)
        print(f"R2ファイル取得成功: ContentLength={response.get('ContentLength', 'unknown')}")
        
        # 成功ログ
        log_security_event(
            event_type="VIDEO_DOWNLOADED",
            user=current_user["sub"],
            ip_address=get_client_ip(request),
            details=f"Downloaded compressed video: {sanitized_filename}"
        )
        
        # ストリーミングレスポンスとして返す（大きなファイルに対応）
        def generate():
            try:
                print("ストリーミング開始...")
                chunk_count = 0
                for chunk in response['Body'].iter_chunks(chunk_size=8192):
                    chunk_count += 1
                    if chunk_count % 1000 == 0:  # 1000チャンクごとにログ
                        print(f"ストリーミング中... チャンク数: {chunk_count}")
                    yield chunk
                print(f"ストリーミング完了。総チャンク数: {chunk_count}")
            except Exception as chunk_error:
                print(f"ストリーミングエラー: {chunk_error}")
                log_security_violation(
                    request=request,
                    user=current_user["sub"],
                    violation_type="STREAMING_ERROR",
                    details=f"Streaming error for {sanitized_filename}: {str(chunk_error)}"
                )
                raise HTTPException(status_code=500, detail="ファイルのストリーミング中にエラーが発生しました")
        
        print("StreamingResponse作成中...")
        
        # 日本語ファイル名のためのRFC 5987準拠のContent-Dispositionヘッダー
        import urllib.parse
        import re
        
        # ASCIIフォールバック名を生成（日本語文字を除去）
        ascii_filename = re.sub(r'[^\x00-\x7F]+', '_', sanitized_filename)
        if not ascii_filename or ascii_filename.replace('_', '') == '':
            # 全て日本語の場合のフォールバック
            ascii_filename = "compressed_video.mp4"
        
        encoded_filename = urllib.parse.quote(sanitized_filename, safe='')
        
        # RFC 5987準拠の形式でContent-Dispositionを設定
        content_disposition = f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{encoded_filename}"
        print(f"ASCII filename: {ascii_filename}")
        print(f"Encoded filename: {encoded_filename}")
        print(f"Content-Disposition: {content_disposition}")
        
        # Content-Lengthヘッダーも文字列として設定
        content_length = str(response['ContentLength']) if 'ContentLength' in response else None
        
        # ヘッダーを個別に確認
        headers_dict = {
            "Content-Disposition": content_disposition
        }
        if content_length:
            headers_dict["Content-Length"] = content_length
            
        print(f"Headers dict: {headers_dict}")
        
        streaming_response = StreamingResponse(
            generate(),
            media_type="video/mp4",
            headers=headers_dict
        )
        print("StreamingResponse作成完了")
        print("=== ダウンロード処理正常終了 ===")
        return streaming_response
        
    except HTTPException:
        # 既にHTTPExceptionが発生している場合は再送出
        print("HTTPException再送出")
        raise
    except Exception as e:
        print(f"予期しないエラー: {type(e).__name__}: {str(e)}")
        import traceback
        print(f"トレースバック: {traceback.format_exc()}")
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="DOWNLOAD_ERROR",
            details=f"Download error for {sanitized_filename}: {str(e)}"
        )
        raise HTTPException(status_code=500, detail=f"ダウンロード中にエラーが発生しました: {str(e)}")

@router.get("/check-compression/{filename}", summary="圧縮処理の完了確認")
async def check_compression_status_endpoint(
    request: Request,
    filename: str,
    current_user: dict = Depends(get_current_user_from_token)
):
    """圧縮処理が完了しているかどうかを確認するエンドポイント"""
    if not validate_filename(filename):
        raise HTTPException(status_code=400, detail="無効なファイル名です")
    
    sanitized_filename = sanitize_filename(filename)
    compressed_key = f"compressed/{sanitized_filename}"
    
    try:
        # ファイルの存在確認
        response = r2_client.head_object(Bucket=settings.R2_BUCKET_NAME, Key=compressed_key)
        
        # 成功ログ
        log_security_event(
            event_type="COMPRESSION_STATUS_CHECKED",
            user=current_user["sub"],
            ip_address=get_client_ip(request),
            details=f"Compression status checked for: {sanitized_filename}"
        )
        
        return {
            "status": "completed",
            "filename": sanitized_filename,
            "size": response.get('ContentLength', 0)
        }
        
    except Exception as e:
        if hasattr(e, 'response') and e.response.get('Error', {}).get('Code') == 'NoSuchKey':
            return {
                "status": "processing",
                "filename": sanitized_filename,
                "message": "圧縮処理がまだ完了していません"
            }
        else:
            log_security_violation(
                request=request,
                user=current_user["sub"],
                violation_type="COMPRESSION_STATUS_CHECK_ERROR",
                details=f"Error checking compression status for {sanitized_filename}: {str(e)}"
            )
            raise HTTPException(status_code=500, detail="圧縮状態の確認中にエラーが発生しました") 

@router.get("/get-download-url/{filename}", summary="直接ダウンロードURL取得")
async def get_direct_download_url_endpoint(
    request: Request,
    filename: str,
    current_user: dict = Depends(get_current_user_from_token)
):
    """圧縮された動画の直接ダウンロードURLを生成するエンドポイント"""
    print(f"=== 直接ダウンロードURL生成開始 ===")
    print(f"Filename: {filename}")
    print(f"User: {current_user['sub']}")
    
    # ファイル名の検証とサニタイゼーション
    if not validate_filename(filename):
        print(f"無効なファイル名: {filename}")
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="INVALID_FILENAME",
            details=f"Invalid filename in direct download URL: {filename}"
        )
        raise HTTPException(status_code=400, detail="無効なファイル名です")
    
    sanitized_filename = sanitize_filename(filename)
    compressed_key = f"compressed/{sanitized_filename}"
    print(f"Sanitized filename: {sanitized_filename}")
    print(f"R2 key: {compressed_key}")
    
    try:
        # ファイルの存在確認
        print("R2でファイル存在確認中...")
        try:
            head_response = r2_client.head_object(Bucket=settings.R2_BUCKET_NAME, Key=compressed_key)
            print(f"ファイル存在確認成功: {head_response}")
        except Exception as head_error:
            print(f"ファイル存在確認エラー: {head_error}")
            if hasattr(head_error, 'response') and head_error.response.get('Error', {}).get('Code') == 'NoSuchKey':
                log_security_violation(
                    request=request,
                    user=current_user["sub"],
                    violation_type="FILE_NOT_FOUND",
                    details=f"File not found in direct download URL: {sanitized_filename}"
                )
                raise HTTPException(status_code=404, detail="圧縮されたファイルが見つかりません。圧縮処理が完了していない可能性があります。")
            else:
                raise head_error
        
        # R2から署名付きURLを生成
        print("R2から署名付きURL生成中...")
        download_url = r2_client.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': settings.R2_BUCKET_NAME, 
                'Key': compressed_key,
                'ResponseContentDisposition': f'attachment; filename="{sanitized_filename}"'
            },
            ExpiresIn=settings.R2_DIRECT_DOWNLOAD_URL_EXPIRE_SECONDS
        )
        print(f"署名付きURL生成完了: {download_url[:50]}...")
        
        # 成功ログ
        log_security_event(
            event_type="DIRECT_DOWNLOAD_URL_GENERATED",
            user=current_user["sub"],
            ip_address=get_client_ip(request),
            details=f"Generated direct download URL for: {sanitized_filename}"
        )
        
        print("=== 直接ダウンロードURL生成正常終了 ===")
        return {
            "download_url": download_url,
            "filename": sanitized_filename,
            "expires_in": settings.R2_DIRECT_DOWNLOAD_URL_EXPIRE_SECONDS,
            "size": head_response.get('ContentLength', 0)
        }
        
    except HTTPException:
        # 既にHTTPExceptionが発生している場合は再送出
        print("HTTPException再送出")
        raise
    except Exception as e:
        print(f"予期しないエラー: {type(e).__name__}: {str(e)}")
        import traceback
        print(f"トレースバック: {traceback.format_exc()}")
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="DIRECT_DOWNLOAD_URL_ERROR",
            details=f"Direct download URL error for {sanitized_filename}: {str(e)}"
        )
        raise HTTPException(status_code=500, detail=f"直接ダウンロードURLの生成中にエラーが発生しました: {str(e)}") 

# 動画管理機能のAPIエンドポイント
@router.put("/manage/update-expiry/{share_token}", summary="共有動画の有効期限を更新")
async def update_video_expiry(
    request: Request,
    share_token: str,
    new_expiry_days: int,
    current_user: dict = Depends(get_current_user_from_token)
):
    """共有動画の有効期限を更新するエンドポイント"""
    print(f"=== 有効期限更新開始 ===")
    print(f"Share token: {share_token}")
    print(f"New expiry days: {new_expiry_days}")
    print(f"User: {current_user['sub']}")
    
    # 有効期限日数の検証
    if new_expiry_days < 1 or new_expiry_days > 365:
        raise HTTPException(status_code=400, detail="有効期限は1日から365日の間で指定してください")
    
    try:
        # ユーザーIDを取得
        user_info = await crud.get_user_by_username(current_user["sub"])
        if not user_info:
            raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
        
        user_id = user_info["id"]
        
        # 共有動画の存在確認と所有者確認
        video = await crud.get_shared_video_by_token_and_user(share_token, user_id)
        if not video:
            raise HTTPException(status_code=404, detail="共有動画が見つからないか、アクセス権限がありません")
        
        # 新しい有効期限を計算
        from datetime import datetime, timezone, timedelta
        jst = timezone(timedelta(hours=9))
        new_expiry_date = (datetime.now(jst) + timedelta(days=new_expiry_days)).isoformat()
        
        # データベースを更新
        success = await crud.update_shared_video_expiry(share_token, new_expiry_date, user_id)
        if not success:
            raise HTTPException(status_code=500, detail="有効期限の更新に失敗しました")
        
        # 成功ログ
        log_security_event(
            event_type="VIDEO_EXPIRY_UPDATED",
            user=current_user["sub"],
            ip_address=get_client_ip(request),
            details=f"Updated expiry for video: {video['original_filename']} to {new_expiry_days} days"
        )
        
        print("=== 有効期限更新正常終了 ===")
        return {
            "message": "有効期限が正常に更新されました",
            "share_token": share_token,
            "new_expiry_date": new_expiry_date,
            "new_expiry_days": new_expiry_days
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"予期しないエラー: {type(e).__name__}: {str(e)}")
        import traceback
        print(f"トレースバック: {traceback.format_exc()}")
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="EXPIRY_UPDATE_ERROR",
            details=f"Error updating expiry for {share_token}: {str(e)}"
        )
        raise HTTPException(status_code=500, detail=f"有効期限の更新中にエラーが発生しました: {str(e)}")

@router.delete("/manage/delete/{share_token}", summary="共有動画を削除")
async def delete_shared_video(
    request: Request,
    share_token: str,
    current_user: dict = Depends(get_current_user_from_token)
):
    """共有動画を削除するエンドポイント（R2ストレージからも削除）"""
    print(f"=== 動画削除開始 ===")
    print(f"Share token: {share_token}")
    print(f"User: {current_user['sub']}")
    
    try:
        # ユーザーIDを取得
        user_info = await crud.get_user_by_username(current_user["sub"])
        if not user_info:
            raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
        
        user_id = user_info["id"]
        
        # 共有動画の存在確認と所有者確認
        video = await crud.get_shared_video_by_token_and_user(share_token, user_id)
        if not video:
            raise HTTPException(status_code=404, detail="共有動画が見つからないか、アクセス権限がありません")
        
        # R2ストレージからファイルを削除
        try:
            r2_client.delete_object(
                Bucket=settings.R2_BUCKET_NAME,
                Key=video['r2_key']
            )
            print(f"R2ストレージからファイル削除完了: {video['r2_key']}")
        except Exception as r2_error:
            print(f"R2ストレージからの削除エラー（無視）: {r2_error}")
            # R2からの削除に失敗してもデータベースからは削除を続行
        
        # データベースから削除
        success = await crud.delete_shared_video_by_token_and_user(share_token, user_id)
        if not success:
            raise HTTPException(status_code=500, detail="動画の削除に失敗しました")
        
        # 成功ログ
        log_security_event(
            event_type="VIDEO_DELETED",
            user=current_user["sub"],
            ip_address=get_client_ip(request),
            details=f"Deleted video: {video['original_filename']}"
        )
        
        print("=== 動画削除正常終了 ===")
        return {
            "message": "動画が正常に削除されました",
            "share_token": share_token,
            "deleted_filename": video['original_filename']
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"予期しないエラー: {type(e).__name__}: {str(e)}")
        import traceback
        print(f"トレースバック: {traceback.format_exc()}")
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="VIDEO_DELETE_ERROR",
            details=f"Error deleting video {share_token}: {str(e)}"
        )
        raise HTTPException(status_code=500, detail=f"動画の削除中にエラーが発生しました: {str(e)}")

@router.get("/manage/stats", summary="ユーザーの動画統計情報を取得")
async def get_user_video_stats(
    request: Request,
    current_user: dict = Depends(get_current_user_from_token)
):
    """ユーザーの動画統計情報を取得するエンドポイント"""
    print(f"=== 統計情報取得開始 ===")
    print(f"User: {current_user['sub']}")
    
    try:
        # ユーザーIDを取得
        user_info = await crud.get_user_by_username(current_user["sub"])
        if not user_info:
            raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
        
        user_id = user_info["id"]
        
        # 統計情報を取得
        stats = await crud.get_user_video_stats(user_id)
        
        # 成功ログ
        log_security_event(
            event_type="VIDEO_STATS_RETRIEVED",
            user=current_user["sub"],
            ip_address=get_client_ip(request),
            details=f"Retrieved video stats: {stats}"
        )
        
        print("=== 統計情報取得正常終了 ===")
        return stats
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"予期しないエラー: {type(e).__name__}: {str(e)}")
        import traceback
        print(f"トレースバック: {traceback.format_exc()}")
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="STATS_RETRIEVAL_ERROR",
            details=f"Error retrieving stats: {str(e)}"
        )
        raise HTTPException(status_code=500, detail=f"統計情報の取得中にエラーが発生しました: {str(e)}")

@router.get("/manage/videos", summary="ユーザーの動画一覧を取得（管理用）")
async def get_user_videos_for_management(
    request: Request,
    current_user: dict = Depends(get_current_user_from_token)
):
    """ユーザーの動画一覧を取得するエンドポイント（管理ページ用）"""
    print(f"=== 動画一覧取得開始（管理用） ===")
    print(f"User: {current_user['sub']}")
    
    try:
        # ユーザーIDを取得
        user_info = await crud.get_user_by_username(current_user["sub"])
        if not user_info:
            raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
        
        user_id = user_info["id"]
        
        # 動画一覧を取得
        videos = await crud.get_shared_videos_by_user(user_id)
        
        # 各動画の詳細情報を追加
        from datetime import datetime, timezone, timedelta
        jst = timezone(timedelta(hours=9))
        current_time = datetime.now(jst).isoformat()
        
        enhanced_videos = []
        for video in videos:
            # 共有URLを生成
            share_url = f"{settings.FRONTEND_URL}/share/{video['share_token']}"
            
            # 期限切れかどうかを判定
            is_expired = video['expiry_date'] < current_time
            
            # 残り日数を計算
            try:
                expiry_date = datetime.fromisoformat(video['expiry_date'])
                remaining_days = (expiry_date - datetime.now(jst)).days
                remaining_days = max(0, remaining_days) if not is_expired else 0
            except:
                remaining_days = 0
            
            enhanced_video = {
                **video,
                "share_url": share_url,
                "is_expired": is_expired,
                "remaining_days": remaining_days
            }
            enhanced_videos.append(enhanced_video)
        
        # 成功ログ
        log_security_event(
            event_type="VIDEO_LIST_RETRIEVED",
            user=current_user["sub"],
            ip_address=get_client_ip(request),
            details=f"Retrieved {len(enhanced_videos)} videos for management"
        )
        
        print("=== 動画一覧取得正常終了（管理用） ===")
        return {
            "videos": enhanced_videos,
            "total_count": len(enhanced_videos)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"予期しないエラー: {type(e).__name__}: {str(e)}")
        import traceback
        print(f"トレースバック: {traceback.format_exc()}")
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="VIDEO_LIST_RETRIEVAL_ERROR",
            details=f"Error retrieving video list: {str(e)}"
        )
        raise HTTPException(status_code=500, detail=f"動画一覧の取得中にエラーが発生しました: {str(e)}")