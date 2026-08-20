from fastapi import (
    APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Response, 
    Request, Depends, BackgroundTasks, File, Form, UploadFile, Query
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse
import os, uuid, shutil, subprocess, asyncio, magic, tempfile, time, json, threading, re
from typing import Dict, Optional, List

from core.config import settings
from core import r2_transfer
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

# ストリーミング時のチャンクサイズ。
# StarletteのStreamingResponseは同期ジェネレータをiterate_in_threadpoolで回すため、
# 1チャンクごとにスレッドホップが発生する。8KBだと1GBのファイルで約13万回になるので
# 1MBまで引き上げてホップ回数を削減する。
STREAM_CHUNK_SIZE = 1024 * 1024


async def _send(client_id: str, payload: dict) -> None:
    """WebSocket送信の共通処理。接続が無い場合と送信失敗を1箇所で握る。

    進捗通知は「届かなくても処理は続行する」性質のものなので、
    ここでの失敗をジョブ本体のエラーに昇格させないこと。
    """
    ws = clients.get(client_id)
    if ws is None:
        return
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        pass


class TransferProgress:
    """boto3のCallbackから呼ばれる、スレッドセーフなバイト数カウンタ。

    【重要】このクラスは加算しかしない。I/Oもawaitも絶対に行わないこと。
    boto3のCallbackはTransferConfig.io_chunksize（既定256KB）ごとに呼ばれるため、
    1.2GBのファイルでは約4,800回呼ばれる。ここからasyncio.run_coroutine_threadsafeで
    WebSocket送信すると、イベントループに数千個のコルーチンが投入され、
    まさに今回修正したループ輻輳を自作することになる。
    送信はイベントループ側の_report_transferがポーリングして行う。

    マルチパート転送では複数のワーカースレッドから並行に呼ばれるためロックが必須。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.seen = 0

    def __call__(self, bytes_amount: int) -> None:
        with self._lock:
            self.seen += bytes_amount


async def _report_transfer(client_id: str, prog: TransferProgress, phase: str, total: int) -> None:
    """R2転送の進捗を定期的にWebSocketへ送るコルーチン。

    整数パーセントが変化したときだけ送るので、送信回数は最大100回に収まる。
    呼び出し元はasyncio.create_taskで起動し、転送完了後にcancel()すること。
    """
    if total <= 0:
        return
    last = -1
    started = time.monotonic()
    try:
        while True:
            await asyncio.sleep(settings.PROGRESS_INTERVAL_SEC)
            with prog._lock:
                seen = prog.seen
            percent = min(int(seen * 100 / total), 99)
            if percent != last:
                elapsed = time.monotonic() - started
                eta = int(elapsed * (total - seen) / seen) if seen > 0 and elapsed > 0 else None
                await _send(client_id, {
                    "type": "progress", "phase": phase, "value": percent, "etaSec": eta,
                })
                last = percent
    except asyncio.CancelledError:
        raise


class _TransferReporter:
    """転送中だけ_report_transferを走らせるための非同期コンテキストマネージャ。

    タスクを回収しないと "Task exception was never retrieved" が出るため、
    cancel後に必ずgatherする。
    """

    def __init__(self, client_id: str, phase: str, total: int):
        self.client_id = client_id
        self.phase = phase
        self.total = total
        self.prog = TransferProgress()
        self._task = None

    async def __aenter__(self) -> TransferProgress:
        await _send(self.client_id, {"type": "progress", "phase": self.phase, "value": 0})
        self._task = asyncio.create_task(
            _report_transfer(self.client_id, self.prog, self.phase, self.total)
        )
        return self.prog

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if exc_type is None:
            await _send(self.client_id, {"type": "progress", "phase": self.phase, "value": 100})
        return False


# 同時圧縮ジョブ数の制限と待ち行列。
# どちらもイベントループ上でしか触らないためロック不要。
#
# 【注意】asyncio.wait_for(sem.acquire(), timeout=...) を使わないこと。
# Python 3.10 では acquire のキャンセル時にパーミットを取りこぼす既知の問題があり、
# 圧縮枠が永久に減っていく。
_compress_sem = asyncio.Semaphore(settings.MAX_CONCURRENT_COMPRESSIONS)
_waiting: List[str] = []


async def _broadcast_queue_positions() -> None:
    """待機中の全ジョブへ現在の順番を通知する。

    キューに入った時・枠を取った時・枠を返した時にイベント駆動で呼ぶ。
    ポーリングしないので、待機中のジョブが増えてもコストが増えない。
    """
    for pos, waiting_client in enumerate(_waiting, start=1):
        await _send(waiting_client, {
            "type": "progress", "phase": "queued", "value": 0, "queuePosition": pos,
        })


class SelfDeletingFileResponse(FileResponse):
    """送出の成否にかかわらず、対象ファイルを必ず削除するFileResponse。

    StarletteのFileResponse.__call__は、レスポンス送出が例外なく完了した後にしか
    `await self.background()` に到達しない（さらにRangeヘッダが不正な場合は
    早期returnするためbackgroundはそもそも実行されない）。
    uvicornは切断済みコネクションへのsendでClientDisconnected(OSError)を送出するため、
    BackgroundTaskに削除を任せるとユーザーがタブを閉じる/ダウンロードを中断するたびに
    最大1GBの一時ファイルがコンテナの書き込み可能レイヤに残り続ける。

    __call__全体をtry/finallyで囲むことで、
    「正常送出」「クライアント切断」「その他の例外」のどの経路でも削除を保証する。
    """

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                print(f"一時ファイルの削除に失敗しました {self.path}: {cleanup_error}")


def is_r2_not_found_error(e) -> bool:
    """R2(S3互換)の「オブジェクトが存在しない」エラーかどうかを判定する。

    boto3が返すError.Codeは呼び出すAPIによって異なる:
      - head_object: ボディを持たないHTTPレスポンスのため、botocoreはHTTPステータスから
                     コードを組み立てる → '404'
      - get_object : XMLのエラーボディが返るため、そこに書かれたコードがそのまま入る
                     → 'NoSuchKey'
    片方だけを判定すると常に不一致になり、本来404を返すべき場面で500になるため、
    両方を許容する。

    'NoSuchBucket'（およびバケット不可視時の 'AccessDenied' / '403'）は意図的に含めない。
    これは「このオブジェクトが無い」ではなく「ストレージ全体が見えない」という別事象であり、
    設定ミスやアカウント障害でも真になる。呼び出し元はTrueのとき共有レコードを削除するため、
    含めてしまうと障害中に share_token → r2_key の対応が復旧不能に失われる。
    """
    if not hasattr(e, 'response'):
        return False
    code = e.response.get('Error', {}).get('Code')
    return code in ('404', 'NoSuchKey')

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
    cpu_fallback_options: list,
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

    # ffprobeをsubprocess.runで呼ぶ同期関数のためスレッドプールに逃がす
    # (機能的に等価なget_video_resolutionと同じ扱いに揃える)
    duration = await asyncio.to_thread(get_video_duration, input_path)
    percent_sent = -1
    encode_started = time.monotonic()

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
                if percent != percent_sent:
                    # phase を付けてフロントが段階を判別できるようにする。
                    # 旧フロントは未知フィールドを無視するので後方互換。
                    elapsed = time.monotonic() - encode_started
                    eta = int(elapsed * (100 - percent) / percent) if percent > 0 else None
                    await _send(client_id, {
                        "type": "progress", "phase": "encoding", "value": percent, "etaSec": eta,
                    })
                    percent_sent = percent
        
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
                
                # CPUエンコーダーで再試行（呼び出し元が構築済みのcpu_fallback_optionsをそのまま使う）
                command = ["ffmpeg", "-y", "-i", input_path] + cpu_fallback_options + ["-progress", "pipe:1", "-nostats", output_path]
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
                # プローブは本番と同じオプション列で行う。ここで通れば本番コマンドも通る
                # （逆にp5/tune未対応のビルドではプローブが失敗して、正しくCPUフォールバック＋
                # warning通知に落ちる）。プローブと本番の乖離で「GPU可と判定したのに実行時に
                # Unrecognized optionでハードエラー」になる事故を防ぐため、
                # build_encoder_optionsから引数を生成する。
                # resolution="source"を渡すのは、プローブの合成入力(testsrc)に対して
                # -vf scaleを付けないため（この引数の組み合わせでは-vfは生成されない）。
                # 戻り値は"-vcodec h264_nvenc"で始まる完全な列なので、-c:vは別途付けない。
                probe_encoder_options = build_encoder_options(
                    NVENC_ENCODER,
                    crf=28,
                    resolution="source",
                    width=None,
                    height=None,
                    source_resolution=None,
                )
                test_result = subprocess.run(
                    ["ffmpeg", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=1"]
                    + probe_encoder_options
                    + ["-t", "1", "-f", "null", "-"],
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
    """【現在未使用】is_modern_ffmpeg分岐の削除（CLOSE_ISSUES §6-3）により本番からの呼び出し元は無い。test_ffmpeg_options.pyが「ビルド時に呼ばれないこと」の監視対象として参照している。

    FFmpegのバージョンを取得
    """
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
    """【現在未使用】導入時から呼び出し元が無い。

    NVENCエンコーダーが実際にサポートされているかチェック
    """
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

# エンコーダ名。フォールバック判定・テストで文字列比較するため定数化する。
NVENC_ENCODER = "h264_nvenc"
X264_ENCODER = "libx264"

# NVENC の -cq と libx264 の -crf に公式な換算式は存在しない。実測系の報告は
# 「同等画質には CQ を CRF より高い数値にする」方向で一致しており(+4〜+5)、
# ここでは暫定的に +4 を採る。CRF28(UI 既定)→ CQ32。
# 【要実測】実動画で数本比較して詰めること(docs\CLOSE_ISSUES.md §6-2)。
NVENC_CQ_OFFSET = 4
# -cq の値域は 0〜51。0 は「自動」の意味なので下限には使わない。
NVENC_CQ_MIN = 1
NVENC_CQ_MAX = 51


def crf_to_nvenc_cq(crf: int) -> int:
    """UI の CRF 値を NVENC の -cq 値へ写像する(クランプ付き)。

    API 側で CRF は 18〜32 に検証済みのため通常はクランプが効かないが、
    純関数として不正値でも異常な -cq を吐かない保証として入れておく。
    """
    return max(NVENC_CQ_MIN, min(NVENC_CQ_MAX, crf + NVENC_CQ_OFFSET))


def get_appropriate_level(resolution: str, width: Optional[str], height: Optional[str], source_resolution: Optional[tuple[int, int]] = None) -> str:
    """解像度に応じて適切なH.264レベルを選択"""
    # 実際の動画解像度は、呼び出し元がジョブ全体で1回だけ取得したものを受け取る
    # (この関数内ではffprobeを呼ばない。1ジョブ1回のプローブという前提を壊さないため)
    actual_width, actual_height = source_resolution or (1920, 1080)

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

def build_encoder_options(
    encoder: str,
    crf: int,
    resolution: str,
    width: Optional[str],
    height: Optional[str],
    source_resolution: Optional[tuple[int, int]] = None,
) -> list:
    """エンコーダを明示してFFmpegのオプション列を組み立てる。

    【重要】この関数はサブプロセスを一切起動しない純関数にすること。
    ffprobe / ffmpeg -versionを呼び戻すと、1ジョブ1回のプローブという前提が壊れ、
    CPUフォールバック時にも再プローブが走る（test_ffmpeg_options.pyが検知する）。
    """
    scale_map = {
        "4320p": "7680:4320", "2160p": "3840:2160", "1440p": "2560:1440",
        "1080p": "1920:1080", "720p": "1280:720", "480p": "854:480", "360p": "640:360"
    }

    # 【ログ方針】この関数はエンコーダ選択のログを出さない。CPUフォールバック用の
    # cpu_fallback_optionsはGPU成功ジョブでも必ず構築されるため、ここでログを出すと
    # GPUジョブでも「Using CPU encoder (libx264)」が出て、ログでのGPU/CPU判定を誤らせる。
    # 実際に採用したエンコーダのログはbuild_ffmpeg_options側の1箇所に集約している。
    if encoder == NVENC_ENCODER:
        # NVENCエンコーダーの最適化設定（CQ方式。ビットレート制御は廃止）
        # NVENCエンコーダーでは-levelパラメータを指定しない（サポートされていないため）
        cq = crf_to_nvenc_cq(crf)
        ffmpeg_options = [
            "-vcodec", "h264_nvenc",
            "-preset", "p5",            # 品質寄りの折衷。p1(最速)〜p7(最高品質)。旧mediumはp4相当
            "-tune", "hq",              # 品質チューニング（旧-tune llは低遅延用でありCQ用途では誤り）
            "-rc", "vbr",               # CQモードはvbr+cq（旧vbr_hqはSDK v12で非推奨）
            "-cq", str(cq),
            "-b:v", "0",                # 必須。省略すると既定2Mが効くバージョンがあり得るための明示
            "-profile:v", "main",       # メインプロファイル（圧縮効率向上）
            "-g", "30",                 # GOPサイズ
            "-keyint_min", "30",        # 最小キーフレーム間隔
            "-bf", "3",                 # Bフレーム数（圧縮効率向上）
            "-refs", "3",               # 参照フレーム数
            "-spatial-aq", "1",         # 空間AQ有効化（平坦部へのビット配分改善）
            "-rc-lookahead", "20",      # 先読みフレーム数（NVIDIA推奨の10〜20）
        ]
    else:
        # CPUエンコーダー（libx264）の設定
        appropriate_level = get_appropriate_level(resolution, width, height, source_resolution)
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


def build_ffmpeg_options(crf: int, resolution: str, width: Optional[str], height: Optional[str], use_gpu: bool = False, source_resolution: Optional[tuple[int, int]] = None) -> list:
    """使用エンコーダを判定してオプション列を返す。

    【注意】is_gpu_encoder_available()がNVENCのテストエンコード(timeout=30)を
    subprocessで実行するため、呼び出し側は必ずasyncio.to_thread経由にすること。
    """
    gpu_available = is_gpu_encoder_available()
    print(f"GPU use requested: {use_gpu}")
    print(f"GPU encoder available: {gpu_available}")
    encoder = NVENC_ENCODER if (use_gpu and gpu_available) else X264_ENCODER
    print(f"Using {'GPU' if encoder == NVENC_ENCODER else 'CPU'} encoder ({encoder})")
    return build_encoder_options(encoder, crf, resolution, width, height, source_resolution)

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
    # daemon=Trueが無いと、インタプリタ終了時にこのスレッド（最大2.5時間sleepする）の
    # 完了をPythonが待ってしまい、コンテナ停止がSIGKILLタイムアウトまでハングする。
    threading.Thread(target=delayed, daemon=True).start()

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
    
    # dict.get(key, default) はキーが存在して値がNULL(None)のときdefaultではなくNoneを返す。
    # DBのupload_capacity_bytes列はNULL許容のため、None時も100MBへフォールバックさせる
    # (そうしないと後続の file_size > None が TypeError になり500を返す)
    user_capacity = user_from_db.get("upload_capacity_bytes") or 104857600 # Default to 100MB
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
    job_id: str, key: str, filename: str, crf: int, resolution: str, width: Optional[str], height: Optional[str], use_gpu: bool, client_id: str
):
    fd_input, temp_input = tempfile.mkstemp(suffix=".mp4")
    fd_output, temp_output = tempfile.mkstemp(suffix=".mp4")
    os.close(fd_input)
    os.close(fd_output)

    print(f"=== GPU圧縮デバッグ情報 ===")
    print(f"Job ID: {job_id}")
    print(f"Use GPU: {use_gpu}")
    print(f"Input file: {temp_input}")
    print(f"Output file: {temp_output}")

    # 同時圧縮数を制限する。
    # セマフォはダウンロード〜圧縮〜アップロードのジョブ全体を囲むこと。
    # ffmpeg部分だけを囲うと、上限を超える数のダウンロードが同時進行して
    # 一時ファイル（1ジョブあたりソースの約2倍）でディスクを食い潰す。
    sem_acquired = False
    try:
        if _compress_sem.locked():
            _waiting.append(client_id)
            await _broadcast_queue_positions()
        try:
            await _compress_sem.acquire()
            sem_acquired = True
        finally:
            if client_id in _waiting:
                _waiting.remove(client_id)
                await _broadcast_queue_positions()

        # R2からファイルをダウンロード
        # boto3は同期APIのため、async関数内で直接呼ぶとイベントループ全体が停止する。
        # 大容量動画では約74秒停止し、レスポンス送出不能によるnginxのHTTP 499や、
        # /auth/me応答不能によるuserInfo未取得→容量誤判定(100MBフォールバック)を引き起こしていた。
        print("R2からファイルをダウンロード中...")
        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
        head = await r2_transfer.run_r2(r2_client.head_object, Bucket=settings.R2_BUCKET_NAME, Key=key)
        source_size = head.get("ContentLength", 0)
        async with _TransferReporter(client_id, "fetching", source_size) as prog:
            await r2_transfer.download_file(settings.R2_BUCKET_NAME, key, temp_input, callback=prog)
        print(f"ダウンロード完了。ファイルサイズ: {os.path.getsize(temp_input)} bytes")

        # 解像度の取得はジョブ全体でここ1回だけ。build_ffmpeg_options側では再取得しない。
        source_resolution = await asyncio.to_thread(get_video_resolution, temp_input)
        print(f"Actual video resolution: {source_resolution[0]}x{source_resolution[1]}")

        # is_gpu_encoder_available()（NVENCテストエンコード timeout=30）をsubprocessで
        # 実行するためスレッドプールに逃がす。
        ffmpeg_options = await asyncio.to_thread(
            build_ffmpeg_options, crf, resolution, width, height, use_gpu, source_resolution
        )
        # NVENC失敗時の再試行用。純関数（サブプロセスなし）なのでto_threadは不要。
        cpu_fallback_options = build_encoder_options(
            X264_ENCODER, crf, resolution, width, height, source_resolution
        )
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
        await run_ffmpeg_process(temp_input, temp_output, ffmpeg_options, cpu_fallback_options, client_id)
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
        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
        async with _TransferReporter(client_id, "storing", os.path.getsize(temp_output)) as prog:
            await r2_transfer.upload_file(temp_output, settings.R2_BUCKET_NAME, compressed_key, callback=prog)
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
                "r2_key": compressed_key,  # 共有機能のためにR2キーを追加
                "original_size": source_size  # フロントの圧縮前サイズ表示をR2のhead_object値で上書きするため
            }))
            print("WebSocket通知送信完了")

        # 元ファイルの削除。
        # 【重要】この処理は必ず `if client_id in clients:` の外に置くこと。
        # 以前は通知処理と一緒に if の内側にあったため、ユーザーがページをリロードして
        # WebSocket が切れていると元ファイルが削除されず、R2 に 1.2GB のゴミが残っていた。
        # 通知が届くかどうかと、アップロード元を片付けるかどうかは無関係である。
        try:
            # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
            await r2_transfer.run_r2(r2_client.head_object, Bucket=settings.R2_BUCKET_NAME, Key=key)
            await r2_transfer.run_r2(r2_client.delete_object, Bucket=settings.R2_BUCKET_NAME, Key=key)
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
        # 圧縮枠を返す。ここで漏らすと枠が永久に減り、以後のジョブが待たされ続ける。
        if sem_acquired:
            _compress_sem.release()
            await _broadcast_queue_positions()
        print("=== GPU圧縮デバッグ情報終了 ===")

@router.post("/compress/async/", summary="R2経由での非同期動画圧縮")
async def compress_video_async_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    key: str = Form(...),
    filename: str = Form(...),
    crf: int = Form(28),
    # 【廃止済み・後方互換のため受理のみ】GPUもCRF指定に統一したため未使用。
    # 古いフロントのキャッシュがこのフィールドを送ってくる間、422/400にしないために残す。
    # フロントの送信が完全に無くなったことを確認できたら削除してよい。
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
    background_tasks.add_task(run_ffmpeg_job_r2, job_id, key, filename, crf, resolution, width, height, use_gpu, client_id)
    
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
    # 【廃止済み・後方互換のため受理のみ】GPUもCRF指定に統一したため未使用。
    # 古いフロントのキャッシュがこのフィールドを送ってくる間、422/400にしないために残す。
    # フロントの送信が完全に無くなったことを確認できたら削除してよい。
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

    # dict.get(key, default) はキーが存在して値がNULL(None)のときdefaultではなくNoneを返す。
    # DBのupload_capacity_bytes列はNULL許容のため、None時も100MBへフォールバックさせる
    # (そうしないと後続の file_size > None が TypeError になり500を返す)
    user_capacity = user_from_db.get("upload_capacity_bytes") or 104857600 # Default to 100MB
    
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
        # ファイル全体を一度にメモリへ載せると最大1GBを消費し、かつ同期writeがループを塞ぐ。
        # UploadFile.read(size)はStarletteがスレッドプールで実行する非同期API、
        # ディスクへのwriteはto_threadへ逃がすことで、チャンク単位でループを解放する。
        def _write_chunk(fh, data):
            fh.write(data)

        # openもos.openを内部で呼ぶ同期呼び出しのため、closeも含めてスレッドプールに逃がす
        # （ruff ASYNC230: async関数内でのopen()直接呼び出しを禁止するルールへの対応）。
        f = await asyncio.to_thread(open, temp_input, "wb")
        try:
            while True:
                chunk = await file.read(STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                await asyncio.to_thread(_write_chunk, f, chunk)
        finally:
            await asyncio.to_thread(f.close)

        # python-magicはファイルを同期で読むためスレッドプールに逃がす
        if not await asyncio.to_thread(is_safe_video, temp_input):
            log_security_violation(
                request=request,
                user=current_user["sub"],
                violation_type="UNSAFE_VIDEO_FILE",
                details=f"Unsafe video file detected: {sanitized_filename}"
            )
            os.remove(temp_input)
            raise HTTPException(status_code=400, detail="Invalid or unsupported video file")

        # 解像度の取得はジョブ全体でここ1回だけ。build_ffmpeg_options側では再取得しない
        # （R2経路のrun_ffmpeg_job_r2と同じ扱いに揃える）。
        source_resolution = await asyncio.to_thread(get_video_resolution, temp_input)
        print(f"Actual video resolution: {source_resolution[0]}x{source_resolution[1]}")

        # is_gpu_encoder_available()（NVENCテストエンコード timeout=30）をsubprocess.runするため、
        # 最悪30秒ループを止める。スレッドプールに逃がす。
        ffmpeg_options = await asyncio.to_thread(
            build_ffmpeg_options, crf, resolution, width, height, use_gpu, source_resolution
        )
        # NVENC失敗時の再試行用。純関数（サブプロセスなし）なのでto_threadは不要。
        cpu_fallback_options = build_encoder_options(
            X264_ENCODER, crf, resolution, width, height, source_resolution
        )

        # GPU使用が要求されたが利用できない場合の通知
        if use_gpu and "h264_nvenc" not in ffmpeg_options and client_id in clients:
            try:
                await clients[client_id].send_text(json.dumps({
                    "type": "warning",
                    "detail": "GPUエンコーダーが利用できません。CPUエンコーダーで処理を続行します。"
                }))
            except Exception as e:
                pass

        await run_ffmpeg_process(temp_input, temp_output, ffmpeg_options, cpu_fallback_options, client_id)
        
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

    # 圧縮後ファイル全体を同期readでメモリに載せると最大1GBを消費し、readの間ループも止まる。
    # FileResponseはStarletteがスレッドプールでチャンク送出するため、両方を回避できる。
    # temp_outputはSelfDeletingFileResponseが送出後に必ず削除する
    # （クライアント切断・例外を含むどの経路でも削除される。BackgroundTaskでは切断時に漏れる）。
    if os.path.exists(temp_input): os.remove(temp_input)

    # CORSヘッダーを明示的に追加
    response_headers = {}
    origin = request.headers.get("origin")
    if origin and origin in settings.CORS_ALLOWED_ORIGINS:
        response_headers["Access-Control-Allow-Origin"] = origin
        response_headers["Access-Control-Allow-Credentials"] = "true"
        response_headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response_headers["Access-Control-Allow-Headers"] = "*"

    return SelfDeletingFileResponse(
        temp_output,
        media_type="video/mp4",
        headers=response_headers
    )

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

def _build_share_url(share_token: str) -> str:
    """共有URLの唯一の生成元。

    request.url.scheme を使わないこと。uvicornは--proxy-headersのみで
    --forwarded-allow-ipsが既定(127.0.0.1)のため、コンテナIPから来るnginxの
    X-Forwarded-Protoが信用されずschemeがhttpになる（実際にhttp://と表示された）。
    """
    return f"{settings.FRONTEND_URL.rstrip('/')}/share/{share_token}"


async def _load_shared_video_or_raise(share_token: str) -> dict:
    """共有トークンの検証と有効期限判定を行う共通ヘルパー。R2には触れない（Class Bを消費しない）。

    404: レコード無し / 410: 期限切れ（DBから削除してから送出）
    """
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

    return shared_video


async def _head_shared_object_or_raise(share_token: str, r2_key: str) -> dict:
    """共有ファイルのhead_objectを取得する共通ヘルパー。

    404のときは共有レコードも削除して404を送出する。その他の例外は500。
    呼び出し元はClass Bを1回消費する点に注意（head_objectもClass B操作）。
    """
    try:
        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
        return await r2_transfer.run_r2(r2_client.head_object, Bucket=settings.R2_BUCKET_NAME, Key=r2_key)
    except Exception as e:
        # head_objectのNot Foundは '404'（'NoSuchKey'ではない）
        if is_r2_not_found_error(e):
            await crud.delete_shared_video_by_token(share_token)
            raise HTTPException(status_code=404, detail="共有ファイルが見つかりません")
        # 例外の文字列表現にはバケット名・オペレーション名・エンドポイントURL・RequestIdが含まれる。
        # このヘルパーは認証不要の /share/{token} 系から呼ばれるため、共有リンクを持つ
        # 第三者にストレージ構成が漏れる。詳細はサーバーログにのみ残し、応答は固定文言にする。
        print(f"R2 head_object error: {e}")
        raise HTTPException(status_code=500, detail="ファイル情報の取得に失敗しました")


def _normalize_content_type(raw: Optional[str]) -> str:
    """R2のContentTypeを正規化する。

    boto3のupload_fileは拡張子からvideo/mp4を推測するが、
    過去に別経路で入ったオブジェクトはbinary/octet-streamのことがある。
    圧縮出力はrun_ffmpeg_job_r2が常に_compressed.mp4を作るためvideo/mp4に寄せる。
    """
    if not raw or raw.endswith("/octet-stream"):
        return "video/mp4"
    return raw


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
        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
        await r2_transfer.run_r2(r2_client.head_object, Bucket=settings.R2_BUCKET_NAME, Key=r2_key)
    except Exception as e:
        # 判定は is_r2_not_found_error に統一する（Error.Code の手書き比較を残すと、
        # get_object 側の 'NoSuchKey' を取りこぼす同種のバグが再発する）。
        if is_r2_not_found_error(e):
            raise HTTPException(status_code=404, detail="圧縮動画が見つかりません")
        else:
            print(f"R2 head_object error (share create): {e}")
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
    
    # 共有URLの生成（生成元は_build_share_urlに一本化。理由は同関数のdocstring参照）
    share_url = _build_share_url(share_token)
    
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

@router.options("/share/{share_token}/info")
async def share_info_options(share_token: str, request: Request):
    """共有メタ情報エンドポイントのOPTIONSリクエストハンドラー"""
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
    # 共有動画情報の取得（トークン検証・期限判定は共通ヘルパーに委譲）
    shared_video = await _load_shared_video_or_raise(share_token)

    # R2でファイルサイズの取得。
    # 旧実装は404以外の失敗時にfile_size=0で描画を続けていたが、ヘルパー共通化により
    # 非404エラーは500になる。このページは外部から到達不能で、唯一の内部呼び出し元だった
    # フロントのAPIルート(pages/api/share/[id].js)も本改修で削除されるため影響は無い。
    head = await _head_shared_object_or_raise(share_token, shared_video["r2_key"])
    file_size = head.get('ContentLength', 0)

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
    expiry_date = datetime.fromisoformat(shared_video["expiry_date"])
    try:
        expiry_str = expiry_date.strftime("%Y年%m月%d日 %H:%M")
    except:
        expiry_str = expiry_date.strftime("%Y-%m-%d %H:%M")

    # HTMLページの生成
    # <source src=...> と <a href=...> は {request.url.scheme}://{request.url.netloc} を廃し、
    # 相対パス（先頭スラッシュ無し）にする。
    # 先頭スラッシュ有りの "/share/{token}/preview" にしないこと。
    # nginx経由（/be/share/{token}）で開かれた場合にルート基準で解決されNext.jsへ流れてしまう。
    # 末尾セグメント基準の相対解決なら、直接アクセス・nginx経由の両方で正しく解決される:
    #   http://compshare-backend:8001/share/T   -> /share/T/preview
    #   https://compshare.yat0i.com/be/share/T  -> /be/share/T/preview
    # share_tokenはsecrets.token_urlsafe由来で[A-Za-z0-9_-]のみのためエスケープ不要。
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
                この共有リンクは有効期限があります。期限を過ぎるとアクセスできなくなります。
            </div>

            <div class="video-container">
                <video controls preload="metadata">
                    <source src="{share_token}/preview" type="video/mp4">
                    お使いのブラウザは動画の再生をサポートしていません。
                </video>
            </div>

            <div class="download-section">
                <a href="{share_token}/download" class="download-btn">
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

@router.get("/share/{share_token}/info", summary="共有動画のメタ情報取得（認証不要）")
async def shared_video_info(share_token: str):
    """共有ページ（Next.js）がブラウザから直接叩くJSON API。

    URLはここでは返さない。バックエンドはnginxの/beプレフィックスを知らないため、
    フロント側がBASE_URLから組み立てる。
    """
    # R2クライアントの初期化チェック
    if r2_client is None:
        raise HTTPException(status_code=500, detail="ストレージクライアントが初期化されていません")

    # 共有動画情報の取得（トークン検証・期限判定は共通ヘルパーに委譲）
    shared_video = await _load_shared_video_or_raise(share_token)
    head = await _head_shared_object_or_raise(share_token, shared_video["r2_key"])

    # 残り日数の計算（日本時間）。/manage/videos(get_user_videos_for_management)と同じ計算にする
    jst = timezone(timedelta(hours=9))
    expiry_date = datetime.fromisoformat(shared_video["expiry_date"])
    remaining_days = max(0, (expiry_date - datetime.now(jst)).days)

    return JSONResponse(content={
        "share_token": share_token,
        "filename": shared_video["compressed_filename"],
        "size": head.get("ContentLength", 0),
        "content_type": _normalize_content_type(head.get("ContentType")),
        "expiry_date": shared_video["expiry_date"],
        "remaining_days": remaining_days,
    })

# parse_single_byte_rangeの戻り値の型
#   None              -> Rangeを無視して200で全体を返す
#   (start, end)      -> 206。両端を含む。0 <= start <= end <= total-1 を保証
#   UNSATISFIABLE     -> 416 + Content-Range: bytes */{total}
UNSATISFIABLE = object()

# Rangeヘッダの数値部の検証パターン。ASCIIの10進数字のみを、19桁までに限って受理する。
#
# 【str.isdigit() を絶対に使わないこと】
#   isdigit() はUnicodeの上付き数字（'¹' U+00B9 / '²' / '³'）にもTrueを返すが、
#   int('¹') は ValueError を送出する。parse_single_byte_rangeは認証不要の
#   /share/{token}/preview から try で包まずに呼ばれるため、この例外はそのまま
#   エンドポイントを貫通し main.py のグローバルハンドラで HTTP 500 になる。
#   実測で到達可能: h11 は `Range: bytes=0-\xb9`（生バイト0xB9）を正常なヘッダとして
#   パースし、Starlette が latin-1 でデコードして 'bytes=0-¹' になる。nginx もこの値を
#   そのまま転送する。つまり有効な共有リンクを持つ匿名ユーザーが1リクエストで500を作れた。
#
# 【str.isdecimal() も使わないこと】
#   全角 '０-９' やアラビア数字 '٥'(U+0665) にTrueを返し、int() も通ってしまう。
#   クラッシュはしないが、HTTPのバイトレンジ仕様上これらは受理すべきでない
#   （実測: `bytes=-٥` -> (995, 999) / `bytes=０-９` -> (0, 9) を返していた）。
#
# 【19桁の上限】
#   CPython 3.10.7以降は int(文字列) の桁数を既定4300桁に制限しており、超えると
#   ValueError を送出する（CVE-2020-10735 の緩和策。本番の python3.10.12 で実測確認済み）。
#   ASCII限定にするだけでは `bytes=0-<5000桁の数字>` で再び500を作れてしまう。
#   バイト数として現実的な最大は 2^63-1（19桁）なので、20桁以上は構文不正として
#   無視し200で全体を返す（RFC 9110 上サーバはRangeを無視して200を返してよい）。
_RANGE_NUMBER_RE = re.compile(r"[0-9]{1,19}")


def _is_range_number(value: str) -> bool:
    """Rangeヘッダの数値部として int() に渡して安全かどうか。

    理由は _RANGE_NUMBER_RE のコメントを参照（isdigit / isdecimal に戻さないこと）。
    """
    return _RANGE_NUMBER_RE.fullmatch(value) is not None


def parse_single_byte_range(range_header: Optional[str], total: int):
    """RangeヘッダーをパースしてSuffix形式/開始-終了形式の単一レンジを解決する純関数。

    I/Oを一切含まない。boto3呼び出しやDBアクセスをここに混ぜないこと
    （境界条件をI/O無しでユニットテストできるようにするため。test_range.py参照）。
    """
    # 1. ヘッダ無し
    if not range_header:
        return None

    spec = range_header.strip()

    # 2. 単位がbytes以外（例 "seconds=1-2"）は無視する（RFC 9110: 未知の単位は無視してよい）
    if not spec.lower().startswith("bytes="):
        return None
    spec = spec[len("bytes="):].strip()

    # 3. 複数レンジは非対応。416ではなく200で全体を返す。
    #    RFC 9110上サーバはRangeを無視して200を返してよく、
    #    416にすると複数レンジを送るクライアントで再生不能になるため。
    #    （Chrome / Safari / Firefoxの<video>は単一レンジしか送らない）
    if "," in spec:
        return None

    # 4. 構文不正は無視して200
    if "-" not in spec:
        return None
    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()

    # 5. ゼロバイトファイル: 満たせるレンジが存在しないので必ず416（Content-Range: bytes */0）。
    #    圧縮出力が0バイトならrun_ffmpeg_job_r2が例外にするので実運用では起きないが、
    #    total-1 == -1 での添字計算事故を防ぐため先に弾く。
    if total <= 0:
        return UNSATISFIABLE

    if first == "":
        # --- suffix形式 "bytes=-N"（末尾Nバイト） ---
        if last == "":
            return None                      # "bytes=-" は構文不正 -> 200
        if not _is_range_number(last):
            return None                      # ASCII10進数字でない -> 200（isdigit禁止。理由は_RANGE_NUMBER_RE参照）
        n = int(last)
        if n == 0:
            return UNSATISFIABLE             # "bytes=-0" は満たせない -> 416
        start = max(total - n, 0)            # Nがtotalを超えても416にせず全体に丸める
        end = total - 1
    else:
        # --- "bytes=S-" / "bytes=S-E" ---
        if not _is_range_number(first):
            return None                      # 負値・非ASCII数字 -> 200（isdigit禁止。理由は_RANGE_NUMBER_RE参照）
        start = int(first)
        if start > total - 1:
            return UNSATISFIABLE             # 開始位置が範囲外 -> 416
        if last == "":
            end = total - 1
        else:
            if not _is_range_number(last):
                return None                  # 同上（isdigit禁止。理由は_RANGE_NUMBER_RE参照）
            end = min(int(last), total - 1)  # 終端が範囲外なら丸める（416にしない）
        if start > end:
            return UNSATISFIABLE             # "bytes=500-100" -> 416

    return (start, end)


@router.get("/share/{share_token}/preview", summary="共有動画のプレビューストリーミング（認証不要）")
async def shared_video_preview_stream(
    share_token: str,
    request: Request
):
    # R2クライアントの初期化チェック
    if r2_client is None:
        raise HTTPException(status_code=500, detail="ストレージクライアントが初期化されていません")

    # 共有動画情報の取得（トークン検証・期限判定は共通ヘルパーに委譲）
    shared_video = await _load_shared_video_or_raise(share_token)
    # 総サイズをhead_objectで確定させる。get_object(Range=...)のContentRangeから逆算する案は、
    # 416応答に必要なtotalが（レンジが満たせない場合は）取れないため採らない。
    head = await _head_shared_object_or_raise(share_token, shared_video["r2_key"])
    total = int(head.get("ContentLength", 0))
    content_type = _normalize_content_type(head.get("ContentType"))

    parsed = parse_single_byte_range(request.headers.get("range"), total)

    if parsed is UNSATISFIABLE:
        # ボディを持たない。get_objectを呼ばないのでClass Bも消費しない。
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"},
        )

    base_headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}

    if parsed is None:
        get_kwargs = {}
        status_code, content_length, extra_headers = 200, total, {}
    else:
        start, end = parsed
        get_kwargs = {"Range": f"bytes={start}-{end}"}
        status_code, content_length = 206, end - start + 1
        # Content-Rangeは自前のstart/end/totalから組む（応答のエコーに依存しない）。
        # 全体長と同じ範囲（bytes=0-）でも200に落とさず206のままにする。SafariはRange probeで
        # bytes=0-を送り、206が返ることを前提にしている。
        extra_headers = {"Content-Range": f"bytes {start}-{end}/{total}"}

    try:
        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
        response = await r2_transfer.run_r2(
            r2_client.get_object, Bucket=settings.R2_BUCKET_NAME, Key=shared_video["r2_key"], **get_kwargs
        )
    except Exception as e:
        # get_objectのNot Foundは 'NoSuchKey'（'404'ではない）
        if is_r2_not_found_error(e):
            await crud.delete_shared_video_by_token(share_token)
            raise HTTPException(status_code=404, detail="共有ファイルが見つかりません")
        # 認証不要のエンドポイントなので、boto3の例外文字列（バケット名・エンドポイントURL・
        # RequestIdを含む）をクライアントへ返さない。詳細はサーバーログにのみ残す。
        print(f"R2 get_object error (preview): {e}")
        raise HTTPException(status_code=500, detail="プレビューの取得に失敗しました")

    # Body.read()は使わないこと。r2_transfer.run_r2はR2専用エグゼキュータ
    # （既定でR2_EXECUTOR_MAX_WORKERS=4）で並列に走り得るため、1GB×4のOOMを
    # 作ることになる。チャンク単位のストリーミングのみを使う。
    def generate():
        try:
            for chunk in response['Body'].iter_chunks(chunk_size=STREAM_CHUNK_SIZE):
                yield chunk
        except Exception as e:
            # 握り潰すと、途中までのストリームを正常な応答として返してしまう。
            # 再送出してコネクションを異常終了させ、再生エラーとして表面化させる。
            print(f"Streaming error: {e}")
            raise
        finally:
            # Starlette 1.6.0のStreamingResponseは切断時にtask groupをキャンセルするだけで、
            # iterate_in_threadpoolは同期イテレータのclose()を呼ばない。
            # 明示的に閉じないとurllib3コネクションがbotocoreのプール
            # （既定 max_pool_connections=10）へ返却されず滞留する。
            try:
                response['Body'].close()
            except Exception:
                pass

    return StreamingResponse(
        generate(),
        status_code=status_code,
        media_type=content_type,
        headers={**base_headers, **extra_headers, "Content-Length": str(content_length)},
    )

@router.get("/share/{share_token}/download", summary="共有動画のダウンロード（認証不要）")
async def download_shared_video(
    share_token: str,
    request: Request
):
    # R2クライアントの初期化チェック
    if r2_client is None:
        raise HTTPException(status_code=500, detail="ストレージクライアントが初期化されていません")

    # 共有動画情報の取得（トークン検証・期限判定は共通ヘルパーに委譲）。
    # このエンドポイントはhead_objectを呼ばない（現行どおりget_objectのNoSuchKeyで404判定し、
    # 余計なClass Bを消費しない）ため、_head_shared_object_or_raiseは使わない。
    shared_video = await _load_shared_video_or_raise(share_token)

    # R2から動画ファイルの取得
    try:
        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）。
        # ここでBody.read()を使うとファイル全体（1GB超もあり得る）をメモリに載せることになり、
        # OOMリスクが跳ね上がる。
        # このエンドポイントは認証不要（共有トークンのみ）でレート制限の対象外なので、
        # プレビュー(shared_video_preview_stream)と同じくチャンク単位のストリーミングで返す。
        response = await r2_transfer.run_r2(r2_client.get_object, Bucket=settings.R2_BUCKET_NAME, Key=shared_video["r2_key"])

        def generate():
            try:
                for chunk in response['Body'].iter_chunks(chunk_size=STREAM_CHUNK_SIZE):
                    yield chunk
            except Exception as stream_error:
                # 握り潰すとContent-Lengthで宣言した長さより短いボディが送出され、
                # 壊れたmp4が無言でユーザーに届く（痕跡はサーバログにしか残らない）。
                # 再送出してh11に不足を検知させ、コネクションを異常終了させる。
                print(f"Shared download streaming error: {stream_error}")
                raise
            finally:
                # 例外を再送出する経路でもfinallyは実行されるため、Bodyは必ず閉じられる。
                # Starlette 1.6.0は同期イテレータのclose()を呼ばないため明示的に閉じる。
                try:
                    response['Body'].close()
                except Exception:
                    pass

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

        # 元のファイルサイズが分かる場合はContent-Lengthも返す（進捗表示のため）
        if 'ContentLength' in response:
            headers["Content-Length"] = str(response['ContentLength'])

        return StreamingResponse(
            generate(),
            media_type="video/mp4",
            headers=headers
        )
    except Exception as e:
        # get_objectのNot Foundは 'NoSuchKey'（'404'ではない）
        if is_r2_not_found_error(e):
            # R2にファイルが存在しない場合は共有情報も削除
            await crud.delete_shared_video_by_token(share_token)
            raise HTTPException(status_code=404, detail="共有ファイルが見つかりません")
        else:
            # 認証不要のエンドポイントなので、例外の詳細はログにのみ残して応答は固定文言にする
            # （_head_shared_object_or_raise / preview と同じ扱い）。
            print(f"R2 get_object error: {e}")
            raise HTTPException(status_code=500, detail="ファイルのダウンロードに失敗しました")

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
            # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
            head_response = await r2_transfer.run_r2(r2_client.head_object, Bucket=settings.R2_BUCKET_NAME, Key=compressed_key)
            print(f"ファイル存在確認成功: {head_response}")
        except Exception as head_error:
            print(f"ファイル存在確認エラー: {head_error}")
            # head_objectのNot Foundは '404'（'NoSuchKey'ではない）
            if is_r2_not_found_error(head_error):
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
        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
        response = await r2_transfer.run_r2(r2_client.get_object, Bucket=settings.R2_BUCKET_NAME, Key=compressed_key)
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
                for chunk in response['Body'].iter_chunks(chunk_size=STREAM_CHUNK_SIZE):
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
            finally:
                # 例外送出経路でもfinallyは実行されるため、Bodyは必ず閉じられる。
                # Starlette 1.6.0は同期イテレータのclose()を呼ばないため明示的に閉じる。
                try:
                    response['Body'].close()
                except Exception:
                    pass
        
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
        # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
        response = await r2_transfer.run_r2(r2_client.head_object, Bucket=settings.R2_BUCKET_NAME, Key=compressed_key)

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
        # head_objectのNot Foundは '404'（'NoSuchKey'ではない）。
        # ここを取り違えると圧縮中でも500になり、フロントのダウンロード導線が壊れる。
        if is_r2_not_found_error(e):
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
            # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
            head_response = await r2_transfer.run_r2(r2_client.head_object, Bucket=settings.R2_BUCKET_NAME, Key=compressed_key)
            print(f"ファイル存在確認成功: {head_response}")
        except Exception as head_error:
            print(f"ファイル存在確認エラー: {head_error}")
            # head_objectのNot Foundは '404'（'NoSuchKey'ではない）
            if is_r2_not_found_error(head_error):
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
            # R2呼び出しは専用エグゼキュータで実行する（既定エグゼキュータの枠を消費しない）
            await r2_transfer.run_r2(
                r2_client.delete_object,
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
            # 共有URLを生成（生成元は_build_share_urlに一本化。理由は同関数のdocstring参照）
            share_url = _build_share_url(video['share_token'])
            
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