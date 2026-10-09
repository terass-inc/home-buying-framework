"""相談の診断レポート。分かっていることから、関係する計算をまとめて実行し、1つのレポートにする。

一問一答ではなく「今わかっている前提で、全体はこう見える。ここが分かると結論がこう動く」を返すのが目的。
未確認の前提は仮置きして、仮置きであることを必ず明示する。居住年数が分からないうちは結論を出さず、
年数別の表を返す（AGENTS.md ルール1）。
"""
from __future__ import annotations

import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from consult import FIELD_INDEX, FIELDS, SURFACE_REASONS, Facts  # noqa: E402
from paths import ROOT  # noqa: E402

try:  # リポジトリから起動したとき
    from scripts.rentbuy import advice, inputs_from_assumptions, load_assumptions, simulate
except ImportError:  # パッケージ（uvx など）から起動したとき
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from rentbuy import advice, inputs_from_assumptions, load_assumptions, simulate  # type: ignore

DISCLAIMER = ("この試算は一般的な前提に基づく概算です。金利・税制は変わるため、最終的な判断は"
              "金融機関やファイナンシャルプランナーなどの専門家と行ってください。")
DISCLOSURE = "この前提は不動産仲介会社の株式会社TERASSが作成したもので、不動産事業者の立場から書かれている。"
PROVISIONAL_RENT_PCT = 3.8          # 比較賃料が不明なときの仮置き（物件価格の年3.8%）
YEARS_GRID = (3, 5, 7, 10, 15, 20)
HORIZON_YEARS = 50            # 受け付ける居住年数の上限（server.FactsInput の stay_years）と合わせる
SCENARIOS = {"low": "低（物価0%・金利1.0%）", "medium": "中（物価1.0%・金利1.5%）", "high": "高（物価2.0%・金利2.5%）"}


def _man(yen: float) -> int:
    return round(yen / 10_000)


def _assumptions():
    return load_assumptions(ROOT / "assumptions.yaml")


def _premises(f: Facts) -> tuple[dict, list[str]]:
    """計算に使う前提と、その出どころ（利用者／仮置き）。"""
    prem, provisional = {}, []
    prem["物件価格"] = (f"{_man(f.property_price_yen):,}万円", "利用者") if f.property_price_yen else None
    if f.comparable_rent_yen:
        prem["比較賃料（同等物件の相場）"] = (f"月{f.comparable_rent_yen / 10_000:.1f}万円", "利用者")
    elif f.property_price_yen:
        r = f.property_price_yen * PROVISIONAL_RENT_PCT / 100 / 12
        prem["比較賃料（同等物件の相場）"] = (f"月{r / 10_000:.1f}万円", f"仮置き（物件価格の年{PROVISIONAL_RENT_PCT}%）")
        provisional.append("比較賃料")
    if f.stay_years:
        prem["居住年数"] = (f"{f.stay_years:g}年", "利用者")
    if f.interest_rate_pct:
        prem["金利"] = (f"{f.interest_rate_pct}%（{f.rate_type or '固定と仮定'}）", "利用者")
    else:
        prem["金利"] = ("シナリオのセットで置く（低1.0%・中1.5%・高2.5%）", "標準の前提")
    return {k: v for k, v in prem.items() if v}, provisional


def _rent(f: Facts) -> Optional[float]:
    if f.comparable_rent_yen:
        return float(f.comparable_rent_yen)
    if f.property_price_yen:
        return f.property_price_yen * PROVISIONAL_RENT_PCT / 100 / 12
    return None


def _sim(a, f: Facts, scenario: str, **over):
    kw = dict(price_yen=f.property_price_yen, rent_yen_per_month=_rent(f), horizon_years=HORIZON_YEARS)
    if f.interest_rate_pct and scenario == "medium":
        kw["interest_rate_pct"] = f.interest_rate_pct
    kw.update(over)
    return simulate(inputs_from_assumptions(a, scenario=scenario, **kw))


def _rent_threshold(a, f: Facts, year: int) -> Optional[float]:
    """中セットで、year 年目に購入と賃貸が並ぶ比較賃料（これを下回ると賃貸が有利）。"""
    lo, hi = 30_000.0, 2_000_000.0
    def gap(r):  # 負なら購入が有利
        return _sim(a, f, "medium", rent_yen_per_month=r).row(year).buy_minus_rent
    if gap(lo) < 0 or gap(hi) > 0:
        return None
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if gap(mid) > 0 else (lo, mid)
    return hi


def _rent_vs_buy(a, f: Facts) -> Optional[dict]:
    if not f.property_price_yen:
        return None
    sims = {sc: _sim(a, f, sc) for sc in SCENARIOS}
    pt0 = _sim(a, f, "medium", price_pass_through_pct=0.0)
    out = {
        "損益分岐年": {SCENARIOS[sc]: s.breakeven_year for sc, s in sims.items()}
        | {"中・物価が価格に乗らない（波及率0%）": pt0.breakeven_year},
        "毎月のローン返済（中セット）": f"{sims['medium'].monthly_payment / 10_000:.1f}万円",
    }
    if f.stay_years:
        y = int(f.stay_years)
        out[f"{y}年住んだ場合（購入−賃貸。負なら購入が有利）"] = {
            SCENARIOS[sc]: f"{_man(s.row(y).buy_minus_rent):+,}万円" for sc, s in sims.items()
        } | {"中・波及率0%": f"{_man(pt0.row(y).buy_minus_rent):+,}万円"}
        r = sims["medium"].row(y)
        out[f"{y}年目に売った場合の手残り（中セット）"] = {
            "物件価値": f"{_man(r.property_value):,}万円", "ローン残債": f"{_man(r.loan_balance):,}万円",
            "売却諸費用": f"{_man(r.selling_cost):,}万円", "手残り": f"{_man(r.equity_on_sale):,}万円"}
    else:
        out["居住年数別（中セット、購入−賃貸。負なら購入が有利）"] = {
            f"{y}年": f"{_man(sims['medium'].row(y).buy_minus_rent):+,}万円" for y in YEARS_GRID}
    return out


def _conditions(a, f: Facts, provisional: list[str]) -> list[str]:
    """結論が変わる条件。"""
    out = []
    if not f.property_price_yen:
        return out
    med = _sim(a, f, "medium")
    if med.breakeven_year:
        out.append(f"居住年数が{med.breakeven_year}年未満なら賃貸のほうが有利（中セット）。"
                   f"{'今の予定（' + format(f.stay_years, 'g') + '年）はこれを' + ('上回る' if f.stay_years >= med.breakeven_year else '下回る') + '。' if f.stay_years else ''}")
    pt0 = _sim(a, f, "medium", price_pass_through_pct=0.0)
    if pt0.breakeven_year != med.breakeven_year:
        out.append(f"物価が物件価格に乗らないエリアなら、分岐は{med.breakeven_year}年目→{pt0.breakeven_year}年目にずれる")
    if f.stay_years:
        th = _rent_threshold(a, f, int(f.stay_years))
        if th:
            label = "仮置きの" if "比較賃料" in provisional else ""
            out.append(f"{format(f.stay_years, 'g')}年で見ると、同等物件の家賃が月{th / 10_000:.1f}万円を下回るなら賃貸が有利"
                       f"（{label}比較賃料は月{_rent(f) / 10_000:.1f}万円）")
    if f.rate_type in ("variable", "変動"):
        out.append("変動金利なので「返済額は固定、賃料は上がる」の前提が成り立たない。金利が上がれば分岐は後ろにずれる")
    return out


def _next_to_learn(f: Facts, provisional: list[str]) -> Optional[dict]:
    """次に分かると精度が上がること（1つだけ）。"""
    order = ["purpose", "stay_years", "property_price_yen", "comparable_rent_yen", "household_income_yen",
             "savings_yen", "rate_type", "rent_moves_known", "family_plan_known"]
    if f.known("purpose") and any(s in (f.purpose or "") for s in SURFACE_REASONS) and not f.purpose_sentence_confirmed:
        order.insert(1, "purpose_sentence_confirmed")
    for k in order:
        if not f.known(k):
            q, why, _ = FIELD_INDEX[k]
            extra = "（今は仮置き）" if k == "comparable_rent_yen" and "比較賃料" in provisional else ""
            return {"field": k, "question": q, "why": why + extra}
    return None


def diagnose(f: Facts) -> dict:
    a = _assumptions()
    premises, provisional = _premises(f)
    sections: dict = {}
    if (rv := _rent_vs_buy(a, f)):
        sections["賃貸か購入か"] = rv
    rate = f.interest_rate_pct or inputs_from_assumptions(a, scenario="medium").interest_rate_pct
    if f.property_price_yen:
        lt = advice.loan_term_comparison(f.property_price_yen, annual_rate_pct=rate,
                                         # 35年を超えて住むなら完済後なので、売却時点の比較はしない
                                         sale_year=int(f.stay_years) if f.stay_years and f.stay_years <= 35 else None,
                                         annual_income_yen=f.household_income_yen, assumptions=a)
        sections["ローン期間（35年と20年）"] = {"要約": lt.summary, "注意": lt.notes[:3]}
    if f.property_price_yen and f.current_rent_yen:
        wc = advice.wait_cost(f.property_price_yen, f.current_rent_yen, assumptions=a)
        sections["待つコスト"] = {"要約": wc.summary}
    if f.household_income_yen:
        bb = advice.borrowing_budget(f.household_income_yen, property_price_yen=f.property_price_yen, assumptions=a)
        names = {"affordable_monthly_payment_yen": "毎月の返済に回せる上限", "stress_rate_pct": "悲観シナリオで置く金利"}
        sections["借りられる額と借りるべき額"] = {
            "要約": bb.summary.replace("inputs_needed の値", "下の項目"),
            "借りるべき額のために確認すること": [names.get(x["name"], x["name"]) for x in bb.inputs_needed]}
    if f.property_price_yen:
        kw = {"savings_yen": f.savings_yen, "securities_yen": 0, "other_assets_yen": 0} if f.savings_yen else {}
        bs = advice.balance_sheet(property_price_yen=f.property_price_yen, term_years=35, annual_rate_pct=rate,
                                  assumptions=a, **kw).to_dict()["figures"]
        v = lambda k: _man(bs[k]["value"])
        sections["購入直後の純資産"] = {
            "要約": f"純資産は {v('now.net_worth_yen'):,}万円 → 購入直後 {v('after_purchase.net_worth_yen'):,}万円。"
                   f"諸費用の分だけ減る（損ではなく、掛け捨ての家賃を資産に変える入口の費用）。",
            "出どころ": "利用者の現預金" if f.savings_yen else "仮置き（calc/balance-sheet.md の標準：資産600万円）",
            "購入後の手元の現金": f"{v('after_purchase.cash_yen'):,}万円"}

    blocked = []
    if not f.known("purpose"):
        blocked.append("なぜ買うのかが未確認。数字は判断材料であって、買う・買わないの結論ではない")
    if not f.stay_years:
        blocked.append("居住年数が未確認なので、ローン期間・賃貸か購入かの結論は出さない（年数別の表を見る）")
    report = {
        "前提": {k: {"値": v, "出どころ": s} for k, (v, s) in premises.items()},
        "仮置きしている前提": provisional,
        "診断": sections,
        "結論が変わる条件": _conditions(a, f, provisional),
        "まだ出せない結論": blocked,
        "次に分かると精度が上がること": _next_to_learn(f, provisional),
        "回答のときに守ること": [
            "次に聞く質問は「次に分かると精度が上がること」の1つだけにする",
            "仮置きの前提は、仮置きだと明示する",
            DISCLOSURE,
            DISCLAIMER,
        ],
    }
    if not sections:
        report["診断"] = {"まだ計算できない": "物件価格（または検討している価格帯）が分かると、賃貸か購入か・ローン期間の試算を出せる"}
    return report


KEY_METRICS = ("損益分岐年（中）", "居住年数時点の購入−賃貸（中）", "毎月のローン返済（中）")


def _metrics(f: Facts) -> dict:
    if not f.property_price_yen:
        return {}
    a = _assumptions()
    med = _sim(a, f, "medium")
    m = {"損益分岐年（中）": med.breakeven_year, "毎月のローン返済（中）": round(med.monthly_payment)}
    if f.stay_years:
        m["居住年数時点の購入−賃貸（中）"] = _man(med.row(int(f.stay_years)).buy_minus_rent)
    return m


def what_if(f: Facts, changes: dict) -> dict:
    g = replace(f, **changes)
    before, after = _metrics(f), _metrics(g)
    diff = {k: {"今": before.get(k), "変更後": after.get(k)} for k in KEY_METRICS if k in before or k in after}
    return {"変更": changes, "主な数字の変化": diff, "変更後の結論が変わる条件": _conditions(_assumptions(), g, _premises(g)[1]),
            "注意": "この変更は保存していない。採用するなら update_facts で反映する。" + DISCLAIMER}


def to_markdown(report: dict) -> str:
    lines = ["# 住宅購入の診断（前提つきの試算）", ""]
    lines += ["## 前提"] + [f"- {k}: {v['値']}（{v['出どころ']}）" for k, v in report["前提"].items()] + [""]
    for name, body in report["診断"].items():
        lines.append(f"## {name}")
        if isinstance(body, dict):
            for k, v in body.items():
                if isinstance(v, dict):
                    lines.append(f"- {k}: " + "、".join(f"{kk} {vv}" for kk, vv in v.items()))
                elif isinstance(v, list):
                    lines += [f"- {k}:"] + [f"  - {x}" for x in v]
                else:
                    lines.append(f"- {k}: {v}")
        else:
            lines.append(str(body))
        lines.append("")
    if report["結論が変わる条件"]:
        lines += ["## 結論が変わる条件"] + [f"- {x}" for x in report["結論が変わる条件"]] + [""]
    if report["まだ出せない結論"]:
        lines += ["## まだ出せない結論"] + [f"- {x}" for x in report["まだ出せない結論"]] + [""]
    if (n := report["次に分かると精度が上がること"]):
        lines += ["## 次に分かると精度が上がること", f"- {n['question']}（{n['why']}）", ""]
    lines += [f"※{DISCLAIMER}", f"※{DISCLOSURE}"]
    return "\n".join(lines)
