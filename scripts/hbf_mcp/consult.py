"""相談の進め方のガードレール。MCP サーバーの next_step / self_check の中身（mcp に依存しない）。

AGENTS.md の「回答前に必ず確認すること」「進め方は4ステップに従う」を、状態から次の一手を返す形にしたもの。
評価（scripts/eval）で最も多く残った失敗は「確認事項を一度に並べて聞く」だったので、
next_step は質問を1つだけ返す。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import ROOT  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts" / "eval"))
import case_parser  # noqa: E402

SURFACE_REASONS = ("家賃がもったいない", "狭い", "金利が上がる前", "資産になる", "周りが買っている", "子供が生まれる")

STEPS = {
    1: "①住む年数と購入コンセプト",
    2: "②資金計画",
    3: "③条件整理",
    4: "④物件見学",
}


@dataclass(frozen=True)
class Topic:
    label: str
    principles: tuple[str, ...]
    cases: tuple[str, ...]
    requires: tuple[str, ...]          # 結論を出す前に必要な事実（FIELDS のキー）
    tools: tuple[str, ...] = ()
    refuse: str = ""                   # 対応しない話題なら、断り方と代わりに示すこと


TOPICS: dict[str, Topic] = {
    "why_buy": Topic("なぜ買うのか・買ってもいいのか", ("00",), ("06",), ("purpose",)),
    "loan_term": Topic("ローン期間・繰上返済", ("02",), ("01",),
                       ("purpose", "stay_years", "savings_yen", "rate_type"), ("loan_term_comparison", "loan_schedule")),
    "rent_vs_buy": Topic("賃貸か購入か", ("04",), ("02", "07", "08"),
                         ("purpose", "stay_years", "property_price_yen", "comparable_rent_yen", "rent_moves_known", "rate_type"),
                         ("compare_rent_vs_buy",)),
    "budget": Topic("いくら借りるべきか", ("03",), ("03",),
                    ("purpose", "stay_years", "household_income_yen", "savings_yen", "family_plan_known"),
                    ("borrowing_budget", "balance_sheet")),
    "asset_value": Topic("資産性・担保評価・この物件はどうか", ("06",), ("04",),
                         ("purpose", "stay_years", "property_price_yen", "property_age_years"), ("selling_costs_breakdown",)),
    "timing": Topic("今買うべきか、待つべきか", ("01", "04"), (),
                    ("purpose", "current_rent_yen", "property_price_yen"), ("wait_cost",)),
    "rates_inflation": Topic("金利上昇・インフレ", ("04",), ("09",),
                             ("purpose", "stay_years", "rate_type"), ("compare_rent_vs_buy",)),
    "credit_investment": Topic("自宅を買わない人の与信の使い道", ("10",), ("10",), ("purpose",)),
    "danshin": Topic("団信・がん団信", ("05",), ("11",),
                     ("purpose", "stay_years", "existing_insurance_known"), ("loan_schedule",)),
    "mansion_vs_house": Topic("マンションか戸建てか", ("07",), (), ("purpose", "stay_years")),
    "new_vs_used": Topic("新築か中古か", ("08",), (), ("purpose", "stay_years")),
    "process": Topic("購入の進め方・営業・契約", ("09",), (), ("purpose", "stay_years")),
    "area": Topic("エリアの良し悪し", ("99", "06"), ("05",), (),
                  refuse=("エリアの優劣と将来の値上がりは断言しない。代わりに確認方法を示す: 平日・休日・夜に自分で歩く、"
                          "駅からの経路を歩く、ハザードマップ、自治体の人口推計と都市計画の一次情報を見る。")),
}

# AGENTS.md「回答前に必ず確認すること」の順。(キー, 質問, なぜ聞くか, ステップ)
FIELDS: list[tuple[str, str, str, int]] = [
    ("purpose", "家を買おうと思ったきっかけを教えてください。", "理由が決まると、何年住むか・広さ・エリアの優先順位が決まる", 1),
    ("purpose_sentence_confirmed", "まとめると「〇〇のための家」ということで合っていますか？",
     "「家賃がもったいない」「狭い」は入口であって理由ではない。描きたい暮らしを1文にして確かめる", 1),
    ("stay_years", "その家には何年くらい住む予定ですか？ 住み替えや売却の可能性はありますか？",
     "住宅の損益は売却時点で確定する。年数を決めずにローン期間や賃貸か購入かの結論は出せない", 1),
    ("household_income_yen", "世帯の年収はどのくらいですか？", "借りられる額と借りるべき額を分けて出すため", 2),
    ("savings_yen", "購入後も手元に残しておける現預金はどのくらいですか？", "頭金・諸費用を払った後の手元資金が、ローン期間や繰上返済の判断を左右する", 2),
    ("current_rent_yen", "今の住居費（家賃）はいくらですか？", "現状の把握と、待つコストの計算に使う（比較の賃料には使わない）", 3),
    ("property_price_yen", "検討している物件の価格はいくらですか？", "比較と予算の起点", 3),
    ("property_age_years", "その物件の種別（マンションか戸建て）と築年数、広さを教えてください。", "資産性・担保評価・修繕費の見込みが変わる", 3),
    ("comparable_rent_yen", "検討物件と同じエリア・広さ・築年帯の物件を借りると、家賃はいくらくらいですか？（募集賃料を3件ほど）",
     "比較に使う賃料は今の家賃ではなく同等物件の相場。ここを外すと結論が反転する", 3),
    ("rent_moves_known", "賃貸を続ける場合、住み替えの予定（時期と広さ）はありますか？", "聞かずに既定値を使うと、賃料が下がる前提が入って賃貸側が安く出る", 3),
    ("rate_type", "金利は固定と変動のどちらを考えていますか？ 適用金利も分かれば教えてください。",
     "「返済額は変わらないが賃料は上がる」という比較は固定金利が前提", 2),
    ("family_plan_known", "これから家族の人数が変わる予定や、教育・介護などで見込んでいる支出はありますか？", "予算は悲観シナリオで逆算するため", 2),
    ("existing_insurance_known", "今入っている医療保険・がん保険・就業不能保険はありますか？", "団信の特約と保障を二重に積まないため", 2),
]
FIELD_INDEX = {k: (q, why, step) for k, q, why, step in FIELDS}


@dataclass
class Facts:
    """相談でここまでに分かっていること。分かっていない項目は None / False のまま渡す。"""
    purpose: Optional[str] = None
    purpose_sentence_confirmed: bool = False
    stay_years: Optional[float] = None
    household_income_yen: Optional[int] = None
    savings_yen: Optional[int] = None
    current_rent_yen: Optional[int] = None
    property_price_yen: Optional[int] = None
    property_age_years: Optional[float] = None
    comparable_rent_yen: Optional[int] = None
    rent_moves_known: bool = False
    rate_type: Optional[str] = None
    interest_rate_pct: Optional[float] = None
    family_plan_known: bool = False
    existing_insurance_known: bool = False
    extra: dict = field(default_factory=dict)

    def known(self, key: str) -> bool:
        v = getattr(self, key)
        return bool(v) if isinstance(v, bool) else v not in (None, "")


def next_step(topic: str, facts: Facts) -> dict:
    if topic not in TOPICS:
        raise ValueError(f"topic は {sorted(TOPICS)} のどれか")
    t = TOPICS[topic]
    warnings: list[str] = []

    if t.refuse:
        return {
            "topic": t.label, "step": None, "can_conclude": False,
            "next_question": None, "missing": [],
            "how_to_answer": t.refuse, "principles": list(t.principles), "tools_when_ready": [],
            "warnings": [], "rule": "対応しない話題。断るだけでなく、代わりの確認方法を必ず示す",
        }

    needed = ["purpose", "stay_years", *t.requires]
    if facts.known("purpose") and any(s in (facts.purpose or "") for s in SURFACE_REASONS):
        warnings.append(f"きっかけ「{facts.purpose}」は入口であって理由ではない。解消されたら何をしたいかまで掘る")
        needed.insert(1, "purpose_sentence_confirmed")
    order = [k for k, *_ in FIELDS]
    missing = [k for k in order if k in set(needed) and not facts.known(k)]

    if facts.known("comparable_rent_yen") and facts.known("current_rent_yen") \
            and facts.comparable_rent_yen == facts.current_rent_yen:
        warnings.append("比較賃料が今の家賃と同じ。同等物件（同じエリア・広さ・築年帯）の相場か確認する")
    if facts.rate_type and facts.rate_type not in ("fixed", "variable", "固定", "変動"):
        warnings.append("rate_type は fixed（固定）か variable（変動）")
    if facts.rate_type in ("variable", "変動") and topic in ("rent_vs_buy", "rates_inflation"):
        warnings.append("変動金利では「返済額は名目で固定、賃料は上がる」という前提が成り立たない。固定の話だと明示する")

    step = min((FIELD_INDEX[k][2] for k in missing), default=4)
    nq = None
    if missing:
        q, why, _ = FIELD_INDEX[missing[0]]
        nq = {"field": missing[0], "question": q, "why": why}
    blocked = []
    if missing:
        blocked.append(f"{t.label}についての結論（{len(missing)}項目が未確認）")
        if "stay_years" in missing:
            blocked.append("ローン期間・賃貸か購入か・物件の良し悪しの結論（居住年数が未確認）")
    return {
        "topic": t.label,
        "step": STEPS[step],
        "can_conclude": not missing,
        "next_question": nq,
        "missing": [{"field": k, "question": FIELD_INDEX[k][0]} for k in missing],
        "blocked_conclusions": blocked,
        "principles": list(t.principles),
        "tools_when_ready": list(t.tools),
        "warnings": warnings,
        "rule": "1回の返答で聞く質問は next_question の1つだけ。missing を一度に並べて聞かない。"
                "未確認のまま一般論を示すときは、前提を置いたことを明示する",
    }


def self_check(topic: str, shows_numbers: bool = False) -> dict:
    """回答を送る前に照らす NG 判定の一覧（cases/ から生成）。"""
    if topic not in TOPICS:
        raise ValueError(f"topic は {sorted(TOPICS)} のどれか")
    t = TOPICS[topic]
    cases = {c.id: c for c in case_parser.load_cases(ROOT / "cases")}
    common = next(iter(cases.values())).common_ng if cases else []
    must = []
    for cid in t.cases:
        c = cases.get(cid)
        if c:
            must.append({"case": f"{c.id} {c.title}", "ng": c.ng_items, "should_include": c.points})
    always = [
        "1回の返答で聞く質問は1つ（多くても2つ。番号付きで3つ以上並べない）",
        "「買うべき」「買わないべき」のどちらにも寄らない。「買わない・今は買わない」も正しい結論",
        "相場の実績を示すときは期間と出典を添える。将来予測とエリアの優劣は断言しない",
    ]
    if shows_numbers:
        always.append("回答の末尾に「この試算は一般的な前提に基づく概算です。金利・税制は変わるため、最終的な判断は"
                      "金融機関やファイナンシャルプランナーなどの専門家と行ってください」という趣旨を必ず添える")
    return {"topic": t.label, "common_ng": common, "always": always, "topic_cases": must}
