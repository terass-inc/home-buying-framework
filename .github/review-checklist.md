# 数値前提の定期見直し

3カ月ごとの定期見直しです。`assumptions.yaml` の数値が現状と合っているかを確認してください。
確認できた項目は、値を更新するか、変更がなければ確認日だけ更新してください。

## 金利

- [ ] `inflation.scenarios` の `medium` / `high` の `interest_rate_pct`（現在 1.5% / 2.5%）を実勢に合わせる
- [ ] `interest_rate.screening_rate_pct`（審査金利、現在 3.0%）
- [ ] `interest_rate.simulation_default_pct`（1.0%）は**書籍の前提なので変更しない**。実勢金利との2本立てで示す運用（`must_show_two_rates`）が守られているかだけ確認する
- [ ] 金利は2箇所（`interest_rate` と `inflation.scenarios`）にある。**片方だけ直していないか**を必ず確認する

## 税制・補助金（年度で変わる。期限に注意）

- [ ] `tax_and_programs.mortgage_deduction`（住宅ローン控除の控除率・期間・借入限度額・所得要件）
- [ ] `tax_and_programs.gift_tax_exemption`（住宅取得等資金の贈与非課税。**期限は2026年12月31日までの贈与**。延長の有無を確認する）
- [ ] `tax_and_programs.subsidies`（補助事業は年度で名称も要件も変わり、**予算上限に達すると年度内でも受付終了**する。公式サイトで受付状況を確認する）
- [ ] 更新したら `tax_and_programs.checked_at` を書き換える

## 賃料・物価

- [ ] `rent.growth_pct_per_year` と `growth_note`（消費者物価指数の家賃、都市部の募集賃料の動向）
- [ ] `rent.comparable_rent_ratio_pct_of_price_per_year`（比較賃料の仮置き 年3.8%）
- [ ] `inflation.simulation_default_pct` と `price_pass_through_pct`

## 費用・保険

- [ ] `selling_costs.brokerage_fee_formula`（**仲介手数料の上限規定と消費税率が変わったら要修正**）
- [ ] `purchase_costs` の各率（新築マンション 4.5% / 中古・戸建て 7.5%）
- [ ] `danshin.equivalent_premium_yen_per_month`（団信相当の保険料 月3,000円）
- [ ] `loan.origination_fee_pct`（2.2%）と `low_fee_type_rate_premium_pct`（0.25%）
- [ ] `ownership_costs` の管理費・修繕積立金・固定資産税

## 相場・その他

- [ ] `depreciation` の下落率（マンション 年1.5% / 戸建て 年1.4%）
- [ ] `loan.default_term_years`（**40年・50年ローンの普及で「最長＝35年」の前提が変わりつつある**）
- [ ] `principles/` 各ファイルの「見直し要」セクション（13ファイルにあり）

## 作業後の手順

- [ ] `assumptions.yaml` の `updated_at` を更新する
- [ ] `python3 scripts/build_lite.py` を実行して `dist/lite.md` と `README.md` を再生成する（忘れると CI が失敗する）
- [ ] `dist/lite.md` が上限 6,500字を超えていないことを確認する
- [ ] `cases/` のテストケースで回答が崩れないか確認する

---

この Issue は `.github/workflows/review-reminder.yml` が3カ月ごとに自動で作成しています。
本文を直したいときは `.github/review-checklist.md` を編集してください。
未対応のものが残っているあいだは重複して立ちません。対応が終わったらクローズしてください。
