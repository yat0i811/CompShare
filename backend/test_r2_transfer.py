#!/usr/bin/env python3
"""core/r2_transfer.py（R2転送専用の実行基盤）のテスト。

偽boto3 client・偽TransferFuture・偽TransferManagerを使い、実際のR2やs3transferの
内部実装（タスクキュー・executor構成等）には依存せずに、以下を検証する:
  - run_r2()がR2専用エグゼキュータ（既定エグゼキュータとは別枠）で実行されること
  - キーワード引数がfunctools.partial経由で正しく転送されること（partialの引数抜け検知）
  - download_file/upload_fileがイベントループをブロックしないこと
  - upload()/download()の引数順が取り違えられていないこと（設計書§48-52参照）
  - タスクキャンセル・shutdown()がTransferFuture.cancel()を確実に呼ぶこと
  - shutdown()がTransferManager.shutdown()を引数なしで呼ぶこと
    （boto/s3transfer#143の引数取り違えバグを踏まないことの回帰）
  - shutdown()が未初期化・二重呼び出しでも安全なこと
  - shutdown()がTransferManager.shutdown()のハングに引きずられずtimeout秒で戻ること
    （daemonスレッド + ポーリング待ちの回帰。core/r2_transfer.py参照）
  - init/shutdownでモジュール状態（get_executor()）が正しく生成・破棄されること
  - 未初期化状態でrun_r2/download_file/upload_fileがRuntimeErrorを送出すること

実物のTransferManagerに偽clientを渡す統合テストは採用していない。内部実装
（タスクキュー・submission/request executorの構成）に結合し、s3transferのバージョン
更新で壊れやすいため。API サーフェスはtest_real_transfer_manager_api_surfaceで、
キャンセル配線はtest_task_cancel_cancels_transfer_future /
test_shutdown_cancels_inflight_transfersで個別に押さえ、s3transfer内部の協調キャンセル
自体はライブラリの責務としてデプロイ前の手動SIGTERM検証（docs/CLOSE_ISSUES.md §5-8参照）で確認する。
"""
import asyncio
import inspect
import threading
import time
from concurrent.futures import CancelledError as FuturesCancelledError
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from core import r2_transfer
from routers.video_router import TransferProgress


# --- 偽boto3 S3クライアント ---
class FakeClient:
    """呼び出し引数を記録するだけの偽boto3 S3クライアント。"""

    def __init__(self) -> None:
        self.calls = []

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        return {"ContentLength": 123}


# --- 偽TransferFuture ---
class FakeTransferFuture:
    """s3transfer.futures.TransferFutureの偽物。

    result()はthreading.Event.wait(timeout=block_sec)で疑似ブロックする。
    ブロック中にcancel()されるとEventがsetされ、result()は
    s3transfer.exceptions.CancelledError（実体はconcurrent.futures.CancelledError。
    core/r2_transfer.pyのモジュールdocstring参照）を送出する。
    block_sec経過まで誰もcancel()しなければ、waitがタイムアウトしてNoneを返す
    （＝転送が正常完了した扱い）。
    """

    def __init__(self, block_sec: float = 0.0) -> None:
        self._block_sec = block_sec
        self._event = threading.Event()
        self.cancel_called = False

    def result(self):
        cancelled = self._event.wait(timeout=self._block_sec)
        if cancelled:
            raise FuturesCancelledError()
        return None

    def cancel(self) -> None:
        self.cancel_called = True
        self._event.set()

    def done(self) -> bool:
        return self._event.is_set()


# --- 偽TransferManager ---
class FakeTransferManager:
    """s3transfer.manager.TransferManagerの偽物。

    download()/upload()が受けた引数をそのまま記録して偽TransferFutureを返す。
    subscribersがあれば、実物のboto3.s3.transfer.ProgressCallbackInvokerと同じ
    シグネチャ on_progress(bytes_transferred, **kwargs) でprogress_chunksの内容を
    順に通知する。
    """

    def __init__(self, block_sec: float = 0.0, progress_chunks=None) -> None:
        self.block_sec = block_sec
        self.progress_chunks = progress_chunks if progress_chunks is not None else [40, 30, 30]
        self.download_calls = []
        self.upload_calls = []
        self.shutdown_calls = []
        self.futures = []

    def download(self, bucket, key, fileobj, extra_args=None, subscribers=None):
        self.download_calls.append((bucket, key, fileobj, extra_args, subscribers))
        return self._start_transfer(subscribers)

    def upload(self, fileobj, bucket, key, extra_args=None, subscribers=None):
        self.upload_calls.append((fileobj, bucket, key, extra_args, subscribers))
        return self._start_transfer(subscribers)

    def _start_transfer(self, subscribers):
        future = FakeTransferFuture(block_sec=self.block_sec)
        self.futures.append(future)
        if subscribers:
            for chunk in self.progress_chunks:
                for subscriber in subscribers:
                    subscriber.on_progress(future=future, bytes_transferred=chunk)
        return future

    def shutdown(self, *args, **kwargs) -> None:
        # 引数を受けた形のまま記録する。boto/s3transfer#143の回避（core/r2_transfer.py
        # 参照）が崩れていないことをtest_shutdown_never_passes_cancel_trueで確認するため、
        # ここでは何も検証せず記録に徹する。
        self.shutdown_calls.append((args, kwargs))


class HangingShutdownTransferManager(FakeTransferManager):
    """shutdown()が長時間ブロックする偽TransferManager。

    実物のTransferManager.shutdown()は内部executorのjoinを含むため、R2が無応答なら
    そこで何十秒も止まり得る。その状況を再現し、r2_transfer.shutdown()が
    「巻き込まれずにtimeout秒で戻る」ことを検証するために使う。
    テスト終了時にrelease.set()して、待たせているdaemonスレッドを必ず解放すること。
    """

    def __init__(self, block_sec: float = 5.0) -> None:
        super().__init__()
        self.entered = threading.Event()   # shutdown()に実際に入ったことの確認用
        self.release = threading.Event()   # テスト側から解放するためのスイッチ
        self._block_sec = block_sec

    def shutdown(self, *args, **kwargs) -> None:
        self.shutdown_calls.append((args, kwargs))
        self.entered.set()
        self.release.wait(timeout=self._block_sec)


# --- r2_stubフィクスチャ ---
class R2Stub:
    """r2_stubフィクスチャがテストへ渡すハンドル。"""

    def __init__(self, client: FakeClient, manager: FakeTransferManager) -> None:
        self.client = client
        self.manager = manager

    def reinit(self, *, block_sec: float = 0.0, progress_chunks=None) -> FakeTransferManager:
        """block_sec/progress_chunksを変えたFakeTransferManagerで作り直す。

        r2_transfer.init()の再呼び出しは旧executorをshutdown(wait=False)する実装に
        なっているため（core/r2_transfer.py参照）、テスト内で何度呼んでも安全。
        """
        self.manager = FakeTransferManager(block_sec=block_sec, progress_chunks=progress_chunks)
        r2_transfer.init(self.client, executor=None, transfer_manager=self.manager)
        return self.manager


@pytest.fixture
async def r2_stub():
    """core.r2_transferを偽client/偽TransferManagerで初期化するフィクスチャ。

    executorはNoneのまま渡す＝r2_transfer.init()が実ThreadPoolExecutorを作る
    （run_r2()が本当にスレッドを切り替えることを検証したいため。executorまで偽物にすると
    test_run_r2_runs_on_dedicated_executor_threadが意味をなさなくなる）。
    後処理では必ずr2_transfer.shutdown()を呼び、R2 executorのスレッドを残さない
    （残すとテストプロセスの終了がブロックされ得るため）。
    """
    client = FakeClient()
    manager = FakeTransferManager()
    r2_transfer.init(client, executor=None, transfer_manager=manager)
    stub = R2Stub(client, manager)
    try:
        yield stub
    finally:
        await r2_transfer.shutdown()


async def _measure_max_loop_delay(stop: asyncio.Event, interval: float = 0.05) -> float:
    """イベントループの応答性を計測するヘルパー。

    interval秒ごとにasyncio.sleep(interval)し、実測時間がintervalをどれだけ超過したかの
    最大値を返す。ループのどこかがブロックされていれば、次にsleepから復帰したときの
    超過分として現れる。stopがsetされるまで回り続ける。
    """
    max_delay = 0.0
    while not stop.is_set():
        started = time.monotonic()
        await asyncio.sleep(interval)
        elapsed = time.monotonic() - started
        delay = elapsed - interval
        if delay > max_delay:
            max_delay = delay
    return max_delay


# --- 1. run_r2()が専用エグゼキュータのスレッドで実行されること ---
async def test_run_r2_runs_on_dedicated_executor_thread(r2_stub):
    name = await r2_transfer.run_r2(lambda: threading.current_thread().name)
    assert name.startswith("r2-exec")

    # 対比: asyncio.to_thread()（既定エグゼキュータ）はr2-execプレフィックスを持たない
    default_name = await asyncio.to_thread(lambda: threading.current_thread().name)
    assert not default_name.startswith("r2-exec")


# --- 2. run_r2()がキーワード引数を正しくfunctools.partial経由で転送すること ---
async def test_run_r2_forwards_keyword_arguments(r2_stub):
    await r2_transfer.run_r2(r2_stub.client.head_object, Bucket="test-bucket", Key="test-key")
    assert r2_stub.client.calls == [("head_object", {"Bucket": "test-bucket", "Key": "test-key"})]


# --- 3. download_file()がイベントループをブロックしないこと ---
async def test_download_file_does_not_block_event_loop(r2_stub):
    manager = r2_stub.reinit(block_sec=1.5, progress_chunks=[100_000, 200_000, 300_000])
    prog = TransferProgress()
    stop = asyncio.Event()

    async def _run_transfer():
        await r2_transfer.download_file("bucket", "key", "filename.mp4", callback=prog)
        stop.set()

    max_delay, _ = await asyncio.gather(_measure_max_loop_delay(stop), _run_transfer())

    assert max_delay < 0.5, f"イベントループが{max_delay}秒応答不能になった"
    assert prog.seen == sum(manager.progress_chunks)


# --- 4. upload_file()がイベントループをブロックしないこと ---
async def test_upload_file_does_not_block_event_loop(r2_stub):
    manager = r2_stub.reinit(block_sec=1.5, progress_chunks=[100_000, 200_000, 300_000])
    prog = TransferProgress()
    stop = asyncio.Event()

    async def _run_transfer():
        await r2_transfer.upload_file("filename.mp4", "bucket", "key", callback=prog)
        stop.set()

    max_delay, _ = await asyncio.gather(_measure_max_loop_delay(stop), _run_transfer())

    assert max_delay < 0.5, f"イベントループが{max_delay}秒応答不能になった"
    assert prog.seen == sum(manager.progress_chunks)


# --- 5. upload()/download()の引数順の回帰テスト ---
async def test_upload_file_argument_order(r2_stub):
    await r2_transfer.upload_file("local.mp4", "my-bucket", "my-key")
    await r2_transfer.download_file("my-bucket", "my-key", "local2.mp4")

    fileobj, bucket, key, _extra_args, subscribers = r2_stub.manager.upload_calls[0]
    assert (fileobj, bucket, key) == ("local.mp4", "my-bucket", "my-key")

    bucket2, key2, fileobj2, _extra_args2, subscribers2 = r2_stub.manager.download_calls[0]
    assert (bucket2, key2, fileobj2) == ("my-bucket", "my-key", "local2.mp4")

    # callback未指定のときはsubscribersにNoneを渡す（空リスト[]ではない）。
    # s3transferは`if subscribers:`ではなくNone判定で既定のsubscriber構成に分岐するため、
    # ここが[]に変わると挙動が変わり得る。
    assert subscribers is None
    assert subscribers2 is None


# --- 6. タスクのcancel()がTransferFuture.cancel()を呼ぶこと ---
async def test_task_cancel_cancels_transfer_future(r2_stub):
    manager = r2_stub.reinit(block_sec=5.0)
    task = asyncio.create_task(r2_transfer.download_file("bucket", "key", "file.mp4"))
    # 転送が開始し、futureがrun_r2(future.result)のawait地点に到達するのを待つ
    await asyncio.sleep(0.1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)

    assert manager.futures[0].cancel_called


# --- 7. shutdown()が進行中のTransferFutureを全てcancelすること ---
async def test_shutdown_cancels_inflight_transfers(r2_stub):
    manager = r2_stub.reinit(block_sec=10.0)
    task = asyncio.create_task(r2_transfer.upload_file("file.mp4", "bucket", "key"))
    await asyncio.sleep(0.1)

    started = time.monotonic()
    await asyncio.wait_for(r2_transfer.shutdown(), timeout=3.0)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"shutdown()が{elapsed}秒かかった"
    assert manager.futures[0].cancel_called

    with pytest.raises(RuntimeError):
        await r2_transfer.run_r2(lambda: None)

    # shutdown()はTransferFuture.cancel()を呼ぶだけでタスク自体の完了は待たないため、
    # ここで明示的に回収する（"Task exception was never retrieved"を防ぐ）。
    try:
        await task
    except BaseException:
        pass


# --- 8. shutdown()がTransferManager.shutdown()を引数なしで呼ぶこと(既知バグ回避の回帰) ---
async def test_shutdown_never_passes_cancel_true(r2_stub):
    """boto/s3transfer#143の回帰テスト。

    shutdown(cancel=True)を呼ぶとTransferManager内部で引数の取り違えにより
    TypeErrorが送出される（詳細はcore/r2_transfer.pyのモジュールdocstring参照）。
    このモジュールは代わりに個別cancel() + 引数なしshutdown()で代替しているため、
    TransferManager.shutdown()が受け取るargs/kwargsは常に空でなければならない。
    """
    await r2_transfer.shutdown()
    assert r2_stub.manager.shutdown_calls == [((), {})]


# --- 9. shutdown()の未初期化・二重呼び出し安全性 ---
async def test_shutdown_is_idempotent_and_safe_without_init(monkeypatch):
    """r2_stubフィクスチャを使わない。

    r2_transfer.init()を呼んでいない状態、および既にshutdown済みの状態で
    shutdown()を呼んでも例外を出さないことを確認する
    （main.pyのlifespan finallyのtry/exceptに頼らず、単体でも安全であること）。

    モジュールグローバルをmonkeypatchで明示的にNoneへ落としてから実行する。
    そうしないと「直前のテストがshutdown済みだったから通っただけ」になり、
    テスト名が主張する「未初期化」を実行順序に依存せず保証できないため。
    """
    monkeypatch.setattr(r2_transfer, "_executor", None)
    monkeypatch.setattr(r2_transfer, "_manager", None)

    await r2_transfer.shutdown()
    await r2_transfer.shutdown()


# --- 10. 実物のTransferManager/TransferConfigのAPIサーフェス確認 ---
def test_real_transfer_manager_api_surface():
    """core/r2_transfer.pyが前提とするs3transferの実APIが崩れていないことを確認する。

    本ファイルの他のテストは全て偽TransferManagerを使うため、実物のコンストラクタ引数・
    _config属性・引数なしshutdown()が例外を出さずに返ることを、このテストだけで押さえる。
    転送は一切行わない（clientはMagicMockでスタブ）。
    """
    from boto3.s3.transfer import ProgressCallbackInvoker
    from s3transfer.manager import TransferConfig, TransferManager

    client = MagicMock()
    config = TransferConfig(
        max_request_concurrency=4, max_submission_concurrency=2, max_io_queue_size=100
    )
    manager = TransferManager(client, config)
    try:
        # プライベート属性の参照だが、壊れたらAPIドリフトとして気づくのが目的。
        assert manager._config.max_request_concurrency == 4
        assert manager._config.max_io_queue_size == 100
    finally:
        manager.shutdown()

    # 引数順・コールバック名を実物のシグネチャで固定する。
    # FakeTransferManager / FakeTransferFutureは自作なので、実物のs3transfer側で引数順が
    # ドリフトしても偽物ベースのテスト（test_upload_file_argument_order等）は緑のまま通る。
    # 実害（バケット名をキーとして扱う等）は本番でしか出ないため、ここで実物を検査する。
    assert list(inspect.signature(TransferManager.download).parameters)[:4] == [
        "self", "bucket", "key", "fileobj",
    ]
    assert list(inspect.signature(TransferManager.upload).parameters)[:4] == [
        "self", "fileobj", "bucket", "key",
    ]
    # FakeTransferManager._start_transferがsubscriber.on_progress(bytes_transferred=...)を
    # キーワードで呼んでいる前提を、実物のProgressCallbackInvokerで固定する。
    assert "bytes_transferred" in inspect.signature(ProgressCallbackInvoker.on_progress).parameters


# --- 11. shutdown()がTransferManager.shutdown()のハングに巻き込まれないこと ---
async def test_shutdown_times_out_without_blocking(r2_stub):
    """TransferManager.shutdown()が固まっても、shutdown()はtimeout秒で戻ること。

    以前の実装はasyncio.wait_for(asyncio.to_thread(_manager.shutdown), timeout)で、
    wait_forがタイムアウトしても既定エグゼキュータの非daemonワーカーが残り、
    プロセス終了までブロックされていた（実測19秒。詳細はcore/r2_transfer.py参照）。
    現在はdaemonスレッド + threading.Eventのポーリング待ちにしてあるため、
    timeout秒で見切って後始末（executor停止・モジュール状態のクリア）まで完了する。
    """
    manager = HangingShutdownTransferManager(block_sec=5.0)
    r2_transfer.init(r2_stub.client, executor=None, transfer_manager=manager)
    r2_stub.manager = manager

    try:
        started = time.monotonic()
        # (a) 例外を外に出さず、timeout秒＋マージンで戻ること
        await r2_transfer.shutdown(timeout=0.2)
        elapsed = time.monotonic() - started

        assert manager.entered.wait(1.0), "TransferManager.shutdown()が呼ばれていない"
        assert elapsed >= 0.2, f"タイムアウトを待たずに{elapsed}秒で戻った"
        assert elapsed < 2.0, f"shutdown()がハングに巻き込まれて{elapsed}秒かかった"

        # (b) 以降のrun_r2はRuntimeError（＝executorが破棄済み）
        with pytest.raises(RuntimeError):
            await r2_transfer.run_r2(lambda: None)

        # (c) モジュール状態のクリアもfinallyで完了している
        assert r2_transfer.get_executor() is None
    finally:
        # 待たせているdaemonスレッドを解放する（放置してもプロセス終了は妨げないが、
        # テストプロセス中に5秒間スレッドを残す必要はない）。
        manager.release.set()


# --- 12. init/shutdownでモジュール状態が生成・破棄されること ---
async def test_init_and_shutdown_lifecycle(monkeypatch):
    """get_executor()がinit後に実executorを返し、shutdown後にNoneへ戻ること。

    r2_stubフィクスチャを使わず、実行順序に依存しないようモジュールグローバルを
    明示的にNoneへ落としてから始める。
    """
    monkeypatch.setattr(r2_transfer, "_executor", None)
    monkeypatch.setattr(r2_transfer, "_manager", None)
    assert r2_transfer.get_executor() is None

    r2_transfer.init(FakeClient(), executor=None, transfer_manager=FakeTransferManager())
    executor = r2_transfer.get_executor()
    assert isinstance(executor, ThreadPoolExecutor)

    # 実際にこのexecutorで動くこと（get_executor()が飾りでないことの確認）
    assert (await r2_transfer.run_r2(lambda: threading.current_thread().name)).startswith("r2-exec")

    await r2_transfer.shutdown()
    assert r2_transfer.get_executor() is None


# --- 13. 未初期化状態での各APIがRuntimeErrorを送出すること ---
@pytest.mark.parametrize(
    "call",
    [
        lambda: r2_transfer.run_r2(lambda: None),
        lambda: r2_transfer.download_file("bucket", "key", "file.mp4"),
        lambda: r2_transfer.upload_file("file.mp4", "bucket", "key"),
    ],
    ids=["run_r2", "download_file", "upload_file"],
)
async def test_public_apis_require_init(monkeypatch, call):
    """init()前（またはshutdown後）に呼ばれたら、黙って既定エグゼキュータへ落ちずに失敗すること。

    run_r2がNoneのexecutorをrun_in_executorへ渡すと、asyncioはそれを
    「既定エグゼキュータを使え」の意味に解釈し、専用エグゼキュータ分離が静かに壊れる。
    そうならずRuntimeErrorになることを3つの公開APIすべてで固定する。
    """
    monkeypatch.setattr(r2_transfer, "_executor", None)
    monkeypatch.setattr(r2_transfer, "_manager", None)

    with pytest.raises(RuntimeError):
        await call()
