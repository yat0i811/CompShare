#!/usr/bin/env python3
"""routers/video_router.py のFFmpegオプション組み立て(NVENC対応 + ffprobe一本化)のテスト。

このテストが守るもの:
  1. NVENC(GPU)エンコード時の画質指定が、旧来のビットレート固定(-b:v X -maxrate Y -bufsize Z /
     -rc cbr)ではなく、CRF相当のCQ方式(-rc vbr -cq <値> -b:v 0)になっていること
     (経緯はdocs/CLOSE_ISSUES.md §6-1参照)。
  2. CRF→CQの変換(crf_to_nvenc_cq、NVENC_CQ_OFFSET=4)が暫定値のまま壊れずに効いていること
     (§6-2参照。実測による見直しは別途必要)。
  3. NVENC失敗時のCPUフォールバックが、文字列置換ではなくエンコーダ別に構築した
     cpu_fallback_optionsをそのまま使うこと(§6-5参照。以前は-rc cbr等のNVENC私有
     オプションがlibx264にそのまま渡ってしまう事故があった)。
  4. 動画の解像度取得(ffprobe)がジョブ全体で1回だけであること。build_ffmpeg_options /
     build_encoder_optionsはsource_resolutionを受け取るだけでffprobeを再実行しない
     (§6-4参照)。

【方針】
実ffmpeg・実GPUには一切触れない。is_gpu_encoder_available()はmonkeypatchで注入し、
サブプロセス呼び出しが必要な箇所(run_ffmpeg_process内のasyncio.create_subprocess_exec、
get_video_duration、R2転送)もすべて偽物に差し替える。

【ローカルアップロード経路(upload_and_compress_local_endpoint)専用のテストは作らない】
R2経路(run_ffmpeg_job_r2)とローカル経路は「解像度をジョブ全体で1回だけ取得し、
build_ffmpeg_options / build_encoder_optionsに渡す」という同一パターンをそのままコピーして
いる(video_router.py参照)。純関数レベルでffprobeを再実行しないことを保証する
test_build_does_not_probe_inputと、R2経路で実際に1回であることを保証する
test_r2_job_probes_resolution_onceの組み合わせで、両経路とも担保される。
"""
import asyncio

import pytest
from fastapi import HTTPException

from routers import video_router
from routers.video_router import (
    NVENC_CQ_MAX,
    NVENC_CQ_MIN,
    NVENC_CQ_OFFSET,
    NVENC_ENCODER,
    X264_ENCODER,
    build_encoder_options,
    build_ffmpeg_options,
    crf_to_nvenc_cq,
    get_appropriate_level,
)


def _value_after(options: list, flag: str) -> str:
    """optionsの中からflagを探し、直後の値を返す。flagが無ければAssertionErrorにする。"""
    assert flag in options, f"{flag} が見つかりません: {options}"
    idx = options.index(flag)
    return options[idx + 1]


def _has_pair(options: list, flag: str, value: str) -> bool:
    """optionsの中に (flag, value) が隣接して存在するか。"""
    return any(options[i] == flag and options[i + 1] == value for i in range(len(options) - 1))


# --- 1. GPU: ビットレート固定ではなくvbr+cq、b:vは明示的に0 ---
def test_gpu_uses_vbr_cq_with_zero_bitrate():
    options = build_encoder_options(NVENC_ENCODER, 28, "source", None, None, (1920, 1080))
    assert _value_after(options, "-rc") == "vbr"
    assert "-cq" in options
    assert _value_after(options, "-b:v") == "0"


# --- 2. GPU: CRF→CQのマッピング(crf + NVENC_CQ_OFFSET) ---
@pytest.mark.parametrize(
    "crf, expected_cq",
    [
        pytest.param(18, 18 + NVENC_CQ_OFFSET, id="crf18"),
        pytest.param(28, 28 + NVENC_CQ_OFFSET, id="crf28"),
        pytest.param(32, 32 + NVENC_CQ_OFFSET, id="crf32"),
    ],
)
def test_gpu_cq_mapping(crf, expected_cq):
    # 期待値を crf + NVENC_CQ_OFFSET で組んでいるため、定数を変えてもこのテストは通ってしまう。
    # オフセット値自体を固定し、変更を「意識的な行為」にするためのガード。
    assert NVENC_CQ_OFFSET == 4  # 変更時は docs/CLOSE_ISSUES.md §6-2 の実測手順を踏み、この期待値も意識的に更新すること
    options = build_encoder_options(NVENC_ENCODER, crf, "source", None, None, (1920, 1080))
    assert _value_after(options, "-cq") == str(expected_cq)
    assert crf_to_nvenc_cq(crf) == expected_cq


# --- 3. GPU: CQ値のクランプ(0〜51の範囲外にならない) ---
def test_gpu_cq_is_clamped():
    # 下限は crf + NVENC_CQ_OFFSET が NVENC_CQ_MIN を実際に下回る入力で確認する
    # (crf=0 では 0+4=4 となり下限クランプを一切通らず、テストが構造上失敗しない)。
    assert crf_to_nvenc_cq(-100) == NVENC_CQ_MIN
    assert crf_to_nvenc_cq(100) == NVENC_CQ_MAX


# --- 4. GPU: ビットレート制御系オプションが残っていないこと ---
def test_gpu_has_no_bitrate_control_options():
    options = build_encoder_options(NVENC_ENCODER, 28, "source", None, None, (1920, 1080))
    assert "-maxrate" not in options
    assert "-bufsize" not in options
    assert _value_after(options, "-rc") != "cbr"


# --- 5. GPU: 品質チューニング系オプション ---
def test_gpu_quality_tuning_options():
    options = build_encoder_options(NVENC_ENCODER, 28, "source", None, None, (1920, 1080))
    assert _has_pair(options, "-tune", "hq")
    assert _has_pair(options, "-spatial-aq", "1")
    assert _has_pair(options, "-rc-lookahead", "20")
    assert "-temporal-aq" not in options


# --- 6. GPU: -levelを指定しないこと(NVENCは-levelパラメータをサポートしていない) ---
def test_gpu_options_have_no_level():
    options = build_encoder_options(NVENC_ENCODER, 28, "source", None, None, (1920, 1080))
    assert "-level" not in options


# --- 7. CPU: 従来構成が変わっていないこと(回帰) ---
def test_cpu_options_are_unchanged():
    options = build_encoder_options(X264_ENCODER, 28, "source", None, None, (1920, 1080))
    assert options == [
        "-vcodec", "libx264",
        "-crf", "28",
        "-preset", "slow",
        "-tune", "film",
        "-profile:v", "high",
        "-level", "4.2",
        "-g", "30",
        "-keyint_min", "30",
        "-sc_threshold", "0",
        "-refs", "16",
        "-bf", "3",
    ]


# --- 8. CPU: NVENC私有オプションが混入していないこと ---
def test_cpu_options_have_no_nvenc_private_options():
    options = build_encoder_options(X264_ENCODER, 28, "source", None, None, (1920, 1080))
    for flag in ("-cq", "-rc", "-spatial-aq", "-temporal-aq", "-rc-lookahead", "-maxrate", "-bufsize"):
        assert flag not in options


# --- 9. スケールオプション(-vf)の組み立て ---
@pytest.mark.parametrize(
    "resolution, width, height, expected_vf",
    [
        pytest.param("720p", None, None, "scale=1280:720", id="preset_720p"),
        pytest.param("custom", "640", "360", "scale=640:360", id="custom_640x360"),
        pytest.param("source", None, None, None, id="source_no_scale"),
    ],
)
def test_scale_option(resolution, width, height, expected_vf):
    options = build_encoder_options(X264_ENCODER, 28, resolution, width, height, (1920, 1080))
    if expected_vf is None:
        assert "-vf" not in options
    else:
        assert _value_after(options, "-vf") == expected_vf


# --- 10. カスタム解像度が非数値なら400 ---
def test_custom_resolution_invalid_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        build_encoder_options(X264_ENCODER, 28, "custom", "abc", "360", (1920, 1080))
    assert exc_info.value.status_code == 400


# --- 11. NVENC不可時はlibx264にフォールバックすること ---
def test_falls_back_to_cpu_when_nvenc_unavailable(monkeypatch):
    monkeypatch.setattr(video_router, "is_gpu_encoder_available", lambda: False)
    options = build_ffmpeg_options(28, "source", None, None, use_gpu=True, source_resolution=(1920, 1080))
    assert "h264_nvenc" not in options
    assert "libx264" in options


# --- 12. build系はffprobeを再実行しないこと(1ジョブ1プローブという前提の純関数側の保証) ---
def test_build_does_not_probe_input(monkeypatch):
    monkeypatch.setattr(video_router, "is_gpu_encoder_available", lambda: False)

    resolution_calls = {"n": 0}
    version_calls = {"n": 0}

    def fake_get_video_resolution(filepath):
        resolution_calls["n"] += 1
        return (1920, 1080)

    def fake_get_ffmpeg_version():
        version_calls["n"] += 1
        return "4.4.2"

    monkeypatch.setattr(video_router, "get_video_resolution", fake_get_video_resolution)
    monkeypatch.setattr(video_router, "get_ffmpeg_version", fake_get_ffmpeg_version)

    build_ffmpeg_options(28, "source", None, None, use_gpu=True, source_resolution=(1920, 1080))
    build_encoder_options(X264_ENCODER, 28, "source", None, None, (1920, 1080))

    assert resolution_calls["n"] == 0
    assert version_calls["n"] == 0


# --- 13. -levelが呼び出し元から渡されたsource_resolutionに応じて決まること ---
@pytest.mark.parametrize(
    "source_resolution, expected_level",
    [
        pytest.param((3840, 2160), "4.1", id="4K"),
        pytest.param((1920, 1080), "4.2", id="1080p"),
        pytest.param((1280, 720), "4.1", id="720p"),
        pytest.param(None, "4.2", id="none_defaults_to_1080p"),
    ],
)
def test_level_uses_given_resolution(source_resolution, expected_level):
    assert get_appropriate_level("source", None, None, source_resolution) == expected_level


# --- 14. build_encoder_optionsはGPU可否を一切確認しないこと(CPUフォールバック用に安全に呼べる) ---
def test_build_encoder_options_never_checks_gpu(monkeypatch):
    calls = {"n": 0}

    def fake_is_gpu_encoder_available():
        calls["n"] += 1
        return True

    monkeypatch.setattr(video_router, "is_gpu_encoder_available", fake_is_gpu_encoder_available)
    build_encoder_options(X264_ENCODER, 28, "source", None, None, (1920, 1080))
    build_encoder_options(NVENC_ENCODER, 28, "source", None, None, (1920, 1080))
    assert calls["n"] == 0


# --- 15. R2経路(run_ffmpeg_job_r2)で解像度取得(ffprobe)がジョブ全体で1回だけであること ---
async def test_r2_job_probes_resolution_once(monkeypatch):
    """R2/ローカル両経路が共有する「1ジョブ1プローブ」の前提を、R2経路の実行で確認する。

    実R2・実ffmpegには一切触れない。r2_transfer.run_r2/download_file/upload_fileと
    run_ffmpeg_process、is_gpu_encoder_availableをすべて偽物に差し替え、
    get_video_resolutionだけをカウンタにして呼び出し回数を検証する。
    """
    monkeypatch.setattr(video_router, "clients", {})
    monkeypatch.setattr(video_router, "is_gpu_encoder_available", lambda: False)

    class _FakeR2Client:
        def head_object(self, **kwargs):
            return {"ContentLength": 0}

    monkeypatch.setattr(video_router, "r2_client", _FakeR2Client())

    async def fake_run_r2(func, *args, **kwargs):
        # head_object相当。実際のR2には一切触れない。
        return {"ContentLength": 0}

    async def fake_download_file(bucket, key, filename, callback=None):
        return None

    async def fake_upload_file(filename, bucket, key, callback=None):
        return None

    monkeypatch.setattr(video_router.r2_transfer, "run_r2", fake_run_r2)
    monkeypatch.setattr(video_router.r2_transfer, "download_file", fake_download_file)
    monkeypatch.setattr(video_router.r2_transfer, "upload_file", fake_upload_file)

    async def fake_run_ffmpeg_process(input_path, output_path, ffmpeg_options, cpu_fallback_options, client_id):
        # 出力ファイルが空だとrun_ffmpeg_job_r2が「FFmpeg出力ファイルが空です」で
        # 異常終了してしまうため、数バイト書いておく。テスト専用スタブでの
        # ローカル一時ファイルへの同期writeなので、本番コードのように
        # イベントループを塞ぐ懸念は無い(ASYNC230を明示的に無視する)。
        with open(output_path, "wb") as f:  # noqa: ASYNC230
            f.write(b"fake-output-bytes")

    monkeypatch.setattr(video_router, "run_ffmpeg_process", fake_run_ffmpeg_process)

    resolution_calls = {"n": 0}

    def fake_get_video_resolution(filepath):
        resolution_calls["n"] += 1
        return (1920, 1080)

    monkeypatch.setattr(video_router, "get_video_resolution", fake_get_video_resolution)

    await video_router.run_ffmpeg_job_r2(
        "job-1", "key-1", "test.mp4", 28, "source", None, None, False, "client-1"
    )

    assert resolution_calls["n"] == 1


# --- 16. CPUフォールバック時、run_ffmpeg_processが呼び出し元のcpu_fallback_optionsをそのまま使うこと ---
async def test_cpu_fallback_uses_given_options(monkeypatch, tmp_path):
    """1回目(GPU)をNVENC初期化失敗で失敗させ、2回目(フォールバック)のコマンドが
    cpu_fallback_optionsそのままで組み立てられ、NVENC私有オプションを含まないことを確認する。
    """
    monkeypatch.setattr(video_router, "clients", {})
    monkeypatch.setattr(video_router, "get_video_duration", lambda filepath: 10.0)

    class _FakeStdout:
        async def readline(self):
            # 進捗解析はこのテストの対象外なので、即EOFにして読み取りループを抜けさせる。
            return b""

    class _FakeStderr:
        def __init__(self, data: bytes):
            self._data = data

        async def read(self):
            return self._data

    class _FakeProcess:
        def __init__(self, returncode: int, stderr: bytes = b""):
            self.returncode = returncode
            self.stdout = _FakeStdout()
            self.stderr = _FakeStderr(stderr)

        async def wait(self):
            return self.returncode

        def terminate(self):
            pass

    calls = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(list(args))
        if len(calls) == 1:
            # run_ffmpeg_processのフォールバック判定文字列("h264_nvenc"と
            # "InitializeEncoder failed")の両方を含む、NVENC初期化失敗を模したエラー。
            return _FakeProcess(1, stderr=b"h264_nvenc: InitializeEncoder failed")
        return _FakeProcess(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    input_path = str(tmp_path / "in.mp4")
    output_path = str(tmp_path / "out.mp4")
    gpu_options = build_encoder_options(NVENC_ENCODER, 28, "source", None, None, (1920, 1080))
    cpu_fallback_options = build_encoder_options(X264_ENCODER, 28, "source", None, None, (1920, 1080))

    await video_router.run_ffmpeg_process(
        input_path, output_path, gpu_options, cpu_fallback_options, "nonexistent-client"
    )

    assert len(calls) == 2
    expected_second_command = (
        ["ffmpeg", "-y", "-i", input_path] + cpu_fallback_options + ["-progress", "pipe:1", "-nostats", output_path]
    )
    assert calls[1] == expected_second_command

    nvenc_private_flags = {"-cq", "-rc", "-spatial-aq", "-rc-lookahead"}
    assert nvenc_private_flags.isdisjoint(calls[1])
    assert not _has_pair(calls[1], "-preset", "p5")
    assert not _has_pair(calls[1], "-tune", "hq")
