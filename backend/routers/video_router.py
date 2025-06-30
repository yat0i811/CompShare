from fastapi import (
    APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Response, 
    Request, Depends, BackgroundTasks, File, Form, UploadFile, Query
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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

router = APIRouter()

clients: Dict[str, WebSocket] = {}

def create_r2_client():
    return boto3.client(
        's3',
        endpoint_url=settings.R2_ENDPOINT_URL,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        config=boto3.session.Config(signature_version="s3v4"),
        region_name="auto"
    )
r2_client = create_r2_client()

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
                 "Cannot load libcuda.so.1" in error_message or "Error initializing output stream" in error_message)):
                
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
        # 4K以上の場合は-levelオプションを付けない
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
        # 4K未満の場合のみ-levelを付与
        if not (input_file and get_video_resolution(input_file)[0] >= 3840 or get_video_resolution(input_file)[1] >= 2160):
            ffmpeg_options.extend(["-level", appropriate_level])
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
                "filename": compressed_filename, "size": file_size
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
    bitrate: float = Form(3.0),
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
    bitrate: float = Form(3.0),
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

    return Response(content=content, media_type="video/mp4")

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