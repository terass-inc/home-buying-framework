"""テストケース（cases/NN-*.md）の読み込みと、API に送るメッセージ・ジャッジ入力の組み立て。

API を呼ばない純粋な処理だけをここに置く。SDK に依存しないので、
`python3 -m unittest discover -s scripts/tests` で単体テストできる。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CASES_DIR = ROOT / "cases"
LITE_PATH = ROOT / "dist" / "lite.md"
JUDGE_PROMPT_PATH = Path(__file__).resolve().parent / "judge_prompt.md"

# lite.md だけを送ったときに AI が返すことになっている1文（dist/lite.md の指示どおり）
FRAMEWORK_ACK = "前提を読み込みました。まず、家を買おうと思ったきっかけを教えてください"

SECTION_QUESTION = "質問"
SECTION_TYPICAL = "読み込ませない場合にありがちな回答"
SECTION_POINTS = "期待する回答の要点"
SECTION_NG = "NG判定"
SECTION_COMMON_NG = "全ケース共通のNG判定"
# 「ありがちな回答」は採点に使わない（ジャッジの自己テストでだけ使う）ので任意
REQUIRED_SECTIONS = (SECTION_QUESTION, SECTION_POINTS, SECTION_NG)

CONDITIONS = ("baseline", "framework")
FRAMEWORK_MODES = ("two-turn", "two-turn-live", "inline")

_NUMBERED = re.compile(r"^(\d+)\.\s+(.*)$")
_BULLET = re.compile(r"^[-*]\s+(.*)$")


class CaseFormatError(ValueError):
    pass


@dataclass
class Case:
    id: str                      # "01"
    slug: str                    # "01-loan-term-20-years"
    title: str                   # "住宅ローンを20年で組むべきか"
    question: str                # 利用者が入力する文（外側の「」は外す）
    points: list[str]            # 期待する回答の要点（番号順）
    ng_items: list[str]          # ケース固有の NG判定
    typical_answer: str = ""     # 読み込ませない場合にありがちな回答（無いケースもある）
    path: Path | None = None
    common_ng: list[str] = field(default_factory=list)

    def all_ng(self) -> list[tuple[str, str]]:
        """ジャッジに渡す NG 項目。ID は C1..（共通）と N1..（ケース固有）。"""
        items = [(f"C{i}", t) for i, t in enumerate(self.common_ng, 1)]
        items += [(f"N{i}", t) for i, t in enumerate(self.ng_items, 1)]
        return items

    def all_points(self) -> list[tuple[str, str]]:
        return [(f"P{i}", t) for i, t in enumerate(self.points, 1)]


def split_sections(markdown: str, level: int = 2) -> dict[str, str]:
    """`## 見出し` ごとに本文を切り出す。見出しより深い `###` は本文に含める。"""
    marker = "#" * level + " "
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in markdown.splitlines():
        if line.startswith(marker) and not line.startswith(marker + "#"):
            current = line[len(marker):].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _collect_items(body: str, start: re.Pattern) -> list[str]:
    """番号付き・箇条書きのリストを1項目ずつにまとめる。

    行頭（インデントなし）で `start` に一致した行が新しい項目。インデントされた
    サブ箇条書きや続きの段落は、直前の項目の一部として保持する。
    """
    items: list[list[str]] = []
    prev_blank = False
    for raw in body.splitlines():
        if not raw.strip():
            if items:
                items[-1].append("")
            prev_blank = True
            continue
        m = start.match(raw)
        if m and not raw[0].isspace():
            items.append([m.group(m.lastindex or 1).strip()])
        elif items and prev_blank and not raw[0].isspace():
            break  # 空行のあとのインデントなしの地の文で、リストは終わる
        elif items:
            items[-1].append(raw.rstrip())
        # 最初の項目より前の地の文は捨てる
        prev_blank = False
    return ["\n".join(lines).strip() for lines in items]


def parse_numbered(body: str) -> list[str]:
    return _collect_items(body, _NUMBERED)


def parse_bullets(body: str) -> list[str]:
    return _collect_items(body, _BULLET)


def strip_quotes(text: str) -> str:
    t = text.strip()
    if t.startswith("「") and t.endswith("」") and t.count("「") == 1:
        return t[1:-1].strip()
    return t


def parse_case(text: str, path: Path | None = None, slug: str | None = None) -> Case:
    name = slug or (path.stem if path else "unknown")
    lines = text.splitlines()
    h1 = next((l for l in lines if l.startswith("# ")), None)
    if h1 is None:
        raise CaseFormatError(f"{name}: H1 タイトルがありません")
    m = re.match(r"#\s+(\d+)\.\s*(.*)", h1)
    case_id = m.group(1) if m else name.split("-")[0]
    title = (m.group(2) if m else h1[2:]).strip()

    sections = split_sections(text)
    missing = [s for s in REQUIRED_SECTIONS if s not in sections]
    if missing:
        raise CaseFormatError(f"{name}: 見出しが見つかりません: {', '.join(missing)}")

    question = strip_quotes(sections[SECTION_QUESTION])
    points = parse_numbered(sections[SECTION_POINTS])
    ng = parse_bullets(sections[SECTION_NG])
    if not question:
        raise CaseFormatError(f"{name}: 質問が空です")
    if not points:
        raise CaseFormatError(f"{name}: 期待する回答の要点が番号付きリストになっていません")
    if not ng:
        raise CaseFormatError(f"{name}: NG判定が箇条書きになっていません")
    return Case(
        id=case_id, slug=name, title=title, question=question,
        typical_answer=sections.get(SECTION_TYPICAL, ""), points=points, ng_items=ng, path=path,
    )


def parse_common_ng(readme_text: str) -> list[str]:
    sections = split_sections(readme_text)
    if SECTION_COMMON_NG not in sections:
        raise CaseFormatError(f"cases/README.md: 「## {SECTION_COMMON_NG}」がありません")
    items = parse_bullets(sections[SECTION_COMMON_NG])
    if not items:
        raise CaseFormatError("cases/README.md: 共通NG判定が箇条書きになっていません")
    return items


def load_cases(cases_dir: Path = CASES_DIR) -> list[Case]:
    common = parse_common_ng((cases_dir / "README.md").read_text(encoding="utf-8"))
    cases = []
    for p in sorted(cases_dir.glob("[0-9]*.md")):
        c = parse_case(p.read_text(encoding="utf-8"), path=p)
        c.common_ng = list(common)
        cases.append(c)
    ids = [c.id for c in cases]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise CaseFormatError(f"ケース番号が重複しています: {sorted(dup)}")
    return cases


def select_cases(cases: list[Case], spec: str | None) -> list[Case]:
    """`--cases 01,07` / `1,7` / `01-03` で絞り込む。未知の番号はエラーにする。"""
    if not spec:
        return cases
    wanted: list[int] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            wanted.extend(range(int(a), int(b) + 1))
        else:
            wanted.append(int(token))
    by_num = {int(c.id): c for c in cases}
    unknown = [w for w in wanted if w not in by_num]
    if unknown:
        raise CaseFormatError(f"存在しないケース番号: {unknown}（有効: {sorted(by_num)}）")
    seen: set[int] = set()
    return [by_num[w] for w in wanted if not (w in seen or seen.add(w))]


# ---- メッセージ組み立て -------------------------------------------------

def build_messages(case: Case, condition: str, lite_text: str,
                   framework_mode: str = "two-turn") -> list[dict]:
    """被評価モデルに送る messages（provider 非依存の {role, content: str} 形式）。

    two-turn-live の場合は、1通目への応答を実際に生成するため、ここでは
    1通目だけを返す（呼び出し側が応答を追記してから質問を足す）。
    """
    if condition == "baseline":
        return [{"role": "user", "content": case.question}]
    if condition != "framework":
        raise ValueError(f"unknown condition: {condition}")
    if framework_mode == "inline":
        return [{"role": "user", "content": f"{lite_text.rstrip()}\n\n{case.question}"}]
    if framework_mode == "two-turn":
        return [
            {"role": "user", "content": lite_text},
            {"role": "assistant", "content": FRAMEWORK_ACK},
            {"role": "user", "content": case.question},
        ]
    if framework_mode == "two-turn-live":
        return [{"role": "user", "content": lite_text}]
    raise ValueError(f"unknown framework_mode: {framework_mode}")


# ---- ジャッジ入力 -------------------------------------------------------

def load_judge_prompt(path: Path = JUDGE_PROMPT_PATH) -> tuple[str, str]:
    """judge_prompt.md を (system, user テンプレート) に分ける。"""
    text = path.read_text(encoding="utf-8")
    # 区切りは「その行だけに書かれた」マーカー（説明文中の言及とは区別する）
    parts = re.split(r"^<!-- (SYSTEM|USER) -->[ \t]*$", text, flags=re.M)
    found = dict(zip(parts[1::2], parts[2::2]))
    if set(found) != {"SYSTEM", "USER"} or len(parts) != 5:
        raise CaseFormatError("judge_prompt.md には <!-- SYSTEM --> と <!-- USER --> の行が1つずつ必要です")
    return found["SYSTEM"].strip(), found["USER"].strip()


def _format_items(items: list[tuple[str, str]]) -> str:
    out = []
    for item_id, body in items:
        first, *rest = body.splitlines()
        out.append(f"- [{item_id}] {first}")
        out.extend(f"  {r}" if r.strip() else "" for r in rest)
    return "\n".join(out)


def render_judge_user(template: str, case: Case, response_text: str) -> str:
    """ジャッジに渡す本文。条件（フレームワークあり/なし）やモデル名は渡さない。"""
    # 回答中に区切りタグが含まれていても枠を壊さないようにする
    safe = response_text.replace("</response>", "&lt;/response&gt;")
    repl = {
        "{{CASE_TITLE}}": case.title,
        "{{QUESTION}}": case.question,
        "{{NG_ITEMS}}": _format_items(case.all_ng()),
        "{{POINTS}}": _format_items(case.all_points()),
        "{{RESPONSE}}": safe if safe.strip() else "（空の応答）",
    }
    out = template
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


def judge_schema(case: Case) -> dict:
    """ジャッジの構造化出力スキーマ。項目IDは enum で縛る。"""
    ng_ids = [i for i, _ in case.all_ng()]
    point_ids = [i for i, _ in case.all_points()]
    return {
        "type": "object",
        "properties": {
            "response_type": {
                "type": "string",
                "enum": ["clarifying_only", "answer_with_questions", "answer_only", "refusal_or_empty"],
            },
            "on_topic": {"type": "boolean"},
            "on_topic_reason": {"type": "string"},
            "ng_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": ng_ids},
                        "reason": {"type": "string"},
                        "evidence": {"type": "string"},
                        "triggered": {"type": "boolean"},
                    },
                    "required": ["id", "reason", "evidence", "triggered"],
                    "additionalProperties": False,
                },
            },
            "points": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": point_ids},
                        "reason": {"type": "string"},
                        "evidence": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["met", "partial", "deferred", "not_met", "not_applicable"],
                        },
                    },
                    "required": ["id", "reason", "evidence", "status"],
                    "additionalProperties": False,
                },
            },
            "overall_note": {"type": "string"},
        },
        "required": ["response_type", "on_topic", "on_topic_reason", "ng_items", "points", "overall_note"],
        "additionalProperties": False,
    }


def _dedupe_agreeing(items: list, verdict_key: str) -> list:
    """同じ項目IDが複数回あっても判定が一致していれば1つにまとめる。食い違っていれば残して重複として扱う。"""
    first, verdicts = {}, {}
    for x in items:
        i = x.get("id")
        verdicts.setdefault(i, set()).add(x.get(verdict_key))
        first.setdefault(i, x)
    out = []
    for x in items:
        i = x.get("id")
        if len(verdicts[i]) > 1:
            out.append(x)
        elif first.get(i) is x:
            out.append(x)
    return out


def validate_judgement(case: Case, j: dict) -> list[str]:
    """スキーマで縛れない整合性（全項目が1回ずつ判定されているか）を確認する。

    判定が一致する重複はその場で1つにまとめる（j を書き換える）。食い違う重複と判定漏れは問題として返す。
    """
    problems = []
    j["ng_items"] = _dedupe_agreeing(j.get("ng_items", []), "triggered")
    j["points"] = _dedupe_agreeing(j.get("points", []), "status")
    for key, expected in (("ng_items", [i for i, _ in case.all_ng()]),
                          ("points", [i for i, _ in case.all_points()])):
        got = [x.get("id") for x in j.get(key, [])]
        missing = [i for i in expected if i not in got]
        dup = sorted({i for i in got if got.count(i) > 1})
        if missing:
            problems.append(f"{key}: 判定漏れ {missing}")
        if dup:
            problems.append(f"{key}: 重複 {dup}")
    return problems


# ---- 採点ルール -----------------------------------------------------------

_POINT_SCORE = {"met": 1.0, "partial": 0.5, "deferred": 0.0, "not_met": 0.0}
_POINT_SCORE_LENIENT = {"met": 1.0, "partial": 0.5, "deferred": 1.0, "not_met": 0.0}


def score_judgement(j: dict) -> dict:
    """1回分のジャッジ結果から合否と要点充足率を計算する。

    合格 = 次をすべて満たす
      - NG判定（共通＋ケース固有）に1つも該当しない
      - 応答が空・拒否ではなく、質問に向き合っている（on_topic）
      - 確認質問だけの応答なら、少なくとも1つの要点を met/partial/deferred で満たしている
    """
    ng = j.get("ng_items", [])
    triggered = [x["id"] for x in ng if x.get("triggered")]
    pts = [p for p in j.get("points", []) if p.get("status") != "not_applicable"]
    n = len(pts)
    strict = sum(_POINT_SCORE.get(p["status"], 0.0) for p in pts) / n if n else None
    lenient = sum(_POINT_SCORE_LENIENT.get(p["status"], 0.0) for p in pts) / n if n else None
    rtype = j.get("response_type")
    engaged = any(p.get("status") in ("met", "partial", "deferred") for p in pts)
    other = []
    if rtype == "refusal_or_empty":
        other.append("空・拒否")
    if not j.get("on_topic", False):
        other.append("質問に向き合っていない")
    if rtype == "clarifying_only" and not engaged:
        other.append("確認質問が要点に沿っていない")
    reasons = (["NG該当: " + ",".join(triggered)] if triggered else []) + other
    case_triggered = [t for t in triggered if t.startswith("N")]
    return {
        "pass": not reasons,
        # 共通NG判定を除いた合否（どの種類のNGが合格率を動かしているかを見るための参考値）
        "pass_case_ng_only": not case_triggered and not other,
        "fail_reasons": reasons,
        "triggered": triggered,
        "coverage_strict": strict,
        "coverage_lenient": lenient,
        "response_type": rtype,
    }


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[*_`#>]", "", s)          # Markdown の装飾は無視する
    return re.sub(r"\s+", "", s)


def verify_evidence(evidence: str, response_text: str) -> bool | None:
    """根拠の引用が回答に実在するか。空の引用（＝該当箇所なし）は None。

    ジャッジが「…」で省略して複数箇所をつなぐことがあるため、省略記号で区切った
    断片がすべて回答に含まれていれば実在とみなす。
    """
    if not evidence or not evidence.strip():
        return None
    target = _norm(response_text)
    frags = [f for f in re.split(r"…|\.\.\.|⋯", evidence.strip().strip("「」\"'")) if _norm(f)]
    return bool(frags) and all(_norm(f) in target for f in frags)
