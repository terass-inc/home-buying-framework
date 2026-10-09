# rentbuy: 賃貸 vs 購入の比較計算エンジン

`calc/rent-vs-buy.md` の計算手順を Python で実装したもの。文章に書かれた分岐年や金額を、テストで再現して保証する。標準ライブラリのみ（Python 3.10 以上）。

**文章とコードが食い違ったら文章が正。** 文章が一意に決めていない箇所は、コード中に【解釈】コメントを付けている。

## 使い方

リポジトリ直下で実行する。金額の入力と表の表示は万円、`--json` の出力は円。

```sh
python3 -m scripts.rentbuy                                  # 中セット・経年減価 中で40年分の表
python3 -m scripts.rentbuy --scenario low --stay 10         # 低セット、10年住む場合の内訳も表示
python3 -m scripts.rentbuy --scenario high --pass-through 0 # 高セットで物価が価格に波及しない場合
python3 -m scripts.rentbuy --price 6000 --rent 20 --move 8:22   # 利用者の値で上書き（住み替えは8年後に22万円の1回）
python3 -m scripts.rentbuy --summary                        # 3セット × 経年減価3つ ＋ 2つの感度の分岐年一覧
python3 -m scripts.rentbuy --json                           # 機械可読
python3 -m scripts.rentbuy --help                           # 全オプション
```

Python から使う場合:

```python
from scripts.rentbuy import load_assumptions, inputs_from_assumptions, simulate
a = load_assumptions()                       # assumptions.yaml を読む。壊れていれば AssumptionsError
inp = inputs_from_assumptions(a, scenario="high", price_pass_through_pct=0)
res = simulate(inp)
res.breakeven_year      # 14
res.row(10).buy_net     # 10年目の購入の純負担（円）
```

## 入力と出力

- 入力 `RentBuyInputs`（dataclass）: 比較賃料 R₁、住み替え (n₂, R₂)・(n₃, R₃)、物件価格 P、借入額 L、期間 T、金利 i、物価上昇率 π、波及率 α、賃料・管理費・修繕積立金の上昇率、経年減価 d、購入諸費用率、住宅ローン控除（率・限度額・年数）、リフォーム、更新料・初期費用の月数、売却諸費用（式／率3.5%／なし）、団信相当の保険料（あり／なし）、家賃補助。既定値は `inputs_from_assumptions()` が `assumptions.yaml` から入れる。`scenario="low"|"medium"|"high"` で `inflation.scenarios` のセットをまとめて入れ、`None` なら「入力」節の単独の既定値を使う。
- 出力 `RentBuyResult`: 年ごとの `YearRow`（賃貸側の賃料・更新料・初期費用・団信相当・累計、購入側の返済・管理費・修繕積立金・固定資産税・控除・リフォーム・累計キャッシュアウト、物件価値・残債・売却諸費用・手残り・純負担・差額）と、損益分岐年 `breakeven_year`（購入の純負担が賃貸の累計を下回る最初の年。範囲内になければ `None`）。

## 文章との対応

| 文章の節 | コード |
| --- | --- |
| calc/rent-vs-buy.md「入力」 | `model.RentBuyInputs`、`model.inputs_from_assumptions` |
| 同「金利・物価・賃料・価格はセットで置く」 | `assumptions.ScenarioSet`、`inputs_from_assumptions(scenario=...)` |
| 同「賃貸側の計算」 | `model.simulate`（賃貸側）、`_rent_base`、`_is_renewal_year` |
| 同「購入側の計算」 | `model.simulate`（購入側）、`loan.py`、`model.selling_costs` |
| 同「出力の形式」 | `cli.py`（表、`--summary`） |
| 同「検算用の標準ケース」 | `tests/test_rentbuy.py` の `TestStandardCaseBreakeven` |
| assumptions.yaml | `assumptions.py`（必要なキーだけ読む）、`yamlmini.py`（最小限の YAML パーサー） |

## 実装上の解釈（文章が一意に決めていない箇所）

1. 賃料は文章どおり `R(k) = R_base(k) × (1 + g)^k`（1年目から上昇済み）。購入側の保有コストは `(k−1)` 乗なので起点が1年ずれるが、文章に従った。
2. 更新料は「偶数年」の字面どおり k = 2, 4, 6, … 年目。住み替えの年（20年目）と重なっても払う。
3. 賃貸の初期費用は住み替えの年だけ。1年目には立てない（今の賃貸から出発する想定）。
4. リフォームは 10・20・30 年目。
5. 残債は月次の元利均等で 12N カ月返済後の残高（文章の `Σ PPMT(i, k, T, L)` は年単位の記法）。
6. 400万円以下の売却価格の仲介手数料は、法定の段階料率（200万円以下5%、200万〜400万円 4%+2万円）。
7. 住宅ローン控除は `min(年末残高, 限度額) × 0.7%` を控除年数のあいだ。名目で固定。
8. `inflation_pct` だけを上書きしたときは、修繕積立金の上昇率を「2.0% + π」に連動させる。

2・3・1 の別解は `renewal_schedule="since_move"`、`initial_cost_at_start=True`、`rent_growth_from_year=0` で確かめられる（分岐年が1年前後動くケースがある）。

## テスト

```sh
python3 -m unittest discover -s scripts/tests -v
```

文章の値を再現できなかったものは `@unittest.expectedFailure` を付け、docstring に差と原因の仮説を書いている。文章を直したら、そのテストの `expectedFailure` を外す。
