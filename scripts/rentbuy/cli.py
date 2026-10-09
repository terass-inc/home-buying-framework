"""コマンドライン: python3 -m scripts.rentbuy [オプション]

金額の入力は万円（--price 5000 は 5,000万円、--rent 16 は月16万円）。
表の金額も万円。--json の出力は円（丸めなし）。
"""
from __future__ import annotations

import argparse
import json
import sys
from .assumptions import AssumptionsError, load_assumptions
from .model import MAN, RentMove, inputs_from_assumptions, simulate

__all__ = ["main", "build_parser"]


def _move(text: str) -> RentMove:
    try:
        year, rent = text.split(":")
        return RentMove(int(year), float(rent) * MAN)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--move は '年:月額万円'（例 5:18）で指定する: {text!r}") from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m scripts.rentbuy",
        description="賃貸 vs 購入の比較（calc/rent-vs-buy.md の手順）。金額の入力・表示は万円。",
    )
    p.add_argument("--assumptions", help="assumptions.yaml のパス（既定はリポジトリ直下）")
    p.add_argument("--scenario", choices=["low", "medium", "high", "none"], default="medium",
                   help="金利・物価・賃料・保有コストのセット（none は「入力」節の単独の既定値）。既定 medium")
    p.add_argument("--depreciation", choices=["small", "medium", "large"], default="medium",
                   help="経年減価 小1%%/中1.5%%/大4%%。既定 medium")
    p.add_argument("--years", type=int, default=40, help="表に出す年数（既定 40）")
    p.add_argument("--stay", type=int, help="居住年数 N。指定するとその年の内訳を強調して出す")
    p.add_argument("--price", type=float, help="物件価格（万円）。既定 5000")
    p.add_argument("--loan", type=float, help="借入額（万円）。既定は物件価格（フルローン）")
    p.add_argument("--rent", type=float, help="比較賃料 R₁（月・万円）。既定 16")
    p.add_argument("--rate", type=float, help="金利（%%）。指定するとセットの金利を上書き（セット内の単独入れ替えは文章が禁じているので注意）")
    p.add_argument("--term", type=int, help="借入期間（年）。既定 35")
    p.add_argument("--inflation", type=float, help="物価上昇率 π（%%）")
    p.add_argument("--pass-through", type=float, help="波及率 α（%%）。既定 100")
    p.add_argument("--rent-growth", type=float, help="賃料上昇率（%%）")
    p.add_argument("--mgmt-growth", type=float, help="管理費の上昇率（%%）")
    p.add_argument("--repair-growth", type=float, help="修繕積立金の上昇率（%%）")
    p.add_argument("--move", type=_move, action="append", metavar="年:万円",
                   help="住み替え（例 --move 5:18 --move 20:14）。指定すると既定の住み替え例を置き換える")
    p.add_argument("--no-moves", action="store_true", help="住み替えなし")
    p.add_argument("--no-selling-costs", action="store_true", help="売却諸費用を引かない（検算用）")
    p.add_argument("--selling-cost-rate", action="store_true", help="売却諸費用を式ではなく率 3.5%% で概算")
    p.add_argument("--no-danshin", action="store_true", help="団信相当の保険料を賃貸側に立てない（検算用）")
    p.add_argument("--danshin", type=float, help="団信相当の保険料（月・円）。既定 3000")
    p.add_argument("--subsidy", type=float, default=0.0, help="家賃補助・経費化で賃貸側から引く額（月・万円）")
    p.add_argument("--deduction-years", type=int, help="住宅ローン控除の年数。既定 10")
    p.add_argument("--deduction-limit", type=float, help="住宅ローン控除の借入限度額（万円）。既定 2000")
    p.add_argument("--summary", action="store_true",
                   help="「出力の形式」節の一覧（低・中・高 × 経年減価 小・中・大の分岐年と、波及率0%%・賃料0.5%%の感度）を出す")
    p.add_argument("--json", action="store_true", help="機械可読な JSON で出力（金額は円）")
    return p


def _inputs(args, a):
    ov = {"horizon_years": max(args.years, args.stay or 0)}
    pairs = [
        ("price", "price_yen", MAN), ("loan", "loan_amount_yen", MAN), ("rent", "rent_yen_per_month", MAN),
        ("rate", "interest_rate_pct", 1), ("term", "loan_term_years", 1), ("inflation", "inflation_pct", 1),
        ("pass_through", "price_pass_through_pct", 1), ("rent_growth", "rent_growth_pct", 1),
        ("mgmt_growth", "management_fee_growth_pct", 1), ("repair_growth", "repair_reserve_growth_pct", 1),
        ("danshin", "danshin_premium_yen_per_month", 1), ("deduction_years", "deduction_years", 1),
        ("deduction_limit", "deduction_limit_yen", MAN),
    ]
    for arg, field_, unit in pairs:
        val = getattr(args, arg)
        if val is not None:
            ov[field_] = val * unit if unit != 1 else val
    if args.no_moves:
        ov["moves"] = ()
    elif args.move:
        ov["moves"] = tuple(sorted(args.move, key=lambda m: m.after_years))
    if args.no_selling_costs:
        ov["include_selling_costs"] = False
    if args.selling_cost_rate:
        ov["selling_cost_method"] = "rate"
    if args.no_danshin:
        ov["include_danshin"] = False
    if args.subsidy:
        ov["rent_subsidy_yen_per_month"] = args.subsidy * MAN
    scenario = None if args.scenario == "none" else args.scenario
    return inputs_from_assumptions(a, scenario=scenario, depreciation=args.depreciation, **ov)


def _m(x: float) -> str:
    return f"{x / MAN:,.0f}"


def _print_table(res, stay, out) -> None:
    inp = res.inputs
    print(f"物件 {_m(inp.price_yen)}万円 / 借入 {_m(inp.loan_principal)}万円・{inp.loan_term_years}年・金利 {inp.interest_rate_pct}%"
          f"（月返済 {res.monthly_payment / MAN:,.2f}万円）/ 購入諸費用 {_m(res.purchase_cost)}万円", file=out)
    print(f"物価 {inp.inflation_pct}% / 波及率 {inp.price_pass_through_pct}% / 経年減価 {inp.depreciation_pct}% / "
          f"賃料上昇 {inp.rent_growth_pct}% / 管理費上昇 {inp.management_fee_growth_pct}% / 修繕積立金上昇 {inp.repair_reserve_growth_pct}%", file=out)
    moves = "、".join(f"{m.after_years}年目から{m.rent_yen_per_month / MAN:g}万円" for m in inp.moves) or "なし"
    print(f"賃料 R₁ {inp.rent_yen_per_month / MAN:g}万円 / 住み替え {moves} / 売却諸費用 "
          f"{'あり（' + ('式' if inp.selling_cost_method == 'formula' else '率') + '）' if inp.include_selling_costs else 'なし'}"
          f" / 団信相当 {'あり' if inp.include_danshin else 'なし'}", file=out)
    print("（金額は万円・名目。割引なし）", file=out)
    head = f"{'年':>3} {'賃料/月':>7} {'賃貸累計':>8} {'購入CF累計':>9} {'物件価値':>8} {'残債':>7} {'売却諸費用':>8} {'手残り':>7} {'購入純負担':>9} {'差(購入-賃貸)':>11}"
    print(head, file=out)
    for r in res.rows:
        mark = " *" if r.year == res.breakeven_year else ("  <- N" if stay == r.year else "")
        print(f"{r.year:>3} {r.rent_monthly / MAN:>8.2f} {_m(r.rent_cum):>9} {_m(r.cashout_cum):>11} {_m(r.property_value):>10}"
              f" {_m(r.loan_balance):>8} {_m(r.selling_cost):>10} {_m(r.equity_on_sale):>8} {_m(r.buy_net):>11} {_m(r.buy_minus_rent):>14}{mark}",
              file=out)
    be = res.breakeven_year
    print(f"\n賃貸より得になる年（購入の純負担が賃貸の累計を下回る最初の年）: "
          f"{f'{be}年目' if be else f'{len(res.rows)}年以内には来ない'}  （* 印）", file=out)
    if stay:
        r = res.row(stay)
        print(f"N = {stay}年: 購入の純負担 {_m(r.buy_net)}万円 / 賃貸の累計 {_m(r.rent_cum)}万円 / 差額 {_m(r.buy_minus_rent)}万円", file=out)
    print("※この試算は一般的な前提に基づく概算で、将来の利益や資産を保証しません。"
          "物価・賃料・金利の上昇率は予測ではなく前提です。", file=out)


def summary(args, a) -> list:
    """3セット × 経年減価3つの分岐年と、文章が必ず並べるよう求める2つの感度。"""
    rows = []
    for sc in ("low", "medium", "high"):
        for dep in ("small", "medium", "large"):
            args.scenario, args.depreciation = sc, dep
            res = simulate(_inputs(args, a))
            r = res.row(args.stay) if args.stay else None
            rows.append({"scenario": sc, "depreciation": dep, "sensitivity": None,
                         "breakeven_year": res.breakeven_year,
                         "stay_buy_net": r.buy_net if r else None, "stay_rent_cum": r.rent_cum if r else None})
        for label, ov in (("pass_through_0", {"pass_through": 0.0}), ("rent_growth_0.5", {"rent_growth": 0.5}),
                          ("pass_through_0_and_rent_growth_0.5", {"pass_through": 0.0, "rent_growth": 0.5})):
            args.scenario, args.depreciation = sc, "medium"
            saved = {k: getattr(args, k) for k in ov}
            for k, v in ov.items():
                setattr(args, k, v)
            res = simulate(_inputs(args, a))
            for k, v in saved.items():
                setattr(args, k, v)
            rows.append({"scenario": sc, "depreciation": "medium", "sensitivity": label,
                         "breakeven_year": res.breakeven_year, "stay_buy_net": None, "stay_rent_cum": None})
    return rows


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        a = load_assumptions(args.assumptions)
        inp = _inputs(args, a)
        if args.stay is not None and args.stay < 1:
            raise ValueError("--stay は1以上")
        res = simulate(inp)
        rows = summary(args, a) if args.summary else None
    except (AssumptionsError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2
    if rows is not None:
        if args.json:
            json.dump({"assumptions_updated_at": a.updated_at, "summary": rows}, out, ensure_ascii=False, indent=2)
            out.write("\n")
            return 0
        print("セット  経年減価  感度                          分岐年", file=out)
        for r in rows:
            be = f"{r['breakeven_year']}年目" if r["breakeven_year"] else f"{args.years}年以内なし"
            print(f"{r['scenario']:<7} {r['depreciation']:<9} {r['sensitivity'] or '-':<30} {be}", file=out)
        print("※年数は前提とセットでしか意味を持たない（holding.recommended_min_years_note）。", file=out)
        return 0
    if args.json:
        d = res.to_dict()
        d["assumptions_updated_at"] = a.updated_at
        d["stay_years"] = args.stay
        json.dump(d, out, ensure_ascii=False, indent=2)
        out.write("\n")
    else:
        _print_table(res, args.stay, out)
    return 0
