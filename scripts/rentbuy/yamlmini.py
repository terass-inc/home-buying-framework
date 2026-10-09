"""assumptions.yaml を読むための最小限の YAML パーサー（標準ライブラリのみ）。

対応する構文は assumptions.yaml が実際に使っている範囲だけに絞っている。
- インデントによるブロックマッピング（`key: value` / `key:` の後に入れ子）
- ブロックシーケンス（`- 値` / `- {a: 1, b: 2}`）
- 1行のフローマッピング（`{a: 1, b: 2}`。入れ子なし）。`key:` の次行に単独で置く形も可
- スカラー: 整数・小数・true/false/null・ダブルクォート文字列・プレーン文字列
- `#` コメント（行頭、または空白の直後でクォートの外にあるもの）

想定外の構文（タブインデント、複数行文字列、アンカー、入れ子のフロー構文、
インデントの食い違い、キーの重複など）は YamlError で即座に失敗させる。
黙って誤読するより、壊れたことが分かるほうが計算エンジンとして安全だから。
"""
from __future__ import annotations

import re

__all__ = ["YamlError", "loads"]


class YamlError(ValueError):
    """assumptions.yaml を解釈できなかったことを表す。行番号を含める。"""


_INT_RE = re.compile(r"^[-+]?\d+$")
_FLOAT_RE = re.compile(r"^[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")
_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_\-]*)\s*:(\s+|$)(.*)$")
_UNSUPPORTED_START = ("|", ">", "&", "*", "!", "[", "'")


def _strip_comment(text: str) -> str:
    """クォートの外にある `#`（行頭または空白の直後）以降を落とす。"""
    in_quote = False
    prev = " "
    for idx, ch in enumerate(text):
        if ch == '"' and prev != "\\":
            in_quote = not in_quote
        elif ch == "#" and not in_quote and prev in " \t":
            return text[:idx].rstrip()
        prev = ch
    if in_quote:
        raise YamlError("クォートが閉じていない")
    return text.rstrip()


def _parse_scalar(raw: str, lineno: int):
    s = raw.strip()
    if s == "":
        return None
    if s.startswith('"'):
        if len(s) < 2 or not s.endswith('"'):
            raise YamlError(f"{lineno}行目: ダブルクォート文字列が閉じていない: {s!r}")
        body = s[1:-1]
        if re.search(r'(?<!\\)"', body):
            raise YamlError(f"{lineno}行目: 文字列の途中にクォートがある: {s!r}")
        return body.replace('\\"', '"').replace("\\\\", "\\")
    if s.startswith(_UNSUPPORTED_START):
        raise YamlError(f"{lineno}行目: このパーサーが対応しない構文: {s!r}")
    if s.startswith("{"):
        return _parse_flow_map(s, lineno)
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~"):
        return None
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    return s  # プレーン文字列（日付 2026-09-17 なども文字列のまま返す）


def _parse_flow_map(s: str, lineno: int) -> dict:
    if not s.endswith("}"):
        raise YamlError(f"{lineno}行目: フローマッピングが1行で閉じていない: {s!r}")
    body = s[1:-1].strip()
    if "{" in body or "}" in body or "[" in body:
        raise YamlError(f"{lineno}行目: 入れ子のフロー構文には対応しない: {s!r}")
    result: dict = {}
    if not body:
        return result
    for part in body.split(","):
        if ":" not in part:
            raise YamlError(f"{lineno}行目: フローマッピングの要素に ':' がない: {part!r}")
        key, val = part.split(":", 1)
        key = key.strip()
        if not key or key in result:
            raise YamlError(f"{lineno}行目: フローマッピングのキーが空か重複: {key!r}")
        if '"' in val:
            raise YamlError(f"{lineno}行目: フローマッピング内の文字列には対応しない: {part!r}")
        result[key] = _parse_scalar(val, lineno)
    return result


def _tokenize(text: str):
    """(行番号, インデント幅, 本文) の列にする。空行・コメント行は落とす。"""
    lines = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlError(f"{lineno}行目: タブによるインデントには対応しない")
        try:
            content = _strip_comment(raw)
        except YamlError as exc:
            raise YamlError(f"{lineno}行目: {exc}") from None
        if not content.strip():
            continue
        if content.strip() in ("---", "..."):
            raise YamlError(f"{lineno}行目: 複数ドキュメントには対応しない")
        indent = len(content) - len(content.lstrip(" "))
        lines.append((lineno, indent, content.strip()))
    return lines


def _parse_block(lines, pos: int, indent: int):
    """lines[pos] から始まる、インデント indent のブロックを読む。(値, 次の位置) を返す。"""
    lineno, ind, body = lines[pos]
    if ind != indent:
        raise YamlError(f"{lineno}行目: インデントが想定と違う（{ind} 文字、想定 {indent} 文字）")
    if body.startswith("- ") or body == "-":
        return _parse_seq(lines, pos, indent)
    if body.startswith("{"):
        value = _parse_flow_map(body, lineno)
        nxt = pos + 1
        if nxt < len(lines) and lines[nxt][1] >= indent:
            raise YamlError(f"{lines[nxt][0]}行目: フローマッピングの後に同じブロックの続きがある")
        return value, nxt
    return _parse_map(lines, pos, indent)


def _parse_seq(lines, pos: int, indent: int):
    items = []
    while pos < len(lines):
        lineno, ind, body = lines[pos]
        if ind < indent:
            break
        if ind > indent:
            raise YamlError(f"{lineno}行目: リスト要素のインデントが深すぎる")
        if not (body.startswith("- ") or body == "-"):
            raise YamlError(f"{lineno}行目: リストの途中にリスト以外の行がある")
        item = body[1:].strip()
        if not item:
            raise YamlError(f"{lineno}行目: 空のリスト要素・入れ子のブロックには対応しない")
        if _KEY_RE.match(item) and not item.startswith("{"):
            raise YamlError(f"{lineno}行目: リスト要素のブロックマッピングには対応しない（フロー形式で書く）")
        items.append(_parse_scalar(item, lineno))
        pos += 1
    return items, pos


def _parse_map(lines, pos: int, indent: int):
    result: dict = {}
    while pos < len(lines):
        lineno, ind, body = lines[pos]
        if ind < indent:
            break
        if ind > indent:
            raise YamlError(f"{lineno}行目: インデントが深すぎる（{ind} 文字、想定 {indent} 文字）")
        m = _KEY_RE.match(body)
        if not m:
            raise YamlError(f"{lineno}行目: 'key: value' の形ではない: {body!r}")
        key, rest = m.group(1), m.group(3).strip()
        if key in result:
            raise YamlError(f"{lineno}行目: キーが重複している: {key!r}")
        pos += 1
        if rest:
            result[key] = _parse_scalar(rest, lineno)
            continue
        if pos < len(lines) and lines[pos][1] > indent:
            result[key], pos = _parse_block(lines, pos, lines[pos][1])
        else:
            result[key] = None
    return result, pos


def loads(text: str) -> dict:
    """YAML テキストを dict にする。対応外の構文は YamlError。"""
    lines = _tokenize(text)
    if not lines:
        raise YamlError("中身が空")
    if lines[0][1] != 0:
        raise YamlError(f"{lines[0][0]}行目: 先頭行がインデントされている")
    value, pos = _parse_map(lines, 0, 0)
    if pos != len(lines):
        raise YamlError(f"{lines[pos][0]}行目: 解釈できない行が残った")
    return value
