#!/usr/bin/env python3
"""LP や PR 記事に置く「AIに聞いてみる」ボタン用の URL を生成する。

ChatGPT は https://chatgpt.com/?q=<prompt>、Claude は https://claude.ai/new?q=<prompt> で
入力欄にプロンプトを入れた状態で開ける。URL 長の制約（約2,000字を目安）があるため、
本文は埋め込まず、AGENTS.md の URL と要点だけを渡す。
※ 各サービスの q パラメータの挙動は変わることがある。公開前に実機で確認すること。
"""
import urllib.parse

RAW = "https://raw.githubusercontent.com/terass-inc/home-buying-framework/main/AGENTS.md"

PROMPT = f"""まず次のURLの内容を読み、その前提で住宅購入の相談に答えてください。
{RAW}

URLを読めない場合は、最低限次の5つを守ってください。
1. 結論より先に「何年住む予定か（売却時期）」を確認する
2. ローン期間は利息総額でなく、売却時の残債と手残りで考える。原則35年で組み、短縮は繰上返済で行う
3. 「借りられる額」（年収7〜8倍）と「借りるべき額」（ライフプランから逆算）を混同しない
4. 賃貸と購入の比較は、購入側に売却後の手残り、賃貸側に賃料上昇・更新料・引っ越し費用を入れる
5. 市況の予測とエリアの優劣は断言しない

準備ができたら「前提を読み込みました。何年くらい住む予定ですか？」から会話を始めてください。
"""

def main() -> None:
    q = urllib.parse.quote(PROMPT, safe="")
    for name, base in [("ChatGPT", "https://chatgpt.com/?q="), ("Claude", "https://claude.ai/new?q=")]:
        url = base + q
        print(f"[{name}] {len(url)}文字\n{url}\n")

if __name__ == "__main__":
    main()
