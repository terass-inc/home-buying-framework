# MCP サーバー

住宅購入AIフレームワークを、Claude Code・Claude Desktop などの MCP 対応アプリから使うためのサーバーです。

一問一答の道具ではなく、**相談の状態をサーバーが覚え、分かったことが増えるたびに、関係する計算をまとめた診断レポートを返します**。前提を変えた「もしも」も、保存せずに試せます。計算は `scripts/rentbuy`（`calc/` と `principles/` の手順の実装。文章に書いた数字をテストで再現済み）を使うので、AIが暗算で間違えることはありません。

## インストール

[uv](https://docs.astral.sh/uv/) があれば、リポジトリを取得しなくても1行で登録できます（文章と数値前提はパッケージに同梱しています）。

```bash
# Claude Code
claude mcp add home-buying-framework -- uvx --from git+https://github.com/terass-inc/home-buying-framework hbf-mcp
```

Claude Desktop では、設定ファイル（`claude_desktop_config.json`）に次を追加します。

```json
{
  "mcpServers": {
    "home-buying-framework": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/terass-inc/home-buying-framework", "hbf-mcp"]
    }
  }
}
```

### URL で追加する（公開後）

サーバーを URL で公開すると、Claude（設定 → コネクタ → カスタムコネクタを追加）、Claude Code（`claude mcp add --transport http home-buying-framework https://<公開URL>/mcp`）、ChatGPT（開発者モードのコネクタ）に、URL を入れるだけで追加できます。

公開用の起動方法は次のとおりです。相談の状態は相談IDそのものに詰めているので、サーバーは何も保存せず、サーバーレスや複数台構成でもそのまま動きます。

```bash
HBF_ALLOWED_HOSTS=mcp.example.com hbf-mcp --http --host 0.0.0.0 --port 8080   # 待ち受けは /mcp
docker build -t hbf-mcp . && docker run -e HBF_ALLOWED_HOSTS=mcp.example.com -p 8080:8080 hbf-mcp
```

`HBF_ALLOWED_HOSTS` には公開するドメインを入れます（DNS リバインディング対策。指定しないとローカルからの接続だけを受け付けます）。

リポジトリを取得済みなら `uv run --with "mcp>=2,<3" python scripts/hbf_mcp/server.py` でも起動できます。

## 相談の流れ

```
start_consultation(facts={物件価格: 5,000万円})
  → 診断レポート：年数別の賃貸 vs 購入、ローン期間、結論が変わる条件
    「居住年数が未確認なので結論は出さない」「次に分かると精度が上がること：きっかけ」
update_facts(きっかけ、居住年数10年、同等物件の家賃16万円 をまとめて)
  → 診断レポートを更新：10年目の差、手残り、損益分岐年（低・中・高・波及率0%）
what_if(居住年数=4年)
  → 主な数字の変化（購入が有利 → 賃貸が有利）。保存はしない
self_check(topic=rent_vs_buy, shows_numbers=true)
  → 回答を送る前に照らす NG 判定と、必ず添える免責文
```

診断レポートの中身：

| 節 | 内容 |
|---|---|
| 前提 | 使った値と出どころ（利用者／仮置き／標準の前提）。仮置きは必ず明示 |
| 賃貸か購入か | 損益分岐年（低・中・高セット、物価が価格に乗らない場合）、居住年数時点の差と売却時の手残り。年数が未確認なら年数別の表 |
| ローン期間 | 35年と20年の毎月返済、売却時点での正味の差、差額の使い道 |
| 待つコスト | 待って得をするための下落率と、過去の下落実績との比較 |
| 借りられる額と借りるべき額 | 銀行の上限と、借りるべき額のために確認すべきこと |
| 購入直後の純資産 | 諸費用で減る分と、手元に残る現金 |
| 結論が変わる条件 | 「居住年数が○年未満なら賃貸が有利」「家賃が月○万円を下回るなら賃貸が有利」など |
| 次に分かると精度が上がること | 次に聞く質問を1つだけ |

## 道具の一覧

| 種類 | 名前 |
|---|---|
| 相談 | `start_consultation`・`update_facts`・`diagnose`・`what_if` |
| ガードレール | `next_step`（次に聞く質問を1つ）・`self_check`（送る前に照らす NG 判定） |
| 計算 | `compare_rent_vs_buy`・`loan_term_comparison`・`loan_schedule`・`wait_cost`・`borrowing_budget`・`balance_sheet`・`selling_costs_breakdown` |
| 参照 | `get_framework`・`list_principles`・`get_principle`・`get_assumptions` |

ほかに、リソース（`framework://lite`、`framework://assumptions`、`framework://principles/{id}`、`framework://cases/{id}`）と、プロンプト（`home_buying_consultation`、`rent_vs_buy_check`、`loan_term_check`）があります。参照と計算の道具には読み取り専用の注釈を付けています。

相談の状態（分かっている事実）は相談IDに詰めて AI とやりとりし、サーバーには保存しません。

## テスト

```bash
pip install "mcp>=2,<3"
python3 -m unittest discover -s scripts/tests
```
