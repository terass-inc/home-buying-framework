"""python3 -m scripts.rentbuy（リポジトリ直下）または python3 -m rentbuy（scripts/ 内）で実行する。"""
import sys

from .cli import main

sys.exit(main())
