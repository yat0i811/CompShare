"""async def 内でR2クライアントを直接叩いていないことを、ASTで機械的に保証するテスト。

【なぜruffではなくこれが要るのか】
`pyproject.toml`で有効にしているruffのASYNCルール群は、`async def`内の同期I/Oを
検知してくれるが、その検知対象は**ruffが知っている組み込みの固定リスト**
（`open` / `os.*` / `time.sleep` / `subprocess` / `requests` / `httpx` / `urllib`）
に限られる。boto3やbotocoreはこのリストに含まれないため、
`async def`の中に`r2_client.head_object(...)`と直書きしても**ruffは何も言わない**
（実際に書いて`ruff check .`が0件で通ることを確認済み）。
つまりdocs/CLOSE_ISSUES.md §4-1で起きた事故（同期のR2呼び出しでイベントループが
約74秒停止した）そのものは、ruffでは検知できない。その直接の再発を守るのがこのテストである。

【判定の方針】
`main.py`と`routers/*.py`をastで解析し、`AsyncFunctionDef`の本体に
`r2_client.<method>(...)`の直接呼び出しがあれば失敗させる。ただし
**ネストされた同期`def`の内側は対象から除外する**。走査ループや削除ループを丸ごと
同期関数にまとめて`r2_transfer.run_r2()`へ渡す既存パターン（main.pyの
`_delete_expired_objects_from_r2`等）や、StreamingResponseに渡す同期ジェネレータは
「スレッドで動かすための正しい書き方」であり、そこに同期呼び出しがあるのは正当なため。

【このテストが検知“できない”パターン（網羅的な静的解析ではない）】
判定は「`r2_client.<method>(...)`という名前ベースの直呼び出し」だけを見ているため、
次はすべてすり抜ける。いずれも現行コードには無いことをレビューで確認済みだが、
将来書かれたときにこのテストは何も言わない:
  - `await run_r2(r2_client.get_paginator, ...)`で得たページャを`async def`内で
    そのまま`for page in paginator.paginate(...)`する形。`paginate()`は遅延評価で
    イテレート時にネットワークI/Oが走るため、§4-1の事故と同型になる。
  - `c = r2_client`のようなエイリアス経由の呼び出し（属性の受け側の名前が違う）。
  - `response["Body"].read()`など、クライアント以外のオブジェクトに対する同期I/O。
逆に`run_r2(lambda: r2_client.head_object(...))`は正当だが、lambdaの中身も
asyncコンテキストのASTとして走査されるため**誤検知する**（現行コードでは未使用）。
つまりこれは網羅的な静的解析ではなく、「最も踏みやすい直呼びの回帰」を止める安全網である。
"""
import ast
import os

import pytest

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))

# R2クライアントを保持しているモジュール変数名。この名前のオブジェクトへの
# 属性呼び出しをネットワークI/Oとみなす。
_R2_CLIENT_NAME = "r2_client"

# 例外的に async def 内での直接呼び出しを許すメソッド。
# generate_presigned_urlはローカルで署名文字列を組み立てるだけでネットワークI/Oを
# 伴わないため（botocoreはリクエストを送信しない）、意図的に同期のまま呼んでいる。
_ALLOWED_METHODS = {"generate_presigned_url"}

# 走査対象のasync関数がこの数を下回ったら、解析対象の指定ミス（パスの取り違え等）で
# 空振りしている可能性が高い。
# 閾値はファイル脱落を検知するためのもの。10だとmain.py単独（10個）で満たしてしまい、
# routers/video_router.pyが対象から丸ごと抜けても気づけなかった（レビュー指摘）。
# 実測内訳（合計59個）: video_router 32 / admin_router 12 / main 10 / auth_router 5。
_MIN_ASYNC_FUNCTIONS = 50

# 走査対象に必ず含まれていなければならないファイル（_BACKEND_DIRからの相対パス）。
# 件数の下限だけでは、小さいファイルが1つ抜けても合計が閾値を上回って気づけないため、
# R2クライアントを握っている主要モジュールの存在自体をファイル名で固定する。
_REQUIRED_FILES = {
    "main.py",
    os.path.join("routers", "video_router.py"),
    os.path.join("routers", "admin_router.py"),
    os.path.join("routers", "auth_router.py"),
}


def _target_files() -> list[str]:
    """解析対象: main.py と routers/*.py（R2クライアントを直接握っているモジュール）。"""
    paths = [os.path.join(_BACKEND_DIR, "main.py")]
    routers_dir = os.path.join(_BACKEND_DIR, "routers")
    for name in sorted(os.listdir(routers_dir)):
        if name.endswith(".py"):
            paths.append(os.path.join(routers_dir, name))
    return paths


def _iter_async_body(node: ast.AST):
    """asyncコンテキストのまま実行されるノードだけを再帰的に列挙する。

    ネストされたFunctionDef（同期def）に入ったらそこで打ち切る。同期関数の中身は
    別スレッド（run_r2）で実行されるのが本アプリの流儀であり、asyncとして評価しては
    ならないため。ネストされたAsyncFunctionDefは呼び出し側で別途走査されるので
    ここでも打ち切ってよい（二重報告を避ける）。
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield child
        yield from _iter_async_body(child)


def _find_violations(path: str) -> tuple[list[str], int]:
    """(違反の説明文リスト, 走査したasync関数の数) を返す。"""
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    violations: list[str] = []
    async_function_count = 0
    try:
        filename = os.path.relpath(path, _BACKEND_DIR)
    except ValueError:
        # Windowsで別ドライブのパスを渡されるとrelpathがValueErrorになる。表示用途なので
        # そのままフルパスで代用する（このテスト自体を検証するときに通る経路）。
        filename = path

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        async_function_count += 1
        for child in _iter_async_body(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if not isinstance(func, ast.Attribute):
                continue
            if not isinstance(func.value, ast.Name) or func.value.id != _R2_CLIENT_NAME:
                continue
            if func.attr in _ALLOWED_METHODS:
                continue
            violations.append(
                f"{filename}:{child.lineno}:{node.name}() が "
                f"{_R2_CLIENT_NAME}.{func.attr}() を async のまま直接呼んでいる"
            )
    return violations, async_function_count


@pytest.mark.parametrize("path", _target_files(), ids=lambda p: os.path.basename(p))
def test_no_direct_r2_client_call_in_async_function(path):
    """async def 内で r2_client.<method>() を直接呼んでいないこと。

    違反した場合の直し方は2通り:
      - `await r2_transfer.run_r2(r2_client.head_object, Bucket=..., Key=...)` にする
      - ループごと同期の内部関数にまとめて `await r2_transfer.run_r2(その関数)` に渡す
    """
    violations, _ = _find_violations(path)
    assert not violations, (
        "async def 内に同期のR2呼び出しがあります（docs/CLOSE_ISSUES.md §4-1 の再発）:\n  "
        + "\n  ".join(violations)
    )


def test_async_hygiene_scan_is_not_vacuous():
    """サニティチェック: 解析が空振りしていないこと。

    対象ファイルのパス指定ミスやast解析の破綻で「1つもasync関数を見つけていない」のに
    全部緑、という偽の安心を避けるため、(1) 主要ファイルが対象集合に入っていること、
    (2) 走査できたasync関数の総数、の両方を下限で固定する。
    """
    paths = _target_files()
    scanned = {os.path.relpath(path, _BACKEND_DIR) for path in paths}
    missing = _REQUIRED_FILES - scanned
    assert not missing, (
        f"解析対象から次のファイルが漏れています: {sorted(missing)}。"
        "_target_files()のパス指定が壊れている可能性があります"
    )

    total = sum(_find_violations(path)[1] for path in paths)
    assert total >= _MIN_ASYNC_FUNCTIONS, (
        f"走査できたasync関数が{total}個しかありません。解析対象の指定が壊れている可能性があります"
    )
