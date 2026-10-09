# MCP サーバー

住宅購入AIフレームワークを、Claude Desktop などの MCP 対応アプリから「道具」として使えるようにするサーバーです。URL を貼る方式と違い、AIが必要なときに原則・数値前提・計算エンジンを呼び出します。計算は `scripts/rentbuy`（`calc/rent-vs-buy.md` の実装。文章の数値をテストで再現済み）をそのまま使うので、AIが暗算で間違えることがありません。

## 道具（ツール）

| 名前 | 内容 |
|---|---|
| `get_framework` | AIへの前提の全文。相談に答える前に読む |
| `list_principles` / `get_principle` | 話題別の原則の一覧と本文 |
| `get_assumptions` | 数値前提（`assumptions.yaml`）。節ごとに取得できる |
| `compare_rent_vs_buy` | 賃貸か購入かの比較。居住年数時点の純負担、損益分岐年、低・中・高セットと波及率0%の感度 |
| `loan_schedule` | 元利均等の毎月返済額、売却時点の残債と、それまでの利息 |

ほかに、前提の全文をリソース `framework://lite` とプロンプト `home_buying_consultation` でも提供します。

## 使い方

[uv](https://docs.astral.sh/uv/) が入っていれば、リポジトリを取得して次で起動できます。

```bash
git clone https://github.com/terass-inc/home-buying-framework.git
cd home-buying-framework
uv run --with "mcp>=2,<3" python scripts/hbf_mcp/server.py
```

Claude Desktop では、設定ファイル（`claude_desktop_config.json`）に次を追加します。パスは取得した場所に置き換えてください。

```json
{
  "mcpServers": {
    "home-buying-framework": {
      "command": "uv",
      "args": ["run", "--with", "mcp>=2,<3", "python", "/path/to/home-buying-framework/scripts/hbf_mcp/server.py"]
    }
  }
}
```

Claude Code では次のとおりです。

```bash
claude mcp add home-buying-framework -- uv run --with "mcp>=2,<3" python /path/to/home-buying-framework/scripts/hbf_mcp/server.py
```

## テスト

```bash
pip install "mcp>=2,<3"
python3 -m unittest discover -s scripts/tests
```

`mcp` が入っていない環境では、MCP のテストだけ飛ばします。
