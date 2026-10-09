#!/usr/bin/env python3
"""住宅購入AIフレームワークの MCP サーバー。

一問一答ではなく、相談の状態をサーバーが覚え、分かったことが増えるたびに
関係する計算をまとめた診断レポートを返す。前提を変えた「もしも」も試せる。
計算は scripts/rentbuy（calc/ と principles/ の手順の実装。文章の数値をテストで再現済み）を使う。

起動:
    uvx --from git+https://github.com/terass-inc/home-buying-framework hbf-mcp
    （リポジトリから）uv run --with "mcp>=2,<3" python scripts/hbf_mcp/server.py
"""
from __future__ import annotations

import base64
import json
import re
import sys
import zlib
from dataclasses import asdict, fields
from pathlib import Path
from typing import Annotated, Literal, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import consult  # noqa: E402
import report  # noqa: E402
from paths import ROOT  # noqa: E402
from report import DISCLAIMER, advice, inputs_from_assumptions, load_assumptions, simulate  # noqa: E402

try:
    from scripts.rentbuy import balance_after_months, interest_paid_months, monthly_payment
except ImportError:
    from rentbuy import balance_after_months, interest_paid_months, monthly_payment  # type: ignore

def _client_name(ctx) -> Optional[str]:
    for path in (("connection", "client_info", "name"), ("session", "client_params", "clientInfo", "name")):
        obj = ctx
        try:
            for a in path:
                obj = getattr(obj, a)
            if obj:
                return str(obj)
        except Exception:  # noqa: BLE001
            continue
    return None


async def usage_log(ctx, call_next):
    """計測用のログ。1回の呼び出しにつき1行の JSON を標準出力へ出す（Cloud Logging がそのまま集計できる形）。

    記録するのは、メソッド・道具の名前・AIアプリの名前・User-Agent・成否・処理時間だけ。
    相談の中身（引数の値、consultation_id）は記録しない。
    """
    import time
    t0, ok = time.perf_counter(), True
    try:
        result = await call_next(ctx)
        is_err = getattr(result, "isError", None) or getattr(result, "is_error", None)
        if is_err is None and isinstance(result, dict):
            is_err = result.get("isError")
        ok = not is_err
        return result
    except Exception:
        ok = False
        raise
    finally:
        method = getattr(ctx, "method", None)
        if method and method != "notifications/initialized":
            params = getattr(ctx, "params", None) or {}
            headers = {}
            try:
                headers = dict(getattr(ctx, "headers", None) or {})
            except Exception:  # noqa: BLE001
                pass
            client = (params.get("clientInfo") or {}).get("name") if method == "initialize" else _client_name(ctx)
            print(json.dumps({
                "event": "hbf_mcp_request", "method": method,
                "tool": params.get("name") if method == "tools/call" else None,
                "client": client, "user_agent": headers.get("user-agent"),
                "ok": ok, "ms": round((time.perf_counter() - t0) * 1000),
            }, ensure_ascii=False), flush=True)


# ---- 入力の範囲（公開サーバーなので、現実にありえない値で計算させない） --------------------------
Yen = Annotated[int, Field(ge=0, le=10_000_000_000, description="円（0〜100億円）")]
PriceYen = Annotated[int, Field(ge=1_000_000, le=10_000_000_000, description="円（100万〜100億円）")]
MonthlyYen = Annotated[int, Field(ge=10_000, le=10_000_000, description="円/月（1万〜1,000万円）")]
Years = Annotated[int, Field(ge=1, le=50, description="年（1〜50）")]
RatePct = Annotated[float, Field(ge=0, le=15, description="%（0〜15）")]
SharePct = Annotated[float, Field(ge=0, le=100, description="%（0〜100）")]


READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
SESSION = ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=False)
Topic = Literal[tuple(consult.TOPICS)]  # type: ignore[valid-type]

server = MCPServer(
    name="home-buying-framework",
    title="住宅購入AIフレームワーク",
    version="0.2.0",
    instructions=(
        "住宅購入・住宅ローン・賃貸か購入かの相談では、最初に get_framework を読み、その前提を会話全体に適用する。"
        "相談が始まったら start_consultation で相談を作り、利用者の話から分かったことを update_facts でまとめて渡す"
        "（1つずつ聞き直さない）。返ってくる診断レポートをもとに答え、次に聞くのは『次に分かると精度が上がること』の1つだけにする。"
        "利用者が前提を変えて考えたいときは what_if を使う。計算は道具を使い、暗算しない。"
        "回答を送る前に self_check で NG 判定に当たっていないか確認する。"
        "比較賃料は今の家賃ではなく同等物件の相場。居住年数が分からないうちは結論を出さない。"
        "作成は不動産仲介会社の株式会社TERASSであり、利用者にはその立場を伝える。"
    ),
    website_url="https://terass-inc.github.io/home-buying-framework/",
    middleware=[usage_log],
)


# ---- 相談の状態 ---------------------------------------------------------------

class FactsInput(BaseModel):
    """相談で分かったこと。分かったものだけ渡せばよい（渡さない項目は変えない）。金額は円。"""
    purpose: Optional[str] = Field(None, max_length=500, description="家を買おうと思ったきっかけ・理由（利用者の言葉のまま、500字以内）")
    purpose_sentence_confirmed: Optional[bool] = Field(None, description="「〇〇のための家」と1文で確認できたか")
    stay_years: Optional[float] = Field(None, ge=1, le=50, description="何年住む予定か（売却・住み替えまでの年数）")
    household_income_yen: Optional[int] = Field(None, ge=0, le=10_000_000_000, description="世帯年収（円）")
    savings_yen: Optional[int] = Field(None, ge=0, le=10_000_000_000, description="購入後も手元に残せる現預金（円）")
    current_rent_yen: Optional[int] = Field(None, ge=10_000, le=10_000_000, description="今の家賃（円/月）。比較の賃料には使わない")
    property_price_yen: Optional[int] = Field(None, ge=1_000_000, le=10_000_000_000, description="検討している物件の価格（円）")
    property_age_years: Optional[float] = Field(None, ge=0, le=150, description="築年数")
    comparable_rent_yen: Optional[int] = Field(None, ge=10_000, le=10_000_000, description="検討物件と同等の物件の募集賃料の相場（円/月）")
    rent_moves_known: Optional[bool] = Field(None, description="賃貸を続ける場合の住み替え予定を確認したか")
    rate_type: Optional[Literal["fixed", "variable"]] = Field(None, description="金利タイプ")
    interest_rate_pct: Optional[float] = Field(None, ge=0, le=15, description="適用金利（%）")
    family_plan_known: Optional[bool] = Field(None, description="家族構成の変化・教育や介護の支出を確認したか")
    existing_insurance_known: Optional[bool] = Field(None, description="医療・がん・就業不能保険の加入状況を確認したか")


_FACT_FIELDS = {f.name for f in fields(consult.Facts)} - {"extra"}
def _empty(v) -> bool:
    """未入力かどうか。数値の 0（貯金0円・金利0%など）は入力済みとして扱う（0 == False で落とさない）。"""
    return v is None or v is False or v == ""


_TOKEN_PREFIX = "c1."
_TOKEN_MAX_CHARS = 4_000    # 実際の相談IDは数百文字
_TOKEN_MAX_BYTES = 16_384   # 展開後の上限


def _encode(f: consult.Facts) -> str:
    """相談の状態を相談IDそのものに詰める。サーバーは何も保存しない（URLで公開しても、
    リクエストごとに別のサーバーが応答しても同じ相談を続けられ、利用者の情報がサーバーに残らない）。"""
    d = {k: v for k, v in asdict(f).items() if k in _FACT_FIELDS and not _empty(v)}
    raw = zlib.compress(json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9)
    return _TOKEN_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _merge(base: consult.Facts, new: Optional[FactsInput]) -> consult.Facts:
    if new is None:
        return base
    d = asdict(base)
    d.update({k: v for k, v in new.model_dump().items() if v is not None and k in _FACT_FIELDS})
    return consult.Facts(**d)


def _session(consultation_id: str) -> consult.Facts:
    try:
        if not consultation_id.startswith(_TOKEN_PREFIX):
            raise ValueError
        b = consultation_id[len(_TOKEN_PREFIX):]
        if len(b) > _TOKEN_MAX_CHARS:  # 公開サーバーなので、巨大な ID で負荷をかけられないようにする
            raise ValueError
        z = zlib.decompressobj()
        raw = z.decompress(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)), _TOKEN_MAX_BYTES)
        if z.unconsumed_tail or not z.eof:  # 展開後が上限を超える（圧縮爆弾）か、壊れている
            raise ValueError
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError
        # 相談IDは利用者側から来る信頼できない入力。ふつうの入力と同じ範囲・型の検査を通す
        checked = FactsInput.model_validate(d)
        return _merge(consult.Facts(), checked)
    except Exception as exc:  # noqa: BLE001
        raise ToolError("consultation_id を読めません。直前に返された consultation_id をそのまま渡すか、"
                        "start_consultation で作り直してください") from exc


def _report(cid: str, f: consult.Facts) -> dict:
    r = report.diagnose(f)
    known = {k: v for k, v in asdict(f).items() if k != "extra" and not _empty(v)}
    return {"consultation_id": cid, "consultation_id_note": "次の呼び出しでは、この consultation_id を使う（更新のたびに変わる）",
            "分かっていること": known, "report": r, "report_markdown": report.to_markdown(r)}


@server.tool(annotations=SESSION, description=(
    "相談を始める。分かっていることがあれば facts に入れる（なくてもよい）。"
    "相談ID（状態を詰めたもの。サーバーは保存しない）と、今の前提での診断レポート（賃貸か購入か・ローン期間・待つコスト・借りるべき額・購入直後の純資産・"
    "結論が変わる条件・次に分かると精度が上がること）を返す。"))
def start_consultation(facts: Optional[FactsInput] = None) -> dict:
    f = _merge(consult.Facts(), facts)
    return _report(_encode(f), f)


@server.tool(annotations=SESSION, description=(
    "相談に、新しく分かったことをまとめて足し、診断レポートを更新して返す。"
    "利用者が1回の発言で複数のことを話したら、全部まとめて渡す。返ってきた新しい consultation_id を次から使う。"))
def update_facts(consultation_id: Annotated[str, Field(max_length=4_100)], facts: FactsInput) -> dict:
    f = _merge(_session(consultation_id), facts)
    return _report(_encode(f), f)


@server.tool(annotations=READ_ONLY, description="相談の今の診断レポートを返す。")
def diagnose(consultation_id: Annotated[str, Field(max_length=4_100)]) -> dict:
    return _report(consultation_id, _session(consultation_id))


@server.tool(annotations=READ_ONLY, description=(
    "前提を一時的に変えたらどうなるかを試す（保存しない）。例: 居住年数を5年にしたら、変動金利なら、"
    "比較賃料が18万円なら。主な数字（損益分岐年・居住年数時点の差・毎月返済）の変化と、変更後の結論が変わる条件を返す。"))
def what_if(consultation_id: Annotated[str, Field(max_length=4_100)], changes: FactsInput) -> dict:
    f = _session(consultation_id)
    ch = {k: v for k, v in changes.model_dump().items() if v is not None and k in _FACT_FIELDS}
    if not ch:
        raise ToolError("changes に変えたい項目を1つ以上入れてください")
    return report.what_if(f, ch)


@server.tool(annotations=READ_ONLY, description=(
    "話題と分かっていることから、相談がどのステップにいるか、次に聞く質問（1つだけ）、まだ出せない結論を返す。"
    "相談IDがあればその状態を使う。"))
def next_step(topic: Topic, consultation_id: Optional[str] = None, facts: Optional[FactsInput] = None) -> dict:
    f = _merge(_session(consultation_id) if consultation_id else consult.Facts(), facts)
    return consult.next_step(topic, f)


@server.tool(annotations=READ_ONLY, description=(
    "回答を送る前に照らす NG 判定の一覧（全ケース共通＋話題別のテストケース）と、必ず含める要点。"
    "金額の試算を示すなら shows_numbers=true（免責文の要件が加わる）。"))
def self_check(topic: Topic, shows_numbers: bool = False) -> dict:
    return consult.self_check(topic, shows_numbers)


# ---- 前提・原則・数値 ---------------------------------------------------------------

def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _principles() -> dict[str, Path]:
    return {p.name[:2]: p for p in sorted((ROOT / "principles").glob("[0-9][0-9]-*.md"))}


@server.tool(annotations=READ_ONLY, description="AIへの前提の全文（5つのルール・確認事項・4ステップ・対応しないこと・数値前提）。相談に答える前に必ず読む。")
def get_framework() -> str:
    return _read("dist/lite.md")


@server.tool(annotations=READ_ONLY, description="話題別の原則の一覧（ID・題名・一文要約）。")
def list_principles() -> list[dict]:
    out = []
    for pid, path in _principles().items():
        t = path.read_text(encoding="utf-8")
        title = re.sub(r"（AIが回答するときの原則）$", "", re.search(r"^# (.+)$", t, re.M).group(1)).strip()
        quote = re.search(r"^> (.+)$", t, re.M)
        out.append({"id": pid, "title": title, "summary": quote.group(1).strip() if quote else ""})
    return out


@server.tool(annotations=READ_ONLY, description="話題別の原則の本文（判断手順・アンチパターン・断り方）。id は list_principles の ID（例: '02'）。")
def get_principle(id: Annotated[str, Field(max_length=4)]) -> str:
    files = _principles()
    if id not in files:
        raise ToolError(f"id は {sorted(files)} のどれか")
    return files[id].read_text(encoding="utf-8")


@server.tool(annotations=READ_ONLY, description="数値前提（assumptions.yaml）。section を指定するとその節だけ。コメントの注意も読む。「見直し要」は最新値の確認が必要。")
def get_assumptions(section: Optional[Annotated[str, Field(max_length=64)]] = None) -> str:
    text = _read("assumptions.yaml")
    if not section:
        return text
    m = re.search(rf"^{re.escape(section)}:.*?(?=^[a-z_]+:|\Z)", text, re.M | re.S)
    if not m:
        raise ToolError(f"section は {re.findall(r'^([a-z_]+):', text, re.M)} のどれか")
    return m.group(0).rstrip()


# ---- 個別の計算 ---------------------------------------------------------------

@server.tool(annotations=READ_ONLY, description=(
    "賃貸か購入かを、売却時の手残りまで含めた純負担で比べる（calc/rent-vs-buy.md）。"
    "rent_yen_per_month は今の家賃ではなく同等物件の相場。低・中・高セットと波及率0%の分岐年も返す。"))
def compare_rent_vs_buy(price_yen: PriceYen, rent_yen_per_month: MonthlyYen, stay_years: Years,
                        scenario: Literal["low", "medium", "high"] = "medium",
                        interest_rate_pct: Optional[RatePct] = None, price_pass_through_pct: SharePct = 100.0,
                        depreciation: Literal["small", "medium", "large"] = "medium",
                        include_danshin: bool = True) -> dict:
    a = load_assumptions(ROOT / "assumptions.yaml")

    def run(sc, **extra):
        kw = dict(price_yen=price_yen, rent_yen_per_month=rent_yen_per_month, horizon_years=max(40, stay_years),
                  price_pass_through_pct=price_pass_through_pct, include_danshin=include_danshin)
        if interest_rate_pct is not None and sc == scenario:
            kw["interest_rate_pct"] = interest_rate_pct
        kw.update(extra)
        return simulate(inputs_from_assumptions(a, scenario=sc, depreciation=depreciation, **kw))

    m = run(scenario)
    r = m.row(stay_years)
    man = lambda y: round(y / 10_000)  # noqa: E731
    return {
        "毎月のローン返済_万円": round(m.monthly_payment / 10_000, 2),
        f"{stay_years}年目": {"賃貸の累計負担_万円": man(r.rent_cum), "購入の累計支出_万円": man(r.cashout_cum),
                             "物件価値_万円": man(r.property_value), "残債_万円": man(r.loan_balance),
                             "売却諸費用_万円": man(r.selling_cost), "手残り_万円": man(r.equity_on_sale),
                             "購入−賃貸_万円（負なら購入が有利）": man(r.buy_minus_rent)},
        "損益分岐年": m.breakeven_year,
        "感度": {f"{sc}セット": run(sc).breakeven_year for sc in ("low", "medium", "high")}
        | {"波及率0%": run(scenario, price_pass_through_pct=0.0).breakeven_year},
        "注意": DISCLAIMER,
    }


@server.tool(annotations=READ_ONLY, description="元利均等の毎月返済額、完済までの利息、sell_after_years 年後の残債とそれまでの利息。")
def loan_schedule(principal_yen: PriceYen, interest_rate_pct: RatePct, term_years: Years, sell_after_years: Optional[Years] = None) -> dict:
    out = {"毎月の返済_円": round(monthly_payment(principal_yen, interest_rate_pct, term_years)),
           "完済までの利息総額_万円": round(interest_paid_months(principal_yen, interest_rate_pct, term_years, term_years * 12) / 10_000)}
    if sell_after_years:
        mths = sell_after_years * 12
        out[f"{sell_after_years}年後の残債_万円"] = round(balance_after_months(principal_yen, interest_rate_pct, term_years, mths) / 10_000)
        out[f"{sell_after_years}年間に払う利息_万円"] = round(interest_paid_months(principal_yen, interest_rate_pct, term_years, mths) / 10_000)
    out["注意"] = DISCLAIMER
    return out


@server.tool(annotations=READ_ONLY, description=(
    "ローン期間の比較（principles/02）。毎月返済・総利息・売却時点の残債と正味の差・差額の使い道・審査金利での返済比率。"))
def loan_term_comparison(principal_yen: PriceYen, annual_rate_pct: Optional[RatePct] = None, short_term_years: Years = 20,
                         long_term_years: Optional[Years] = None, sale_year: Optional[Years] = None,
                         investment_yield_pct: Optional[RatePct] = None, annual_income_yen: Optional[Yen] = None) -> dict:
    return advice.loan_term_comparison(principal_yen, annual_rate_pct=annual_rate_pct, short_term_years=short_term_years,
                                       long_term_years=long_term_years, sale_year=sale_year,
                                       investment_yield_pct=investment_yield_pct, annual_income_yen=annual_income_yen).to_dict()


@server.tool(annotations=READ_ONLY, description="待つコスト（principles/01・04）。待って得をするための損益分岐下落率と、過去の下落実績との比較。")
def wait_cost(property_price_yen: PriceYen, monthly_rent_yen: MonthlyYen,
              wait_years: Annotated[float, Field(gt=0, le=10)] = 1) -> dict:
    return advice.wait_cost(property_price_yen, monthly_rent_yen, wait_years=wait_years).to_dict()


@server.tool(annotations=READ_ONLY, description=(
    "借りられる額（銀行の上限）と借りるべき額（principles/03）を分けて返す。借りるべき額は式で決めず、確認すべき値を返す。"))
def borrowing_budget(annual_income_yen: Annotated[int, Field(ge=1_000_000, le=10_000_000_000)], property_price_yen: Optional[PriceYen] = None,
                     property_type: Optional[Literal["new_condo", "used_or_house"]] = None,
                     other_debt_annual_payment_yen: Yen = 0, affordable_monthly_payment_yen: Optional[MonthlyYen] = None,
                     stress_rate_pct: Optional[RatePct] = None) -> dict:
    return advice.borrowing_budget(annual_income_yen, property_price_yen=property_price_yen, property_type=property_type,
                                   other_debt_annual_payment_yen=other_debt_annual_payment_yen,
                                   affordable_monthly_payment_yen=affordable_monthly_payment_yen,
                                   stress_rate_pct=stress_rate_pct).to_dict()


@server.tool(annotations=READ_ONLY, description="住宅購入のバランスシート（calc/balance-sheet.md）。購入直後と N 年後の資産・負債・純資産。")
def balance_sheet(property_price_yen: PriceYen, savings_yen: Yen = 4_000_000, securities_yen: Yen = 1_000_000,
                  other_assets_yen: Yen = 1_000_000, term_years: Years = 35, annual_rate_pct: Optional[RatePct] = None,
                  target_year: Years = 20) -> dict:
    return advice.balance_sheet(property_price_yen=property_price_yen, savings_yen=savings_yen, securities_yen=securities_yen,
                                other_assets_yen=other_assets_yen, term_years=term_years, annual_rate_pct=annual_rate_pct,
                                target_year=target_year).to_dict()


@server.tool(annotations=READ_ONLY, description="売却諸費用の内訳（仲介手数料・その他費用・合計・率）。")
def selling_costs_breakdown(sale_price_yen: PriceYen) -> dict:
    return advice.selling_costs_breakdown(sale_price_yen).to_dict()


# ---- リソース・プロンプト ---------------------------------------------------------------

@server.resource("framework://lite", name="AIへの前提（全文）", mime_type="text/markdown")
def lite_resource() -> str:
    return _read("dist/lite.md")


@server.resource("framework://assumptions", name="数値前提（assumptions.yaml）", mime_type="application/yaml")
def assumptions_resource() -> str:
    return _read("assumptions.yaml")


@server.resource("framework://principles/{pid}", name="話題別の原則", mime_type="text/markdown")
def principle_resource(pid: str) -> str:
    return get_principle(pid)


@server.resource("framework://cases/{cid}", name="テストケース", mime_type="text/markdown")
def case_resource(cid: str) -> str:
    hits = sorted((ROOT / "cases").glob(f"{cid}-*.md"))
    if not hits:
        raise ToolError(f"ケース {cid} はありません")
    return hits[0].read_text(encoding="utf-8")


@server.prompt(name="home_buying_consultation", description="住宅購入AIフレームワークの前提で相談を始める")
def consultation_prompt() -> str:
    return _read("dist/lite.md") + "\n\n相談が始まったら start_consultation を使い、分かったことは update_facts でまとめて渡すこと。"


@server.prompt(name="rent_vs_buy_check", description="賃貸か購入かを、前提をそろえて診断する")
def rent_vs_buy_prompt(property_price: str = "", comparable_rent: str = "", stay_years: str = "") -> str:
    known = "、".join(x for x in (f"物件価格 {property_price}" if property_price else "",
                                  f"同等物件の家賃 {comparable_rent}" if comparable_rent else "",
                                  f"居住年数 {stay_years}" if stay_years else "") if x)
    return (f"住宅購入AIフレームワークの前提で、賃貸か購入かを考えたい。分かっていること: {known or 'まだない'}。"
            "get_framework を読み、start_consultation で診断し、足りない前提は1つずつではなく、次に必要なものから聞いてください。")


@server.prompt(name="loan_term_check", description="ローン期間（20年か35年か）を、売却時期から考える")
def loan_term_prompt(loan_amount: str = "", sale_after_years: str = "") -> str:
    return (f"住宅ローンの期間を決めたい。借入額 {loan_amount or '未定'}、売却予定 {sale_after_years or '未定'}。"
            "get_framework を読み、利息総額ではなく売却時の残債と手残りで比べてください（loan_term_comparison を使う）。")


def http_app(allowed_hosts: Optional[list[str]] = None):
    """URL で公開するときの ASGI アプリ（/mcp）。状態を持たないので、サーバーレスや複数台でも動く。

    allowed_hosts（または環境変数 HBF_ALLOWED_HOSTS、カンマ区切り）に公開するドメインを入れる。
    例: HBF_ALLOWED_HOSTS=home-buying-framework.example.com。指定がなければローカル（127.0.0.1・localhost）だけを受け付ける。
    """
    import os
    from mcp.server.transport_security import TransportSecuritySettings
    hosts = allowed_hosts or [h.strip() for h in os.environ.get("HBF_ALLOWED_HOSTS", "").split(",") if h.strip()]
    hosts = hosts or ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    sec = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                    allowed_origins=[f"https://{h}" for h in hosts] + [f"http://{h}" for h in hosts])
    path = os.environ.get("HBF_MCP_PATH", "/mcp")  # terass.house では /mcp/home-buying
    from http_guard import HttpGuard
    app = server.streamable_http_app(streamable_http_path=path, stateless_http=True, json_response=True,
                                     transport_security=sec, max_request_body_size=64 * 1024)
    return HttpGuard(app, path)


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="住宅購入AIフレームワークの MCP サーバー")
    p.add_argument("--http", action="store_true", help="標準入出力ではなく HTTP（/mcp）で待ち受ける")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    if args.http:
        import uvicorn
        uvicorn.run(http_app(), host=args.host, port=args.port)
    else:
        server.run()


if __name__ == "__main__":
    main()
