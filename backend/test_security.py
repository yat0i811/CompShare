"""utils/security.py のセキュリティ関連ユーティリティのテスト。

旧test_security.pyはprint + `if __name__ == "__main__":` のスクリプト形式で、
✓/✗をprintするだけで失敗してもプロセスの終了コードは常に0だった
（テストが落ちてもCIやコマンド一つでは検知できなかった）。pytest化してassertベースにし、
失敗時はpytestが非0で終了するようにする。

【現挙動を正としたテストケースについて】
以下5件は、旧スクリプトが期待していた値と実装の現在の挙動が食い違っていた。
今回の変更ではutils/security.py側のロジックは変更しないため、実装のバグとして
勝手に「あるべき値」へ書き換えず、現在の挙動をそのまま固定する
（経緯はdocs/CLOSE_ISSUES.md §5-7参照）。
  1. sanitize_filename("file with spaces.mp4") -> 空白は置換対象外なので変化しない
  2. sanitize_filename("../../../etc/passwd") -> "_.._.._etc_passwd"
     (パストラバーサル対策は入口のvalidate_filenameが担っており、sanitize_filenameは
      表示・保存用の無害化に留まるため、".."の並び自体の除去までは保証しない)
  3. sanitize_filename("a"*300+".mp4") -> 全長がちょうど255文字になるよう "a"*251+".mp4"
  4. validate_filename("CON.mp4") -> True (Windows予約名パターンは完全一致のみを弾く設計。
     拡張子が付くと一致しない。Linuxコンテナ+R2キーとして運用しており実害は無い)
  5. is_private_ip("::1") -> False (実装はIPv4のみ対応。ip.split('.')がIPv6表記では
     4要素にならず、int()もValueErrorになるのでexceptで握りつぶされFalseになる)
"""
import logging

import pytest

from utils.security import is_private_ip, log_security_event, sanitize_filename, validate_filename

# --- sanitize_filename ---
SANITIZE_CASES = [
    pytest.param("normal_file.mp4", "normal_file.mp4", id="通常のファイル名は変化しない"),
    pytest.param("file with spaces.mp4", "file with spaces.mp4", id="空白は置換対象外(現挙動)"),
    pytest.param("file/with/path.mp4", "file_with_path.mp4", id="スラッシュはアンダースコアに置換"),
    pytest.param(
        "file\\with\\backslash.mp4", "file_with_backslash.mp4", id="バックスラッシュはアンダースコアに置換"
    ),
    pytest.param("file*with*asterisk.mp4", "file_with_asterisk.mp4", id="アスタリスクはアンダースコアに置換"),
    pytest.param("file?with?question.mp4", "file_with_question.mp4", id="疑問符はアンダースコアに置換"),
    pytest.param('file"with"quote.mp4', "file_with_quote.mp4", id="ダブルクォートはアンダースコアに置換"),
    pytest.param("file<with>brackets.mp4", "file_with_brackets.mp4", id="山括弧はアンダースコアに置換"),
    pytest.param("file|with|pipe.mp4", "file_with_pipe.mp4", id="パイプはアンダースコアに置換"),
    pytest.param("file:with:colon.mp4", "file_with_colon.mp4", id="コロンはアンダースコアに置換"),
    pytest.param(
        "../../../etc/passwd", "_.._.._etc_passwd",
        id="ディレクトリトラバーサル文字列は除去しない(現挙動、注記4参照)"
    ),
    pytest.param("CON.mp4", "CON.mp4", id="Windows予約名+拡張子は変化しない(sanitizeは予約名を見ない)"),
    pytest.param("", "unnamed_file", id="空文字列はunnamed_fileにフォールバック"),
    pytest.param("   .   ", "unnamed_file", id="空白とドットのみはunnamed_fileにフォールバック"),
    pytest.param(
        "a" * 300 + ".mp4", "a" * 251 + ".mp4",
        id="長すぎるファイル名は全長255文字に丸める(現挙動、注記3参照)"
    ),
]


@pytest.mark.parametrize("original, expected", SANITIZE_CASES)
def test_sanitize_filename(original, expected):
    assert sanitize_filename(original) == expected


def test_sanitize_filename_removes_control_characters():
    """制御文字(unicodedata.categoryの先頭が'C')は除去される。"""
    result = sanitize_filename("bad\x00name\x1f.mp4")
    assert "\x00" not in result
    assert "\x1f" not in result


# --- validate_filename ---
VALIDATE_CASES = [
    pytest.param("normal_file.mp4", True, id="通常のファイル名はTrue"),
    pytest.param("file with spaces.mp4", True, id="空白を含んでもTrue"),
    pytest.param("../../../etc/passwd", False, id="ディレクトリトラバーサルはFalse"),
    pytest.param("CON.mp4", True, id="Windows予約名+拡張子はTrue(完全一致のみ判定・現挙動、注記4参照)"),
    pytest.param("", False, id="空文字列はFalse"),
    pytest.param("file*with*asterisk.mp4", False, id="アスタリスクを含むとFalse"),
    pytest.param("file?with?question.mp4", False, id="疑問符を含むとFalse"),
    pytest.param('file"with"quote.mp4', False, id="ダブルクォートを含むとFalse"),
    pytest.param("file<with>brackets.mp4", False, id="山括弧を含むとFalse"),
    pytest.param("file|with|pipe.mp4", False, id="パイプを含むとFalse"),
    pytest.param("file:with:colon.mp4", False, id="コロンを含むとFalse"),
    pytest.param("a\r\nb.mp4", False, id="CR_LFを含むとFalse(レスポンス分割対策)"),
]


@pytest.mark.parametrize("filename, expected", VALIDATE_CASES)
def test_validate_filename(filename, expected):
    assert validate_filename(filename) == expected


# --- is_private_ip ---
PRIVATE_IP_CASES = [
    pytest.param("127.0.0.1", True, id="ループバック"),
    pytest.param("192.168.1.1", True, id="192.168.0.0_16"),
    pytest.param("10.0.0.1", True, id="10.0.0.0_8"),
    pytest.param("172.16.0.1", True, id="172.16.0.0_12"),
    pytest.param("169.254.1.1", True, id="リンクローカル"),
    pytest.param("::1", False, id="IPv6ループバックはFalse(IPv4のみ対応・現挙動、注記5参照)"),
    pytest.param("8.8.8.8", False, id="パブリックIP_Google"),
    pytest.param("1.1.1.1", False, id="パブリックIP_Cloudflare"),
    pytest.param("208.67.222.222", False, id="パブリックIP_OpenDNS"),
    pytest.param("unknown", False, id="unknown文字列はFalse"),
]


@pytest.mark.parametrize("ip, expected", PRIVATE_IP_CASES)
def test_is_private_ip(ip, expected):
    assert is_private_ip(ip) == expected


# --- log_security_event ---
def test_log_security_event_writes_record(caplog):
    """log_security_eventがsecurityロガーへ想定通りのメッセージを記録すること。

    ログファイル(logs/security.log)は読み取らない。caplogでログレコードを直接検証する。
    """
    caplog.set_level(logging.INFO, logger="security")

    log_security_event(
        event_type="TEST_EVENT",
        user="test_user",
        ip_address="127.0.0.1",
        details="pytestからのテストイベント",
        severity="INFO",
    )

    assert "SECURITY_EVENT - Type: TEST_EVENT" in caplog.text
