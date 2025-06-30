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
            error_message = f"FFmpeg error (code {return_code}): {stderr_output.decode()}"
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

    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        error_message = "FFmpeg processing timed out"
        if client_id in clients:
            try:
                await clients[client_id].send_text(json.dumps({"type": "error", "detail": error_message}))
            except Exception as e:
                pass
        raise HTTPException(status_code=500, detail=error_message)
    except Exception as e:
        error_message = f"FFmpeg processing failed: {str(e)}"
        if client_id in clients:
            try:
                await clients[client_id].send_text(json.dumps({"type": "error", "detail": error_message}))
            except Exception as ws_e:
                pass
        raise HTTPException(status_code=500, detail=error_message)

    stderr_output = await process.stderr.read()
    if stderr_output:
        pass

def build_ffmpeg_options(crf: int, resolution: str, width: Optional[str], height: Optional[str]) -> list:
    scale_map = {
        "4320p": "7680:4320", "2160p": "3840:2160", "1440p": "2560:1440",
        "1080p": "1920:1080", "720p": "1280:720", "480p": "854:480", "360p": "640:360"
    }
    ffmpeg_options = ["-vcodec", "libx264", "-crf", str(crf), "-preset", "fast"]
    
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
        ExpiresIn=3600,
    )
    delete_after_delay(settings.R2_BUCKET_NAME, key, delay_seconds=3600 + 600)
    
    # 成功ログ
    log_security_event(
        event_type="UPLOAD_URL_GENERATED",
        user=current_user["sub"],
        ip_address=get_client_ip(request),
        details=f"Generated upload URL for file: {sanitized_filename}, size: {file_size}"
    )
    
    return {"upload_url": presigned_url, "key": key}

async def run_ffmpeg_job_r2(
    job_id: str, key: str, filename: str, ffmpeg_options: list, client_id: str
):
    fd_input, temp_input = tempfile.mkstemp(suffix=".mp4")
    fd_output, temp_output = tempfile.mkstemp(suffix=".mp4")
    os.close(fd_input)
    os.close(fd_output)

    try:
        r2_client.download_file(settings.R2_BUCKET_NAME, key, temp_input)
        await run_ffmpeg_process(temp_input, temp_output, ffmpeg_options, client_id)

        base, ext = os.path.splitext(filename)
        compressed_filename = f"{base}_compressed{ext}"
        compressed_key = f"compressed/{compressed_filename}"
        r2_client.upload_file(temp_output, settings.R2_BUCKET_NAME, compressed_key)

        if client_id in clients:
            url = r2_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': settings.R2_BUCKET_NAME, 'Key': compressed_key},
                ExpiresIn=3600
            )
            file_size = os.path.getsize(temp_output)
            await clients[client_id].send_text(json.dumps({
                "type": "done", "url": url,
                "filename": compressed_filename, "size": file_size
            }))
            try:
                r2_client.head_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
                r2_client.delete_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
            except Exception as e:
                if hasattr(e, 'response') and e.response.get('Error', {}).get('Code') == '404':
                    pass
                else:
                    pass
    except HTTPException as e:
        if client_id in clients:
            try: await clients[client_id].send_text(json.dumps({"type": "error", "detail": e.detail}))
            except: pass
    except Exception as e:
        if client_id in clients:
            try: await clients[client_id].send_text(json.dumps({"type": "error", "detail": str(e)}))
            except: pass
    finally:
        if os.path.exists(temp_input): os.remove(temp_input)
        if os.path.exists(temp_output): os.remove(temp_output)

@router.post("/compress/async/", summary="R2経由での非同期動画圧縮")
async def compress_video_async_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    key: str = Form(...),
    filename: str = Form(...),
    crf: int = Form(28),
    resolution: str = Form("source"),
    width: Optional[str] = Form(None),
    height: Optional[str] = Form(None),
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
    ffmpeg_options = build_ffmpeg_options(crf, resolution, width, height)
    background_tasks.add_task(run_ffmpeg_job_r2, job_id, key, filename, ffmpeg_options, client_id)
    
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
    return JSONResponse(content={"job_id": job_id, "status": "started"})

@router.post("/upload/", summary="ローカルでの動画アップロードと圧縮")
async def upload_and_compress_local_endpoint(
    request: Request,
    file: UploadFile = File(...),
    filename: str = Form(...),
    crf: int = Form(28),
    resolution: str = Form("source"),
    width: Optional[str] = Form(None),
    height: Optional[str] = Form(None),
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

        ffmpeg_options = build_ffmpeg_options(crf, resolution, width, height)

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
    # ファイル名の検証とサニタイゼーション
    if not validate_filename(filename):
        log_security_violation(
            request=request,
            user=current_user["sub"],
            violation_type="INVALID_FILENAME",
            details=f"Invalid filename in download: {filename}"
        )
        raise HTTPException(status_code=400, detail="無効なファイル名です")
    
    sanitized_filename = sanitize_filename(filename)
    compressed_key = f"compressed/{sanitized_filename}"
    
    try:
        # R2からファイルを取得
        response = r2_client.get_object(Bucket=settings.R2_BUCKET_NAME, Key=compressed_key)
        
        # 成功ログ
        log_security_event(
            event_type="VIDEO_DOWNLOADED",
            user=current_user["sub"],
            ip_address=get_client_ip(request),
            details=f"Downloaded compressed video: {sanitized_filename}"
        )
        
        # ストリーミングレスポンスとして返す（大きなファイルに対応）
        def generate():
            for chunk in response['Body'].iter_chunks(chunk_size=8192):
                yield chunk
        
        return StreamingResponse(
            generate(),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f"attachment; filename={sanitized_filename}",
                "Content-Length": str(response['ContentLength']) if 'ContentLength' in response else None
            }
        )
        
    except Exception as e:
        if hasattr(e, 'response') and e.response.get('Error', {}).get('Code') == 'NoSuchKey':
            log_security_violation(
                request=request,
                user=current_user["sub"],
                violation_type="FILE_NOT_FOUND",
                details=f"File not found in download: {sanitized_filename}"
            )
            raise HTTPException(status_code=404, detail="ファイルが見つかりません")
        else:
            log_security_violation(
                request=request,
                user=current_user["sub"],
                violation_type="DOWNLOAD_ERROR",
                details=f"Download error for {sanitized_filename}: {str(e)}"
            )
            raise HTTPException(status_code=500, detail="ダウンロード中にエラーが発生しました") 