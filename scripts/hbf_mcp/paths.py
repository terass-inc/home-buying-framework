"""文章（principles/・cases/・assumptions.yaml など）の置き場所を解決する。

リポジトリから起動したときはリポジトリ直下を、パッケージ（uvx など）から起動したときは
同梱した _data/ を使う。どちらでも同じ前提・同じ計算になる。
"""
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]


def data_root() -> Path:
    if (_REPO / "AGENTS.md").exists() and (_REPO / "principles").is_dir():
        return _REPO
    bundled = _HERE / "_data"
    if (bundled / "AGENTS.md").exists():
        return bundled
    raise FileNotFoundError("フレームワークの文章が見つかりません（リポジトリ直下にも _data/ にもない）")


ROOT = data_root()
