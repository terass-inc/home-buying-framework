#!/usr/bin/env python3
"""LP や PR 記事に置く「AIに聞いてみる」ボタン用の URL を生成する。

ChatGPT は https://chatgpt.com/?q=<prompt>、Claude は https://claude.ai/new?q=<prompt> で
入力欄にプロンプトを入れた状態で開ける。2種類を出力する。

- embed（推奨）: dist/lite.md の全文を q に埋め込む。AIがURLを開く必要がないので無料版でも動く。
  日本語は URL エンコードで約9倍に膨らみ、約36,000文字になる。ブラウザのリンクとしては問題ないが、
  QRコード（上限約4,000文字）や一部の短縮URLサービスには入らない。
- fetch: GitHub のリポジトリURL（README に AI向け前提を埋め込み済み）と要点だけを渡す（約4,000文字）。QR や短縮URL向け。
  無料版 ChatGPT は URL を開けないことが多いため、読めなかった場合の逃げ道（要点版の5ルール）を含める。

※ 各サービスの q パラメータの挙動と受け付ける長さは変わることがある。公開前に実機で確認すること。
使い方: python3 scripts/make_button_urls.py [embed|fetch|all]
"""
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/terass-inc/home-buying-framework"
SERVICES = [("ChatGPT", "https://chatgpt.com/?q="), ("Claude", "https://claude.ai/new?q=")]

FETCH_PROMPT = f"""まず次のページの「AIへの前提」以下を読み、その前提で住宅購入の相談に答えてください。
{REPO}

URLを実際に読めた場合だけ「完全版を読み込みました」と言い、読めなかった場合は読めたふりをせず「要点版で進めます」と言ってから、最低限次の5つを守ってください。
1. 結論より先に「なぜ買うのか」を掘り（「家賃がもったいない」は理由ではない）、次に「何年住む予定か（売却時期）」を確認する
2. ローン期間は利息総額でなく、売却時の残債と手残りで考える。原則は組める最長で組み、短縮は繰上返済で行う
3. 「借りられる額」（年収7〜8倍）と「借りるべき額」（ライフプランから逆算）を混同しない
4. 賃貸と購入の比較は、購入側に売却後の手残りと修繕積立金の上昇、賃貸側に更新料と住み替え時の初期費用を入れる
5. 市況の予測とエリアの優劣は断言しない

準備ができたら、要約や復唱をせず「前提を読み込みました。まず、家を買おうと思ったきっかけを教えてください」の1文だけ返して会話を始めてください。確認事項は一度に全部聞かず、順に聞いてください。
"""


def embed_prompt() -> str:
    # lite.md は冒頭に「この文章だけが送られたときの返し方」の指示を含むので、そのまま渡せる
    return (ROOT / "dist" / "lite.md").read_text(encoding="utf-8")


def print_urls(label: str, prompt: str) -> None:
    q = urllib.parse.quote(prompt, safe="")
    print(f"===== {label}（本文{len(prompt)}字）=====")
    for name, base in SERVICES:
        url = base + q
        print(f"[{name}] {len(url)}文字\n{url}\n")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("embed", "all"):
        print_urls("embed: lite全文を埋め込み（推奨）", embed_prompt())
    if mode in ("fetch", "all"):
        print_urls("fetch: URL読み込み＋要点（QR・短縮URL向け）", FETCH_PROMPT)


if __name__ == "__main__":
    main()
