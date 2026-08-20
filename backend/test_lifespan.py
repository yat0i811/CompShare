"""main.lifespanの停止経路でr2_transfer.shutdownが呼ばれることの検証。

【他のテストファイルから分離する理由】
このファイルだけがmainをimportする。main.py importは以下の副作用を伴うため
（core.config.settings評価・R2クライアント生成・core.r2_transfer.init()呼び出し等）、
他のテストファイル（test_r2_transfer.py等）と混ぜず本ファイルに閉じ込める。
conftest.pyが環境変数にダミー値を注入済み（R2_ENDPOINT_URL=https://r2.invalid）のため、
main import時にboto3.client()が呼ばれてもネットワークには一切触れない
（クライアントの生成自体はネットワークI/Oを伴わない。転送を実際に走らせない限り安全）。
"""
import main


class _FakeScheduler:
    """main.schedulerの差し替え用。add_job/start/shutdownの呼び出しを記録する。"""

    def __init__(self, start_should_raise: bool = False) -> None:
        self.add_job_calls = []
        self.start_called = False
        self.shutdown_called = False
        self._start_should_raise = start_should_raise

    def add_job(self, *args, **kwargs) -> None:
        self.add_job_calls.append((args, kwargs))

    def start(self) -> None:
        self.start_called = True
        if self._start_should_raise:
            raise RuntimeError("scheduler.start()が失敗した(テスト用)")

    def shutdown(self) -> None:
        self.shutdown_called = True


async def _fake_db_lifespan(app):
    """main.db_lifespanの差し替え用。DB初期化を行わない素通りのasync generator。"""
    yield


async def _fake_cleanup_expired_videos() -> None:
    """main.cleanup_expired_videosの差し替え用。何もしない。"""
    return None


def _make_recording_shutdown():
    """main.r2_transfer.shutdownの差し替え用。呼び出し回数・引数を記録するだけの非同期関数。"""
    calls = []

    async def fake_shutdown(*args, **kwargs):
        calls.append((args, kwargs))

    fake_shutdown.calls = calls
    return fake_shutdown


async def test_lifespan_shuts_down_r2_executor(monkeypatch):
    """lifespanを正常に抜けたとき、r2_transfer.shutdownがちょうど1回呼ばれること。"""
    fake_scheduler = _FakeScheduler()
    monkeypatch.setattr(main, "scheduler", fake_scheduler)
    monkeypatch.setattr(main, "db_lifespan", _fake_db_lifespan)
    monkeypatch.setattr(main, "cleanup_expired_videos", _fake_cleanup_expired_videos)
    fake_shutdown = _make_recording_shutdown()
    monkeypatch.setattr(main.r2_transfer, "shutdown", fake_shutdown)

    async with main.lifespan(None):
        pass

    assert len(fake_shutdown.calls) == 1
    assert fake_scheduler.start_called
    assert fake_scheduler.shutdown_called


async def test_lifespan_shuts_down_r2_even_if_scheduler_failed(monkeypatch):
    """scheduler.start()が例外を送出しても、r2_transfer.shutdownは必ず呼ばれること。

    main.lifespanはscheduler起動失敗を握りつぶして続行する設計（黙って停止していた過去の
    バグの再発防止のため例外は握るが、コンテナ自体は起動させ続ける）。scheduler_started=False
    のままなのでscheduler.shutdown()は呼ばれないが、r2_transfer.shutdown()は
    scheduler_startedの真偽に関わらず必ず実行される。
    """
    fake_scheduler = _FakeScheduler(start_should_raise=True)
    monkeypatch.setattr(main, "scheduler", fake_scheduler)
    monkeypatch.setattr(main, "db_lifespan", _fake_db_lifespan)
    monkeypatch.setattr(main, "cleanup_expired_videos", _fake_cleanup_expired_videos)
    fake_shutdown = _make_recording_shutdown()
    monkeypatch.setattr(main.r2_transfer, "shutdown", fake_shutdown)

    async with main.lifespan(None):
        pass

    assert len(fake_shutdown.calls) == 1
    assert fake_scheduler.start_called
    assert not fake_scheduler.shutdown_called
