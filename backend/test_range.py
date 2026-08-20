#!/usr/bin/env python3
"""
共有動画プレビューのRangeリクエスト解析（video_router.parse_single_byte_range）のテスト。

parse_single_byte_rangeはI/Oを一切含まない純関数として実装されているため、
R2クライアントやDBのセットアップは不要。境界条件は設計書「§B. Rangeリクエスト処理の
擬似コード」に記載の表（total=1000の全ケース + total=0のケース）をそのまま網羅する。

test_security.pyと異なりpytest形式で書く（このファイル固有の方針）。
"""
import os
import sys

# routers.video_router を "backend" 直下からの絶対importとして解決するため、
# このファイルのディレクトリ（backend/）をsys.pathに加える。
# test_security.py（utils.securityをimportしている）と同じやり方。
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import pytest

from routers.video_router import parse_single_byte_range, UNSATISFIABLE


def _assert_result(result, expected):
    """UNSATISFIABLEはモジュール内のシングルトン(object())なのでis比較、
    それ以外（None / (start, end)タプル）は値比較する。
    """
    if expected is UNSATISFIABLE:
        assert result is UNSATISFIABLE
    else:
        assert result == expected


# --- total = 1000 における境界条件（設計書 §B の表と一対一対応） ---
TOTAL_1000_CASES = [
    pytest.param(None, None, id="no-header_全体を200で返す"),
    pytest.param(
        "bytes=0-", (0, 999),
        id="bytes=0-_全体長と同じ範囲でも200に落とさず206のまま(Safariがこれを前提にする)"
    ),
    pytest.param("bytes=100-199", (100, 199), id="bytes=100-199_通常の範囲指定"),
    pytest.param("bytes=-500", (500, 999), id="bytes=-500_suffix形式(末尾500バイト)"),
    pytest.param(
        "bytes=-5000", (0, 999),
        id="bytes=-5000_suffix長がtotalを超えたら全体に丸める(416にしない)"
    ),
    pytest.param("bytes=0-0", (0, 0), id="bytes=0-0_1バイトだけの範囲"),
    pytest.param(
        "bytes=999-5000", (999, 999),
        id="bytes=999-5000_終端がtotalを超えたら丸める(416にしない)"
    ),
    pytest.param("bytes=1000-", UNSATISFIABLE, id="bytes=1000-_開始位置が範囲外なら416"),
    pytest.param("bytes=500-100", UNSATISFIABLE, id="bytes=500-100_start>endは416"),
    pytest.param("bytes=-0", UNSATISFIABLE, id="bytes=-0_suffix長0は満たせないので416"),
    pytest.param(
        "bytes=0-99,200-299", None,
        id="複数レンジ_416にせず200で全体を返す(複数レンジを送るのは単一レンジしか"
           "送らないブラウザではないクライアントのみ)"
    ),
    pytest.param("bytes=abc-def", None, id="非数値_構文不正は無視して200"),
    pytest.param("bytes=-", None, id="suffix値が空_構文不正は無視して200"),
    pytest.param("bytes=-1-2", None, id="ハイフンが余分_構文不正は無視して200"),
    pytest.param("seconds=1-2", None, id="unknown_unit_bytes以外は無視して200"),
]


@pytest.mark.parametrize("range_header, expected", TOTAL_1000_CASES)
def test_parse_single_byte_range_total_1000(range_header, expected):
    result = parse_single_byte_range(range_header, 1000)
    _assert_result(result, expected)


# --- total = 0（ゼロバイトファイル）における境界条件 ---
# 圧縮出力が0バイトならrun_ffmpeg_job_r2が例外にするので実運用では起きないが、
# total-1 == -1 での添字計算事故を防ぐガード（parse_single_byte_range内の手順5）を検証する。
TOTAL_0_CASES = [
    pytest.param(None, None, id="no-header_totalが0でもヘッダ無しは200"),
    pytest.param("bytes=0-", UNSATISFIABLE, id="total=0_任意のRangeは416"),
    pytest.param("bytes=-1", UNSATISFIABLE, id="total=0_suffix形式でも416"),
    pytest.param("bytes=0-0", UNSATISFIABLE, id="total=0_1バイト指定でも416"),
    pytest.param(
        "bytes=0-99,200-299", None,
        id="total=0_複数レンジはtotalチェックより前に弾かれ200のまま"
    ),
    pytest.param(
        "seconds=1-2", None,
        id="total=0_unknown_unitはtotalチェックより前に弾かれ200のまま"
    ),
]


@pytest.mark.parametrize("range_header, expected", TOTAL_0_CASES)
def test_parse_single_byte_range_total_0(range_header, expected):
    result = parse_single_byte_range(range_header, 0)
    _assert_result(result, expected)


# --- 敵対的入力（回帰テスト） ---
# 背景: 判定に str.isdigit() を使っていたため、Unicodeの上付き数字（'¹' U+00B9）が
# isdigit()=True を通過し、続く int('¹') が ValueError を送出していた。
# parse_single_byte_range は認証不要の /share/{token}/preview から try で包まずに
# 呼ばれるため、この例外はエンドポイントを貫通して HTTP 500 になっていた
# （h11 は生バイト 0xB9 を正常なヘッダとして受理し、Starlette が latin-1 で '¹' に
#   デコードする。nginx もそのまま転送するので、共有リンクを持つ匿名ユーザーが
#   1リクエストで500を作れた）。
#
# isdecimal() への置換も不可。全角 '０-９' やアラビア数字 '٥' を受理してしまい、
# クラッシュこそしないが、HTTPのバイトレンジ仕様上あり得ない値で206を返す
# （修正前の実測: `bytes=-٥` -> (995, 999) / `bytes=０-９` -> (0, 9)）。
#
# したがって期待値は全て None（Rangeを無視して200で全体を返す）。
HOSTILE_CASES = [
    pytest.param("bytes=0-¹", id="上付き1_U+00B9_isdigitはTrueだがint()がValueErrorになる"),
    pytest.param("bytes=-²", id="上付き2_U+00B2_suffix形式"),
    pytest.param("bytes=³-¹", id="上付き3と1_両端とも上付き数字"),
    pytest.param("bytes=０-９", id="全角０-９_isdecimalはTrueなので使ってはいけない"),
    pytest.param("bytes=-٥", id="アラビア数字٥_U+0665_isdecimalはTrue"),
    pytest.param("bytes=0-١٠", id="アラビア数字١０_U+0661U+0660_終端指定"),
    pytest.param("bytes=٩-", id="アラビア数字٩_U+0669_開始位置のみ"),
    pytest.param("bytes=0-۵", id="拡張アラビア数字۵_U+06F5"),
    pytest.param("bytes=०-९", id="デーヴァナーガリー数字०-९_U+0966"),
    pytest.param("bytes=0-②", id="丸数字②_U+2461_isdigitはTrue"),
    pytest.param("bytes=-½", id="分数½_U+00BD_isnumericのみTrue"),
    pytest.param("bytes=0-" + "9" * 20, id="20桁_CPythonのint桁数制限(4300桁)未満だが上限外として無視する"),
    pytest.param("bytes=" + "9" * 5000 + "-", id="5000桁_int()が桁数制限でValueErrorになる長さ"),
    pytest.param("bytes=-" + "1" * 4301, id="4301桁_suffix形式でint()の桁数制限を超える"),
]


@pytest.mark.parametrize("range_header", HOSTILE_CASES)
def test_parse_single_byte_range_hostile_input_is_ignored(range_header):
    """非ASCII数字・桁数過大は例外にせず None（200で全体を返す）に落とす。"""
    assert parse_single_byte_range(range_header, 1000) is None


def test_parse_single_byte_range_accepts_19_digits():
    """上限の19桁はASCII数字として受理し、終端がtotalを超えるので丸める。
    （桁数上限を下げすぎて正常なリクエストを弾いていないことの確認）
    """
    assert parse_single_byte_range("bytes=0-" + "9" * 19, 1000) == (0, 999)


# 例外を出さないことの保証。
# 上の HOSTILE_CASES に加え、構造そのものが壊れた入力・空白・大文字小文字違い・
# 制御文字などを total を変えながら総当たりする。
NEVER_RAISE_INPUTS = [
    None, "", " ", "-", "bytes", "bytes=", "bytes=-", "bytes=--", "bytes=-1-2",
    "bytes=abc-def", "bytes= 0 - 9 ", "BYTES=0-9", "Bytes=0-", "bytes=0-9,",
    "bytes=,", "bytes=0-9,10-19", "bytes=+5-", "bytes=-+5", "bytes=0x10-",
    "bytes=1e3-", "bytes=０-９", "bytes=0-¹", "bytes=-٥", "bytes=٥-٩",
    "bytes=¹²³-", "bytes=" + "9" * 5000 + "-9",
    "bytes=0-" + "9" * 5000, "bytes=-" + "9" * 5000,
    "bytes=nan-inf", "bytes=１-", "seconds=1-2", "items=0-9", "bytes=0-9\x00",
    "bytes=\t0-9\n", "bytes=１２３-４５６", "bytes=٠-", "bytes=-٠",
]


@pytest.mark.parametrize("total", [0, 1, 1000, 2 ** 40])
@pytest.mark.parametrize("range_header", NEVER_RAISE_INPUTS)
def test_parse_single_byte_range_never_raises(range_header, total):
    """どんな入力でも例外を送出しないこと。

    このテストが落ちる形（＝例外がそのまま出る形）は、認証不要エンドポイントの
    HTTP 500 に直結する。戻り値の型も 3種（None / UNSATISFIABLE / (start, end)）に
    限られることを併せて確認する。
    """
    result = parse_single_byte_range(range_header, total)

    if result is None or result is UNSATISFIABLE:
        return
    assert isinstance(result, tuple) and len(result) == 2
    start, end = result
    assert isinstance(start, int) and isinstance(end, int)
    assert 0 <= start <= end <= total - 1
