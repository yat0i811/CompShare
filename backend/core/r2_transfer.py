"""R2（Cloudflare R2 = S3互換ストレージ）専用の転送実行基盤。

【なぜ専用エグゼキュータなのか】
CLOSE_ISSUES §4-1で記録した「大容量動画のダウンロード中にasync関数内で同期の
r2_client呼び出しを直接行い、イベントループ全体が約74秒停止した」事故の再発防止のため、
R2への同期I/O（download_file/upload_file/head_object/get_object/delete_object等）は
FastAPI/uvicornの既定エグゼキュータとは別枠の専用スレッドプールで実行する。
bcrypt（db/crud.py, routers/auth_router.py）やffprobe/ffmpeg・python-magic
（routers/video_router.py）は既定エグゼキュータのまま維持し、R2転送が
それらの枠を奪わない（逆にR2側もそれらに枠を奪われない）ようにする。

【スレッド総数の計算式（R2関連のみ。既定エグゼキュータとは別枠）】
  R2専用executor           : R2_EXECUTOR_MAX_WORKERS         = 4
  TransferManager request  : R2_TRANSFER_MAX_CONCURRENCY     = 4  （全転送で共有。転送ごとに4ではない）
  TransferManager submission: _SUBMISSION_CONCURRENCY        = 2  （同時転送はMAX_CONCURRENT_COMPRESSIONS=2が上限）
  TransferManager io        : 固定                            = 1
  ------------------------------------------------------------------
  合計上限                                                    = 11 スレッド

  ※ マネージド転送1本につきR2 executorのワーカーを1本、完了待ち（TransferFuture.result()）で
     占有する。同時2転送で2本占有 → head_object/delete_object/get_object用に2本残る設計。
  ※ メモリ上限（ダウンロード時のディスク書き込み待ちバッファ）: io_chunksize 256KB ×
     max_io_queue_size 100 = 約25MB。TransferConfigで明示している（後述）。
  ※【この4本の限界】run_r2にはマネージド転送だけでなく、バケット全走査・一括削除といった
     長時間ジョブ（admin_routerの/cleanup/scan・/cleanup/execute・/r2/usage、main.pyの
     毎時/毎日cron）も載る。これらは処理が終わるまでワーカーを1本占有し続けるため、
     「転送2本 + バッチ2本」で4本が埋まると、共有プレビュー等の公開エンドポイントが行う
     head_object/get_objectがキュー待ちになる（＝一般ユーザーの応答が管理バッチに
     引きずられる）。実運用でこれが頻発するならR2_EXECUTOR_MAX_WORKERSを引き上げること
     （その際はR2_MAX_POOL_CONNECTIONSも連動して見直す。上の接続プールの計算式参照）。

【botocoreの接続プール（R2_MAX_POOL_CONNECTIONS）】
  4（転送request）
+ 2（転送submission: CreateMultipartUpload / HeadObjectなど）
+ 4（R2 executor上のhead/get/deleteが最大同時4）
+ 6（StreamingResponseが保持中のget_object Body用の余裕）
= 16
  ※ 超過してもエラーにはならず"Connection pool is full, discarding connection"警告が出て
     コネクションが使い捨てになるだけ。性能のためのチューニング値であり上限ではない。

【TransferManager.shutdown(cancel=True)を絶対に使ってはいけない理由】
s3transfer 0.19.2のTransferManager.shutdownは
    def shutdown(self, cancel=False, cancel_msg=''):
        self._shutdown(cancel, cancel, cancel_msg)
と実装されており、_shutdown(self, cancel, cancel_msg, exc_type=CancelledError)の
第2引数（本来cancel_msg文字列を渡す位置）にboolのcancelを、第3引数（本来例外クラスを
渡す位置exc_type）に実際のcancel_msg文字列を渡してしまっている（boto/s3transfer#143、
0.19.2で再現確認済み）。cancel=Trueかつ進行中の転送がある状態で呼ぶと、内部で
文字列であるcancel_msgをexc_typeとして呼び出そうとし
"TypeError: 'str' object is not callable"がexecutorのjoin前に送出され、
shutdownが完了しない。進行中転送が無ければ`if cancel:`の中を通らないため表面化せず、
気づきにくい。
このモジュールでは代わりに「追跡集合内のTransferFutureを個別にcancel()してから、
引数なしのshutdown()（既定cancel=False。上記の分岐に入らないため安全）を呼ぶ」という
協調的キャンセルで代替する。

【キャンセルされた転送から飛んでくる例外の型（重要。except節の書き方に直結する）】
`s3transfer.exceptions.CancelledError`は`concurrent.futures.CancelledError`そのもの
（別名の再エクスポート）であり、Python 3.10では`Exception`のサブクラス＝
`asyncio.CancelledError`（`BaseException`のサブクラス）とは**別物**である。
ところが`loop.run_in_executor`のFuture経由でこの例外が伝播するとき、asyncioの
`_convert_future_exc`が`type(exc) is concurrent.futures.CancelledError`という
**厳密な型一致**でこれを`asyncio.CancelledError`へ**変換**する。
その結果、shutdown時に`future.cancel()`された転送を待っている
`await run_r2(future.result)`は`asyncio.CancelledError`を送出する。呼び出し元
`run_ffmpeg_job_r2`の`except Exception`はBaseException派生のこれを捕まえないため、
エラー通知を出さずにfinally（一時ファイル削除・セマフォ解放）だけが走る
——これは停止時の意図どおりの挙動である。
  ※ `except concurrent.futures.CancelledError`と書いてもマッチしない。変換後は
     asyncio側の型になっているため。捕まえたいなら`asyncio.CancelledError`を書く。
  ※ 変換は厳密型一致なので、サブクラス（`s3transfer.exceptions.FatalError`等）は
     変換されずそのまま届き、`except Exception`で捕まる（＝通常のエラー扱い）。

【TransferManager.upload()/download()の引数順】
    upload(fileobj, bucket, key, ...)    # client.upload_file(Filename, Bucket, Key)と同順
    download(bucket, key, fileobj, ...)  # client.download_file(Bucket, Key, Filename)と同順
取り違えるとバケット名をキーとして、キーをファイル名として扱うなど気づきにくいバグに
なるため、test_r2_transfer.py::test_upload_file_argument_orderで固定する。
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, Set

from boto3.s3.transfer import ProgressCallbackInvoker
from s3transfer.futures import TransferFuture
from s3transfer.manager import TransferConfig, TransferManager

from core.config import settings

# --- モジュール状態（すべてプライベート。initで生成しshutdownでNoneに戻す） ---
_executor: Optional[ThreadPoolExecutor] = None
_manager: Optional[TransferManager] = None
_inflight: Set[TransferFuture] = set()
_inflight_lock = threading.Lock()

_SUBMISSION_CONCURRENCY = 2  # 同時転送数の上限（settings.MAX_CONCURRENT_COMPRESSIONS）に合わせる
_SHUTDOWN_TIMEOUT_SEC = 3.0  # uvicornのgraceful 5sとdockerの10sから逆算（§3参照）
_SHUTDOWN_POLL_INTERVAL_SEC = 0.05  # manager停止の完了待ちをポーリングする間隔（shutdown参照）


def init(client, *, executor=None, transfer_manager=None) -> None:
    """main.pyから1回だけ呼ぶ。executor / transfer_managerはテスト用の差し替え口。

    再呼び出し時は旧executorをshutdown(wait=False, cancel_futures=True)してから
    作り直す（テスト間のリーク防止）。旧managerには触らない。
    ThreadPoolExecutorはワーカーを遅延生成するので、init時点ではスレッドは増えない。
    """
    global _executor, _manager

    old_executor = _executor

    _executor = (
        executor
        if executor is not None
        else ThreadPoolExecutor(
            max_workers=settings.R2_EXECUTOR_MAX_WORKERS, thread_name_prefix="r2-exec"
        )
    )

    if transfer_manager is not None:
        _manager = transfer_manager
    else:
        config = TransferConfig(
            # boto3のTransferConfig(max_concurrency=N)に相当。全転送で共有される。
            max_request_concurrency=settings.R2_TRANSFER_MAX_CONCURRENCY,
            max_submission_concurrency=_SUBMISSION_CONCURRENCY,
            # boto3のTransferConfig既定値（100）に明示的に合わせる。io_chunksize 256KB × 100
            # = 約25MBが「ダウンロード済みだがまだディスクに書けていない」バッファの上限になる。
            # s3transfer.manager.TransferConfigを直接使うと素の既定は1000（＝約250MB）で、
            # ディスクがネットワークより遅い局面（HDD・他プロセスと競合中など）にメモリを
            # 食い潰す。boto3経由をやめてTransferManagerを自前で持った副作用なので明示する。
            max_io_queue_size=100,
            # s3transferはbotocoreのリトライとは**別に**ダウンロードを再試行する（素の既定5。
            # boto3経由でも5）。ReadTimeoutError等でget_objectを再発行する経路であり、
            # TransferFuture.cancel()後もキャンセルをチェックせず再発行するため、停止時の
            # 最悪占有を抑える目的で2に絞る。通常の一時障害は2回で十分（ジョブ全体は
            # ユーザーが再実行でき、クリーンアップはcronが再試行する）。
            num_download_attempts=2,
        )
        _manager = TransferManager(client, config)

    if old_executor is not None and old_executor is not _executor:
        old_executor.shutdown(wait=False, cancel_futures=True)


async def run_r2(fn: Callable, /, *args: Any, **kwargs: Any) -> Any:
    """R2の同期呼び出しを専用エグゼキュータで実行する。

    loop.run_in_executorはキーワード引数を取れないため、functools.partialに包む。

    【注意】ここに渡した非マネージド呼び出し（head/get/delete等）はキャンセルできない。
    R2無応答時はbotocoreのread_timeout（main.pyで30秒）×合計試行回数までワーカーが
    解放されない。main.pyのretriesは`max_attempts=1`＝**合計2試行**（max_attemptsは
    "追加リトライ回数"でbotocoreがN+1と解釈する）なので、最悪占有のオーダーは
      非マネージド呼び出し（この関数に直接渡すhead/get/delete）: 2試行 × 30秒 = 約60秒
      マネージド転送のレンジタスク: num_download_attempts 2 × 合計試行 2 × 30秒 = 約120秒
    となる（後者はs3transferがbotocoreとは別に持つ再試行の分。initのTransferConfig参照）。
    通常は数十ms程度で完了するため実害は無い、という前提で許容している。
    """
    # _executorをローカルに束縛してからNone判定・使用する。将来この関数にawaitが挟まると、
    # 判定後・使用前のshutdownで_executorがNoneに変わり、run_in_executor(None, ...)＝
    # 既定エグゼキュータへのフォールバックとなって専用エグゼキュータ分離が静かに壊れるため。
    executor = _executor
    if executor is None:
        raise RuntimeError("r2_transfer.init() が呼ばれていません")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, functools.partial(fn, *args, **kwargs))


async def _run_transfer(future: TransferFuture) -> None:
    """マネージド転送（download/upload）のTransferFutureを追跡しつつ完了を待つ共通処理。"""
    with _inflight_lock:
        _inflight.add(future)
    try:
        # ワーカー1本を完了まで占有する。future.result()以外はブロックしない
        # （_manager.download()/upload()自体はキューに積むだけで即座に返るため）。
        await run_r2(future.result)
    except asyncio.CancelledError:
        future.cancel()
        raise
    finally:
        with _inflight_lock:
            _inflight.discard(future)


async def download_file(bucket: str, key: str, filename: str, callback=None) -> None:
    """client.download_file(Bucket, Key, Filename, Callback=)と同じ引数順。"""
    if _manager is None:
        raise RuntimeError("r2_transfer.init() が呼ばれていません")
    subscribers = [ProgressCallbackInvoker(callback)] if callback else None
    future = _manager.download(bucket, key, filename, subscribers=subscribers)
    await _run_transfer(future)


async def upload_file(filename: str, bucket: str, key: str, callback=None) -> None:
    """client.upload_file(Filename, Bucket, Key, Callback=)と同じ引数順。"""
    if _manager is None:
        raise RuntimeError("r2_transfer.init() が呼ばれていません")
    subscribers = [ProgressCallbackInvoker(callback)] if callback else None
    future = _manager.upload(filename, bucket, key, subscribers=subscribers)
    await _run_transfer(future)


async def _await_manager_shutdown(manager: TransferManager, timeout: float) -> None:
    """TransferManager.shutdown()をdaemonスレッドで走らせ、最大timeout秒だけ完了を待つ。

    【なぜasyncio.to_thread / wait_forを使わないのか】
    asyncio.to_thread()は**既定エグゼキュータ**（非daemonワーカー）を使う。そこへ
    manager.shutdown()を載せると2つの問題が同時に起きる:
      1) 時間を制限できない。asyncio.wait_forがタイムアウトしても、走っているスレッドは
         止まらずワーカーに残る。uvicornはループ終了時にloop.shutdown_default_executor()を
         呼ぶが、Python 3.10のこれにはtimeout引数が無く**完了まで無条件に待つ**。さらに
         concurrent.futuresのatexitフックが非daemonワーカーをjoinするため、
         プロセス終了そのものがブロックされる（実測: 停止に19秒かかった）。
      2) 「R2の処理を既定エグゼキュータに載せない」という本モジュールの前提を破る。
    そのため自前のdaemonスレッドで実行し、完了はthreading.Eventのポーリングで待つ。
    daemonスレッドはインタプリタ終了時にjoinされないため、タイムアウトして放置しても
    プロセス終了を妨げない（＝時間予算を本当に守れる）。
    """
    done = threading.Event()

    def _shutdown_manager() -> None:
        try:
            # 引数を渡さない（既知バグ回避。理由はモジュールdocstring参照）
            manager.shutdown()
        except Exception as e:
            print(f"[WARNING] r2_transfer.shutdown: TransferManager.shutdown()で例外が発生しました: {e}")
        finally:
            done.set()

    try:
        threading.Thread(target=_shutdown_manager, name="r2-mgr-shutdown", daemon=True).start()
    except RuntimeError as e:
        # スレッド数の上限やインタプリタ終了中でstart()が失敗することがある。start()が
        # 失敗しても停止処理全体は続行させる（finallyのexecutor停止と状態クリアが本丸のため）。
        print(f"[WARNING] r2_transfer.shutdown: TransferManager.shutdown()用のスレッドを"
              f"起動できませんでした。manager停止をスキップして続行します: {e}")
        return

    # done.wait()はスレッドをブロックしてしまうため、イベントループ側からポーリングする。
    # ここでrun_in_executor/to_threadを使うと上記の問題に逆戻りするので使わないこと。
    deadline = time.monotonic() + timeout
    while not done.is_set():
        if time.monotonic() >= deadline:
            print(f"[WARNING] r2_transfer.shutdown: TransferManager.shutdown()が{timeout}秒で"
                  "タイムアウトしました。daemonスレッドとして放置し停止処理を続行します")
            return
        await asyncio.sleep(_SHUTDOWN_POLL_INTERVAL_SEC)


async def shutdown(timeout: float = _SHUTDOWN_TIMEOUT_SEC) -> None:
    """未初期化・二重呼び出しでも例外を出さない（早期return）。

    1) 追跡中のTransferFutureを全てcancel()（非ブロッキング）
    2) TransferManager.shutdown()をdaemonスレッドで実行し、最大timeout秒だけ完了を待つ
       （_await_manager_shutdown参照）。タイムアウト・例外はログのみで、ここで停止処理
       全体を止めない。
    3) finallyで必ず後始末する:
       _executor.shutdown(wait=False, cancel_futures=True)（非ブロッキング）→
       _inflightのクリア → モジュール状態をNoneに戻す
       ※ 2)を待っている最中にlifespanタスク自体がcancelされても後始末が走るように
          try/finallyにしている（さもないとexecutorとTransferManagerが宙に浮く）。
    """
    global _executor, _manager

    manager = _manager
    executor = _executor
    if manager is None and executor is None:
        return

    with _inflight_lock:
        futures = list(_inflight)
    for future in futures:
        try:
            future.cancel()
        except Exception as e:
            print(f"[WARNING] r2_transfer.shutdown: TransferFuture.cancel()に失敗しました: {e}")

    try:
        if manager is not None:
            await _await_manager_shutdown(manager, timeout)
    finally:
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        with _inflight_lock:
            _inflight.clear()
        # shutdownの待機中（最大timeout秒）にinit()が走って新しい状態が作られていた場合、
        # それを巻き添えでNoneにすると新executorが孤児化し、以後run_r2が全て
        # RuntimeErrorになる。自分がキャプチャした世代のときだけクリアする。
        if _executor is executor:
            _executor = None
        if _manager is manager:
            _manager = None


def get_executor() -> Optional[ThreadPoolExecutor]:
    """テスト用。"""
    return _executor
