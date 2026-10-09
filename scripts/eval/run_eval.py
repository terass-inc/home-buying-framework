#!/usr/bin/env python3
"""フレームワーク（dist/lite.md）なし／ありで、cases/ の NG判定の通過率を計測する評価ハーネス。

使い方は scripts/eval/README.md を参照。

    python3 scripts/eval/run_eval.py --dry-run
    python3 scripts/eval/run_eval.py --models claude-opus-5-5,gpt-5 --trials 3
    python3 scripts/eval/run_eval.py --judge-selftest --cases 01,07

設計の要点:
- 被評価モデルの応答と、ジャッジ（別の LLM）の採点を分けて保存する。
- ジャッジには条件（フレームワークあり/なし）もモデル名も渡さない（ラベルへの忖度を防ぐ）。
- API エラー・打ち切り・拒否は「不合格」に混ぜず、別に数える。
- 合格率の区間は、同じケースの試行どうしが相関するため、ケース単位のブートストラップで出す。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import random
import secrets
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import case_parser as cp  # noqa: E402

HARNESS_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# 既定値はここに集約する（モデルIDは --models / --judge-model / 環境変数で上書き）
# ---------------------------------------------------------------------------
DEFAULTS = {
    # 被評価モデル。環境変数 EVAL_MODELS（カンマ区切り）で上書きできる。
    # OpenAI 側の ID は実行前に https://platform.openai.com/docs/models で現行のものを確認すること。
    "models": ["claude-opus-5-5", "gpt-5"],
    # ジャッジ。環境変数 EVAL_JUDGE_MODEL で上書きできる。
    "judge_model": "claude-opus-5-5",
    "judge_effort": "high",          # Anthropic のジャッジに渡す output_config.effort
    "judge_repeats": 1,              # 同じ応答を何回採点するか（2以上で多数決と一致率を出す）
    "trials": 3,
    "workers": 4,
    "framework_mode": "two-turn",
    "max_tokens": 16000,             # 被評価モデルの出力上限
    "judge_max_tokens": 32000,
    "max_attempts": 6,               # 429/5xx/接続エラー時の最大試行回数
    "timeout_s": 600,
    "shuffle_seed": 20261009,        # 実行順のシャッフル（条件ごとの時間帯の偏りを避ける）
    "bootstrap_seed": 12345,
    "bootstrap_resamples": 2000,
    "openai_base_url": "https://api.openai.com/v1",
}

# 費用の概算用（USD / 100万トークン）。Anthropic の公表価格 2026-10-06 時点。
# 載っていないモデルはトークン数だけ表示する。
PRICES_PER_MTOK = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01},
}

# --dry-run の費用見積もりに使う仮定（実測ではない）
ESTIMATE = {
    "chars_per_token": 1.0,          # 日本語はおおむね1文字≒1トークン以上。安全側に倒す
    "subject_output_tokens": 3000,   # 応答＋思考（thinking/reasoning）の合計の仮定
    "judge_output_tokens": 6000,     # 項目ごとの理由・引用＋思考の合計の仮定
}

LITE_MARKER = "{{dist/lite.md sha256=%s}}"


def env_list(name: str, default: list[str]) -> list[str]:
    v = os.environ.get(name)
    return [x.strip() for x in v.split(",") if x.strip()] if v else list(default)


def resolve_provider(model: str) -> tuple[str, str]:
    """'anthropic:xxx' / 'openai:xxx' の明示、または ID の接頭辞で provider を決める。"""
    if ":" in model:
        p, m = model.split(":", 1)
        if p not in ("anthropic", "openai"):
            raise ValueError(f"未知の provider: {p}")
        return p, m
    if model.startswith("claude"):
        return "anthropic", model
    if model.startswith(("gpt", "chatgpt", "o1", "o3", "o4", "o5")):
        return "openai", model
    raise ValueError(f"provider を判別できません: {model}（anthropic:{model} のように明示してください）")


def has_key(provider: str) -> bool:
    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return bool(os.environ.get("OPENAI_API_KEY"))


def key_env(provider: str) -> str:
    return "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"


# ---------------------------------------------------------------------------
# 再試行
# ---------------------------------------------------------------------------
class RetryableError(Exception):
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class CallFailed(Exception):
    def __init__(self, msg: str, error_class: str, attempts: int):
        super().__init__(msg)
        self.error_class = error_class
        self.attempts = attempts


def with_retries(fn, max_attempts: int, label: str):
    """fn() を呼ぶ。RetryableError はジッター付き指数バックオフで再試行する。

    返り値: (結果, 試行回数, 成功した試行の所要秒数)。待ち時間は所要秒数に含めない。
    """
    for attempt in range(1, max_attempts + 1):
        t0 = time.monotonic()
        try:
            out = fn()
            return out, attempt, time.monotonic() - t0
        except RetryableError as e:
            if attempt == max_attempts:
                raise CallFailed(f"{label}: 再試行上限 {max_attempts} 回に達しました: {e}", "retry_exhausted", attempt)
            wait = e.retry_after if e.retry_after else min(60.0, 2.0 ** attempt)
            wait *= random.uniform(0.8, 1.3)
            print(f"  [retry] {label}: {e} → {wait:.1f}秒後に再試行（{attempt}/{max_attempts}）", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Provider クライアント
# ---------------------------------------------------------------------------
class AnthropicProvider:
    name = "anthropic"

    def __init__(self, timeout_s: float):
        import anthropic  # 遅延 import（--dry-run と単体テストでは SDK 不要）
        self.sdk = anthropic
        # 再試行は with_retries 側で行い、回数を記録する
        self.client = anthropic.Anthropic(max_retries=0, timeout=timeout_s)

    def version(self) -> str:
        return getattr(self.sdk, "__version__", "unknown")

    def _call(self, **kwargs):
        a = self.sdk
        try:
            with self.client.messages.stream(**kwargs) as stream:
                return stream.get_final_message()
        except a.APIConnectionError as e:          # APITimeoutError を含む
            raise RetryableError(f"connection: {e}")
        except a.APIStatusError as e:
            if e.status_code in (408, 409, 429) or e.status_code >= 500:
                ra = e.response.headers.get("retry-after") if e.response is not None else None
                raise RetryableError(f"HTTP {e.status_code}", float(ra) if ra else None)
            raise CallFailed(f"HTTP {e.status_code}: {e.message}", f"http_{e.status_code}", 1)

    @staticmethod
    def _to_api_messages(messages: list[dict], cache_first: bool) -> list[dict]:
        out = []
        for i, m in enumerate(messages):
            if isinstance(m["content"], str):
                block = {"type": "text", "text": m["content"]}
                # lite.md を含む最初の user メッセージをキャッシュする（出力には影響しない）
                if cache_first and i == 0:
                    block["cache_control"] = {"type": "ephemeral"}
                out.append({"role": m["role"], "content": [block]})
            else:
                out.append(m)                      # two-turn-live で応答ブロックをそのまま返す場合
        return out

    @staticmethod
    def _usage(msg) -> dict:
        u = msg.usage
        return {
            "input_tokens": u.input_tokens or 0,
            "output_tokens": u.output_tokens or 0,
            "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }

    def generate(self, model: str, messages: list[dict], max_tokens: int, sampling: dict,
                 cache_first: bool) -> dict:
        kwargs = {"model": model, "max_tokens": max_tokens,
                  "messages": self._to_api_messages(messages, cache_first)}
        if sampling.get("effort"):
            kwargs["output_config"] = {"effort": sampling["effort"]}
        if sampling.get("temperature") is not None:
            # SDK 1.x は temperature 引数を持たない。新しいモデルは既定値以外を 400 で拒否する
            kwargs["extra_body"] = {"temperature": sampling["temperature"]}
        msg = self._call(**kwargs)
        text = "".join(b.text for b in msg.content if b.type == "text")
        status = {"max_tokens": "truncated", "refusal": "refusal"}.get(msg.stop_reason, "ok")
        return {
            "text": text, "stop_reason": msg.stop_reason, "status": status,
            "served_model": msg.model, "usage": self._usage(msg),
            "_assistant_content": msg.content,
        }

    def judge(self, model: str, system: str, user: str, schema: dict, max_tokens: int,
              effort: str | None) -> dict:
        output_config = {"format": {"type": "json_schema", "schema": schema}}
        if effort:
            output_config["effort"] = effort
        msg = self._call(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
        if msg.stop_reason != "end_turn":
            raise CallFailed(f"judge stop_reason={msg.stop_reason}", f"judge_{msg.stop_reason}", 1)
        text = next(b.text for b in msg.content if b.type == "text")
        return {"parsed": json.loads(text), "served_model": msg.model, "usage": self._usage(msg)}


class OpenAIProvider:
    """OpenAI Chat Completions を標準ライブラリで呼ぶ（SDK の版差を避けるため）。"""
    name = "openai"

    def __init__(self, timeout_s: float, base_url: str):
        self.key = os.environ["OPENAI_API_KEY"]
        self.base = base_url.rstrip("/")
        self.timeout = timeout_s

    def version(self) -> str:
        return "urllib (Chat Completions REST)"

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code in (408, 409, 429) or e.code >= 500:
                ra = e.headers.get("retry-after")
                raise RetryableError(f"HTTP {e.code}", float(ra) if ra else None)
            raise CallFailed(f"HTTP {e.code}: {detail}", f"http_{e.code}", 1)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise RetryableError(f"connection: {e}")

    @staticmethod
    def _usage(resp: dict) -> dict:
        u = resp.get("usage") or {}
        return {
            "input_tokens": u.get("prompt_tokens", 0),
            "output_tokens": u.get("completion_tokens", 0),
            "cache_read_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0,
        }

    def generate(self, model: str, messages: list[dict], max_tokens: int, sampling: dict,
                 cache_first: bool) -> dict:
        body = {"model": model, "max_completion_tokens": max_tokens,
                "messages": [{"role": m["role"], "content": m["content"]} for m in messages]}
        if sampling.get("temperature") is not None:
            body["temperature"] = sampling["temperature"]
        if sampling.get("reasoning_effort"):
            body["reasoning_effort"] = sampling["reasoning_effort"]
        resp = self._post(body)
        ch = resp["choices"][0]
        msg = ch.get("message") or {}
        finish = ch.get("finish_reason")
        status = "ok"
        if finish == "length":
            status = "truncated"
        elif finish == "content_filter" or msg.get("refusal"):
            status = "refusal"
        text = msg.get("content") or ""
        return {"text": text, "stop_reason": finish, "status": status,
                "served_model": resp.get("model"), "usage": self._usage(resp),
                "_assistant_content": text}

    def judge(self, model: str, system: str, user: str, schema: dict, max_tokens: int,
              effort: str | None) -> dict:
        body = {
            "model": model, "max_completion_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "judgement", "strict": True, "schema": schema}},
        }
        resp = self._post(body)
        ch = resp["choices"][0]
        if ch.get("finish_reason") != "stop":
            raise CallFailed(f"judge finish_reason={ch.get('finish_reason')}", "judge_incomplete", 1)
        return {"parsed": json.loads(ch["message"]["content"]),
                "served_model": resp.get("model"), "usage": self._usage(resp)}


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------
def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=cp.ROOT, capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return ""


def repo_info() -> dict:
    return {
        "head": git("rev-parse", "HEAD") or "unknown",
        "lite_commit": git("log", "-1", "--format=%H", "--", "dist/lite.md") or "unknown",
        "lite_dirty": bool(git("status", "--porcelain", "--", "dist/lite.md")),
        "cases_dirty": bool(git("status", "--porcelain", "--", "cases/")),
        "harness_dirty": bool(git("status", "--porcelain", "--", "scripts/eval/")),
    }


def served_ok(requested: str, served: str | None) -> bool:
    # OpenAI は日付つきスナップショット名を返すため前方一致で見る
    return bool(served) and served.startswith(requested)


class Runner:
    def __init__(self, args, providers: dict, lite_text: str, judge_system: str, judge_template: str,
                 out_dir: Path | None):
        self.args = args
        self.providers = providers
        self.lite = lite_text
        self.lite_sha = sha256(lite_text)
        self.judge_system = judge_system
        self.judge_template = judge_template
        self.out_dir = out_dir
        self.lock = threading.Lock()
        self.usage = defaultdict(Counter)   # (role, model) -> tokens
        self.judged: list[dict] = []
        self.outcomes: list[dict] = []      # 全試行の status（error も含む）

    # ---- ログ ----
    def _append(self, name: str, row: dict):
        if not self.out_dir:
            return
        with self.lock, open(self.out_dir / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def _add_usage(self, role: str, model: str, usage: dict):
        with self.lock:
            c = self.usage[(role, model)]
            c["calls"] += 1
            for k, v in usage.items():
                c[k] += v or 0

    def _redact(self, messages: list[dict]) -> list[dict]:
        marker = LITE_MARKER % self.lite_sha
        out = []
        for m in messages:
            c = m["content"]
            if isinstance(c, str):
                c = c.replace(self.lite, marker)
            else:
                c = "(assistant content blocks)"
            out.append({"role": m["role"], "content": c})
        return out

    def sampling(self, provider: str) -> dict:
        s = {}
        if self.args.temperature is not None:
            s["temperature"] = self.args.temperature
        if provider == "anthropic" and self.args.effort:
            s["effort"] = self.args.effort
        if provider == "openai" and self.args.reasoning_effort:
            s["reasoning_effort"] = self.args.reasoning_effort
        return s

    # ---- 1試行 ----
    def run_trial(self, case: cp.Case, model_spec: str, condition: str, trial: int) -> None:
        provider_name, model = resolve_provider(model_spec)
        prov = self.providers[provider_name]
        a = self.args
        key = {"case_id": case.id, "model": model, "provider": provider_name,
               "condition": condition, "trial": trial}
        label = f"{case.id}/{model}/{condition}/#{trial}"
        sampling = self.sampling(provider_name)
        messages = cp.build_messages(case, condition, self.lite, a.framework_mode)
        cache_first = condition == "framework"
        ack = None
        try:
            if condition == "framework" and a.framework_mode == "two-turn-live":
                r0, _, _ = with_retries(
                    lambda: prov.generate(model, messages, a.max_tokens, sampling, cache_first),
                    a.max_attempts, label + "/ack")
                self._add_usage("subject", model, r0["usage"])
                ack = r0["text"]
                messages = messages + [
                    {"role": "assistant", "content": r0["_assistant_content"]},
                    {"role": "user", "content": case.question},
                ]
            res, attempts, latency = with_retries(
                lambda: prov.generate(model, messages, a.max_tokens, sampling, cache_first),
                a.max_attempts, label)
        except CallFailed as e:
            self._error(key, "subject", e.error_class, str(e), e.attempts)
            return
        except Exception as e:  # 想定外もスコアに混ぜずに記録する
            self._error(key, "subject", type(e).__name__, str(e), 1)
            return

        self._add_usage("subject", model, res["usage"])
        transcript = {
            **key, "framework_mode": a.framework_mode if condition == "framework" else None,
            "request": {"messages": self._redact(messages), "max_tokens": a.max_tokens,
                        "sampling": sampling or "provider default"},
            "ack_generated": ack,
            "response": {k: v for k, v in res.items() if not k.startswith("_")},
            "attempts": attempts, "latency_s": round(latency, 2),
        }
        self._append("transcripts.jsonl", transcript)

        if not served_ok(model, res["served_model"]):
            self._error(key, "subject", "served_model_mismatch",
                        f"requested={model} served={res['served_model']}", attempts)
            return
        with self.lock:
            self.outcomes.append({**key, "status": res["status"]})
        if res["status"] != "ok":
            return  # 打ち切り・拒否は採点せず、別に数える

        rec = self.judge_response(case, res["text"], key)
        if rec is not None:
            rec["output_tokens"] = res["usage"]["output_tokens"]
            with self.lock:
                self.judged.append(rec)

    def _error(self, key: dict, stage: str, cls: str, msg: str, attempts: int):
        row = {**key, "stage": stage, "error_class": cls, "message": msg[:1000], "attempts": attempts}
        self._append("errors.jsonl", row)
        with self.lock:
            self.outcomes.append({**key, "status": "error" if stage == "subject" else "judge_error"})
        print(f"  [error] {key.get('case_id')}/{key.get('model')}/{key.get('condition')}: {cls} {msg[:200]}",
              file=sys.stderr)

    # ---- 採点 ----
    def judge_response(self, case: cp.Case, text: str, key: dict) -> dict | None:
        a = self.args
        jp, jm = resolve_provider(a.judge_model)
        prov = self.providers[jp]
        user = cp.render_judge_user(self.judge_template, case, text)
        schema = cp.judge_schema(case)
        repeats = []
        for r in range(a.judge_repeats):
            parsed = None
            for _ in range(2):  # 項目の漏れ・重複があれば1回だけ採点し直す
                try:
                    out, _, _ = with_retries(
                        lambda: prov.judge(jm, self.judge_system, user, schema, a.judge_max_tokens,
                                           a.judge_effort if jp == "anthropic" else None),
                        a.max_attempts, f"judge {key.get('case_id')}")
                except (CallFailed, json.JSONDecodeError, StopIteration) as e:
                    self._error(key, "judge", getattr(e, "error_class", type(e).__name__), str(e), 1)
                    return None
                self._add_usage("judge", jm, out["usage"])
                problems = cp.validate_judgement(case, out["parsed"])
                if not problems:
                    parsed = out["parsed"]
                    break
            if parsed is None:
                self._error(key, "judge", "judge_incomplete", "; ".join(problems), 2)
                return None
            checks = {}
            for item in parsed["ng_items"] + parsed["points"]:
                checks[item["id"]] = cp.verify_evidence(item["evidence"], text)
            repeats.append({"judgement": parsed, "score": cp.score_judgement(parsed),
                            "evidence_verified": checks, "served_model": out["served_model"]})
        return {**key, "judge_model": jm, "repeats": repeats, "score": aggregate_repeats(repeats)}


def aggregate_repeats(repeats: list[dict]) -> dict:
    """ジャッジを複数回走らせた場合は、合否を多数決（同数なら不合格）、充足率を平均にする。"""
    scores = [r["score"] for r in repeats]
    passes = sum(s["pass"] for s in scores)
    ng_counts = Counter(t for s in scores for t in s["triggered"])

    def mean(key):
        vals = [s[key] for s in scores if s[key] is not None]
        return sum(vals) / len(vals) if vals else None

    return {
        "pass": passes * 2 > len(scores),
        "pass_case_ng_only": sum(s["pass_case_ng_only"] for s in scores) * 2 > len(scores),
        "judge_agreement": len({s["pass"] for s in scores}) == 1,
        "triggered": sorted(t for t, c in ng_counts.items() if c * 2 > len(scores)),
        "coverage_strict": mean("coverage_strict"),
        "coverage_lenient": mean("coverage_lenient"),
        "response_type": Counter(s["response_type"] for s in scores).most_common(1)[0][0],
        "fail_reasons": scores[0]["fail_reasons"],
    }


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------
def bootstrap_ci(per_case: dict[str, list[float]], resamples: int, seed: int,
                 per_case_b: dict[str, list[float]] | None = None) -> tuple[float, float] | None:
    """ケース単位のクラスター・ブートストラップ（95%区間）。

    per_case_b を渡すと、対応のある差（a の平均 − b の平均、共通ケースのみ）の区間を返す。
    """
    cases = sorted(per_case) if per_case_b is None else sorted(set(per_case) & set(per_case_b))
    if len(cases) < 2:
        return None
    rng = random.Random(seed)
    stats = []
    for _ in range(resamples):
        sample = [rng.choice(cases) for _ in cases]
        if per_case_b is None:
            vals = [v for c in sample for v in per_case[c]]
            stats.append(sum(vals) / len(vals))
        else:
            diffs = [statistics.mean(per_case[c]) - statistics.mean(per_case_b[c]) for c in sample]
            stats.append(sum(diffs) / len(diffs))
    stats.sort()
    return stats[int(0.025 * resamples)], stats[min(resamples - 1, int(0.975 * resamples))]


def pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def ci_str(ci) -> str:
    return "" if ci is None else f"（{ci[0] * 100:.0f}〜{ci[1] * 100:.0f}%）"


def signed_ci(ci) -> str:
    return "" if ci is None else f"（{ci[0] * 100:+.0f}〜{ci[1] * 100:+.0f}pt）"


def cost_usd(model: str, u: Counter) -> float | None:
    p = PRICES_PER_MTOK.get(model)
    if not p:
        return None
    uncached = u["input_tokens"]  # Anthropic の input_tokens はキャッシュ分を含まない
    return (uncached * p["input"] + u["cache_write_tokens"] * p["input"] * 1.25
            + u["cache_read_tokens"] * p["cache_read"] + u["output_tokens"] * p["output"]) / 1e6


def build_summary(meta: dict, cases: list[cp.Case], models: list[str], conditions: list[str],
                  judged: list[dict], outcomes: list[dict], usage: dict, args) -> str:
    L: list[str] = []
    rs, seed = args.bootstrap_resamples, args.bootstrap_seed
    L.append(f"# 評価結果 {meta['run_id']}\n")
    L.append("## 実行条件\n")
    rows = [
        ("実行日時", meta["executed_at"]),
        ("ケース", f"{len(cases)}件（{', '.join(c.id for c in cases)}）"),
        ("試行回数", f"{args.trials}回／ケース×モデル×条件"),
        ("被評価モデル", ", ".join(models)),
        ("サンプリング", meta["sampling"]),
        ("フレームワークの渡し方", meta["framework_mode_desc"]),
        ("ジャッジ", f"{meta['judge_model']}（effort={meta['judge_effort']}、採点回数={args.judge_repeats}）"),
        ("dist/lite.md", f"commit `{meta['repo']['lite_commit'][:12]}`、sha256 `{meta['lite_sha256'][:12]}`"
                         + ("、**未コミットの変更あり**" if meta['repo']['lite_dirty'] else "")),
        ("judge_prompt.md", f"sha256 `{meta['judge_prompt_sha256'][:12]}`"),
        ("ハーネス", f"v{HARNESS_VERSION}、HEAD `{meta['repo']['head'][:12]}`"
                    + ("、scripts/eval に未コミットの変更あり" if meta['repo']['harness_dirty'] else "")),
        ("SDK", meta["sdk_versions"]),
    ]
    L += ["| 項目 | 値 |", "| --- | --- |"] + [f"| {k} | {v} |" for k, v in rows] + [""]
    if meta["judge_model"] in [resolve_provider(m)[1] for m in models]:
        L.append(f"> 注意: ジャッジ（{meta['judge_model']}）が被評価モデルにも含まれています。"
                 "自己選好バイアスの可能性があるため、別系統のジャッジでの再採点と比べてください。\n")

    L.append("## 合格の定義\n")
    L.append("- **合格** = NG判定（全ケース共通＋ケース固有）に1つも該当せず、応答が質問に向き合っていること。"
             "確認質問だけの応答は、期待する要点のどれかに沿っていれば合格になり得る（判定方針は judge_prompt.md）。")
    L.append("- **要点充足率（厳格）** = met=1、partial=0.5、deferred=0 の平均。**（寛容）** は deferred（確認質問で適切に後回し）も1と数える。"
             "not_applicable の要点は分母から除く。")
    L.append("- API エラー・出力上限での打ち切り・拒否は採点せず、下の表に件数だけ示す（不合格には数えない）。")
    L.append(f"- 区間はケース単位のクラスター・ブートストラップ（{rs}回、seed={seed}）による95%区間。"
             f"ケース数が{len(cases)}件と少ないため幅は広い。\n")

    by = defaultdict(list)
    for j in judged:
        by[(j["model"], j["condition"])].append(j)
    oc = defaultdict(Counter)
    for o in outcomes:
        oc[(o["model"], o["condition"])][o["status"]] += 1

    def per_case(items, key="pass"):
        d = defaultdict(list)
        for j in items:
            d[j["case_id"]].append(float(j["score"][key]))
        return d

    L.append("## モデル×条件ごとの合格率\n")
    L.append("| モデル | 条件 | 採点数 | 合格率（95%区間） | 共通NGを除く合格率 | 要点充足率 厳格／寛容 | 確認質問のみ | 打ち切り／拒否／エラー | 平均出力トークン |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for m in [resolve_provider(x)[1] for x in models]:
        for cond in conditions:
            items = by.get((m, cond), [])
            c = oc[(m, cond)]
            errs = c["error"] + c["judge_error"]
            if not items:
                L.append(f"| {m} | {cond} | 0 | - | - | - | - | {c['truncated']}／{c['refusal']}／{errs} | - |")
                continue
            n = len(items)
            rate = sum(j["score"]["pass"] for j in items) / n
            ci = bootstrap_ci(per_case(items), rs, seed)
            rate2 = sum(j["score"]["pass_case_ng_only"] for j in items) / n
            cs = [j["score"]["coverage_strict"] for j in items if j["score"]["coverage_strict"] is not None]
            cl = [j["score"]["coverage_lenient"] for j in items if j["score"]["coverage_lenient"] is not None]
            clar = sum(j["score"]["response_type"] == "clarifying_only" for j in items) / n
            otok = statistics.mean(j["output_tokens"] for j in items)
            L.append(f"| {m} | {cond} | {n} | {pct(rate)}{ci_str(ci)} | {pct(rate2)} | "
                     f"{pct(statistics.mean(cs) if cs else None)}／{pct(statistics.mean(cl) if cl else None)} | "
                     f"{pct(clar)} | {c['truncated']}／{c['refusal']}／{errs} | {otok:,.0f} |")
    L.append("")

    if set(conditions) == {"baseline", "framework"}:
        L.append("## フレームワークによる差（framework − baseline、同じケースどうしで対応づけ）\n")
        L.append("| モデル | 合格率の差（95%区間） | 要点充足率（厳格）の差 |")
        L.append("| --- | --- | --- |")
        for m in [resolve_provider(x)[1] for x in models]:
            f, b = by.get((m, "framework"), []), by.get((m, "baseline"), [])
            if not f or not b:
                L.append(f"| {m} | - | - |")
                continue
            pf, pb = per_case(f), per_case(b)
            common = sorted(set(pf) & set(pb))
            diff = statistics.mean(statistics.mean(pf[c]) - statistics.mean(pb[c]) for c in common)
            ci = bootstrap_ci(pf, rs, seed, pb)
            cf, cb = per_case(f, "coverage_strict"), per_case(b, "coverage_strict")
            cc = sorted(set(cf) & set(cb))
            cdiff = statistics.mean(statistics.mean(cf[c]) - statistics.mean(cb[c]) for c in cc) if cc else None
            L.append(f"| {m} | {diff * 100:+.0f}pt{signed_ci(ci)} | "
                     f"{'-' if cdiff is None else f'{cdiff * 100:+.0f}pt'} |")
        L.append("")

    L.append("## ケース別の合格数（合格／採点数）\n")
    cols = [(resolve_provider(m)[1], cond) for m in models for cond in conditions]
    L.append("| ケース | " + " | ".join(f"{m}<br>{c}" for m, c in cols) + " |")
    L.append("| --- | " + " | ".join("---" for _ in cols) + " |")
    for case in cases:
        cells = []
        for m, cond in cols:
            items = [j for j in by.get((m, cond), []) if j["case_id"] == case.id]
            cells.append(f"{sum(j['score']['pass'] for j in items)}/{len(items)}" if items else "-")
        L.append(f"| {case.id} {case.title} | " + " | ".join(cells) + " |")
    L.append("")

    L.append("## よく該当したNG判定（全モデル合計の該当回数）\n")
    hits = defaultdict(Counter)
    for j in judged:
        for t in j["score"]["triggered"]:
            hits[(j["case_id"], t)][j["condition"]] += 1
    if hits:
        L.append("| ケース | 項目 | " + " | ".join(conditions) + " |")
        L.append("| --- | --- | " + " | ".join("---" for _ in conditions) + " |")
        case_by_id = {c.id: c for c in cases}
        for (cid, item), cnt in sorted(hits.items(), key=lambda kv: -sum(kv[1].values()))[:30]:
            text = dict(case_by_id[cid].all_ng()).get(item, "").splitlines()[0][:60]
            L.append(f"| {cid} | {item} {text} | " + " | ".join(str(cnt[c]) for c in conditions) + " |")
    else:
        L.append("（該当なし）")
    L.append("")

    L.append("## ジャッジの信頼性の目安\n")
    ev = [v for j in judged for r in j["repeats"] for v in r["evidence_verified"].values() if v is not None]
    if ev:
        L.append(f"- 根拠の引用が回答本文に実在した割合: {pct(sum(ev) / len(ev))}（{len(ev)}件中）。"
                 "低い場合はジャッジが要約を引用として書いている可能性がある。")
    if args.judge_repeats > 1 and judged:
        agree = sum(j["score"]["judge_agreement"] for j in judged) / len(judged)
        L.append(f"- 同じ応答を{args.judge_repeats}回採点して合否が一致した割合: {pct(agree)}")
    L.append("- 人手ラベルとの一致率は未測定。記事に載せる前に、ケースごとに数件を人が採点して照合すること"
             "（README の「限界」を参照）。\n")

    L.append("## トークン使用量\n")
    L.append("| 役割 | モデル | 呼び出し | 入力 | キャッシュ読込 | キャッシュ書込 | 出力 | 概算費用 |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    total = 0.0
    for (role, model), u in sorted(usage.items()):
        c = cost_usd(model, u)
        total += c or 0
        L.append(f"| {role} | {model} | {u['calls']:,} | {u['input_tokens']:,} | {u['cache_read_tokens']:,} | "
                 f"{u['cache_write_tokens']:,} | {u['output_tokens']:,} | {'-' if c is None else f'${c:,.2f}'} |")
    L.append(f"\n概算費用の合計（価格表にあるモデルのみ）: ${total:,.2f}\n")
    L.append("## ファイル\n")
    L.append("- `transcripts.jsonl`: 被評価モデルへの入力と応答（lite.md の本文は sha256 のマーカーに置き換えて保存）")
    L.append("- `judgements.json`: ジャッジの項目別判定・根拠の引用・引用の実在確認")
    L.append("- `errors.jsonl`: 採点に回らなかった試行（API エラー、モデル名の不一致、ジャッジの失敗）")
    L.append("- `run.json`: 実行設定の全体")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# dry-run / self-test
# ---------------------------------------------------------------------------
def estimate_tokens(chars: int) -> int:
    return int(chars / ESTIMATE["chars_per_token"])


def dry_run(args, cases, models, conditions, lite, judge_system, judge_template):
    print(f"== dry-run（API は呼びません）\n")
    print(f"dist/lite.md: {len(lite):,}文字  sha256={sha256(lite)[:12]}  commit={repo_info()['lite_commit'][:12]}")
    print(f"共通NG判定: {len(cases[0].common_ng) if cases else 0}項目")
    for i, t in enumerate(cases[0].common_ng if cases else [], 1):
        print(f"  C{i}: {t}")
    print()
    for m in models:
        p, mid = resolve_provider(m)
        print(f"被評価モデル: {mid}（{p}、キー{'あり' if has_key(p) else 'なし → 実行時はスキップ'}）")
    jp, jm = resolve_provider(args.judge_model)
    print(f"ジャッジ: {jm}（{jp}、キー{'あり' if has_key(jp) else 'なし → 実行不可'}）")
    print(f"条件: {', '.join(conditions)} / フレームワークの渡し方: {args.framework_mode} / 試行: {args.trials}\n")

    in_chars = 0
    judge_chars = 0
    for c in cases:
        print(f"--- [{c.id}] {c.title}  要点{len(c.points)}・固有NG{len(c.ng_items)}（計NG{len(c.all_ng())}）")
        print(f"    質問: {c.question}")
        for cond in conditions:
            msgs = cp.build_messages(c, cond, lite, args.framework_mode)
            if cond == "framework" and args.framework_mode == "two-turn-live":
                msgs = msgs + [{"role": "assistant", "content": "（1通目への実際の応答）"},
                               {"role": "user", "content": c.question}]
            desc = []
            for msg in msgs:
                body = msg["content"]
                if lite in body:
                    head = "<lite.md>" + ("＋質問" if body != lite else "")
                else:
                    head = body[:40].replace("\n", " ") + ("…" if len(body) > 40 else "")
                desc.append(f"{msg['role']}({len(body):,}字): {head}")
            print(f"    {cond:9}: " + " → ".join(desc))
            in_chars += sum(len(m["content"]) for m in msgs) * args.trials * len(models)
        if args.verbose:
            print("    要点:")
            for pid, t in c.all_points():
                print(f"      {pid}: {t.splitlines()[0][:100]}")
            print("    固有NG:")
            for nid, t in c.all_ng()[len(c.common_ng):]:
                print(f"      {nid}: {t[:100]}")
        judge_chars += len(judge_system) + len(cp.render_judge_user(judge_template, c, "x" * 2000))
    print()
    if cases:
        print("== ジャッジへの入力例（1件目、回答部分は仮）")
        sample = cp.render_judge_user(judge_template, cases[0], "（ここに被評価モデルの応答が入る）")
        print(sample if args.verbose else sample[:1200] + ("\n…（--verbose で全文）" if len(sample) > 1200 else ""))
        print()

    n_subject = len(cases) * len(models) * len(conditions) * args.trials
    live_extra = len(cases) * len(models) * args.trials if (
        "framework" in conditions and args.framework_mode == "two-turn-live") else 0
    n_judge = n_subject * args.judge_repeats
    sub_in = estimate_tokens(in_chars)
    sub_out = (n_subject + live_extra) * ESTIMATE["subject_output_tokens"]
    j_in = estimate_tokens(judge_chars) * len(models) * len(conditions) * args.trials * args.judge_repeats
    j_out = n_judge * ESTIMATE["judge_output_tokens"]
    print("== 呼び出し回数と費用の見積もり（文字数からの概算。キャッシュ割引は含めない）")
    print(f"被評価モデル: {n_subject + live_extra}回  入力≈{sub_in:,}  出力≈{sub_out:,}（1回{ESTIMATE['subject_output_tokens']:,}と仮定）")
    print(f"ジャッジ    : {n_judge}回  入力≈{j_in:,}  出力≈{j_out:,}（1回{ESTIMATE['judge_output_tokens']:,}と仮定）")
    print(f"合計        : 入力≈{sub_in + j_in:,}トークン  出力≈{sub_out + j_out:,}トークン")
    jc = PRICES_PER_MTOK.get(jm)
    if jc:
        print(f"ジャッジ分の概算: ${(j_in * jc['input'] + j_out * jc['output']) / 1e6:,.2f}")
    for m in models:
        mid = resolve_provider(m)[1]
        p = PRICES_PER_MTOK.get(mid)
        if p:
            share_in, share_out = sub_in / len(models), sub_out / len(models)
            print(f"{mid} 分の概算: ${(share_in * p['input'] + share_out * p['output']) / 1e6:,.2f}")


def judge_selftest(args, runner: Runner, cases: list[cp.Case]) -> int:
    """既知の不合格例（ありがちな回答・空・「わかりません」）を採点し、ジャッジが落とせるか確かめる。"""
    negatives = [("ありがちな回答", None), ("空の応答", ""), ("わかりません", "わかりません。")]
    rows = []
    for c in cases:
        for label, text in negatives:
            if text is None and not c.typical_answer:
                continue  # 「ありがちな回答」が無いケース
            body = c.typical_answer if text is None else text
            # 「（手残りを計算に入れず…）」のような注記は回答本文ではないので外す
            if text is None and "」（" in body:
                body = body.split("」（", 1)[0] + "」"
            body = cp.strip_quotes(body)
            rec = runner.judge_response(c, body, {"case_id": c.id, "model": "selftest", "condition": label,
                                                  "trial": 0})
            ok = rec is not None and not rec["score"]["pass"]
            rows.append({"case_id": c.id, "input": label, "expected": "fail",
                         "judged_pass": None if rec is None else rec["score"]["pass"],
                         "triggered": None if rec is None else rec["score"]["triggered"], "ok": ok,
                         "record": rec})
            mark = "OK " if ok else "NG!"
            print(f"  {mark} [{c.id}] {label}: 判定={'合格' if rec and rec['score']['pass'] else '不合格'}"
                  f" 該当={rec['score']['triggered'] if rec else 'ジャッジ失敗'}")
    if runner.out_dir:
        (runner.out_dir / "judge_selftest.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    bad = [r for r in rows if not r["ok"]]
    print(f"\n既知の不合格例 {len(rows)}件のうち、ジャッジが正しく不合格にしたもの: {len(rows) - len(bad)}件")
    return 1 if bad else 0


# ---------------------------------------------------------------------------
def parse_args(argv=None):
    D = DEFAULTS
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--models", default=",".join(env_list("EVAL_MODELS", D["models"])),
                   help="被評価モデル（カンマ区切り。anthropic:ID / openai:ID で provider を明示可）")
    p.add_argument("--judge-model", default=os.environ.get("EVAL_JUDGE_MODEL", D["judge_model"]))
    p.add_argument("--judge-effort", default=D["judge_effort"],
                   help="Anthropic ジャッジの effort（low/medium/high/xhigh/max。空文字で既定値）")
    p.add_argument("--judge-repeats", type=int, default=D["judge_repeats"])
    p.add_argument("--conditions", default="baseline,framework")
    p.add_argument("--framework-mode", choices=cp.FRAMEWORK_MODES, default=D["framework_mode"],
                   help="two-turn: lite.md→定型の受け答え→質問（既定）/ two-turn-live: 受け答えも実際に生成 / "
                        "inline: lite.md と質問を同じ最初のメッセージに入れる")
    p.add_argument("--cases", default=None, help="例: 01,07 / 1-3")
    p.add_argument("--trials", type=int, default=D["trials"])
    p.add_argument("--workers", type=int, default=D["workers"])
    p.add_argument("--max-tokens", type=int, default=D["max_tokens"])
    p.add_argument("--judge-max-tokens", type=int, default=D["judge_max_tokens"])
    p.add_argument("--max-attempts", type=int, default=D["max_attempts"])
    p.add_argument("--timeout", type=float, default=D["timeout_s"])
    p.add_argument("--temperature", type=float, default=None,
                   help="指定時のみ送る（Claude Opus 5.5 などは既定値以外を拒否する）。未指定=各 API の既定値")
    p.add_argument("--effort", default=None, help="Anthropic 被評価モデルの effort。未指定=API 既定値")
    p.add_argument("--reasoning-effort", default=None, help="OpenAI 被評価モデルの reasoning_effort。未指定=API 既定値")
    p.add_argument("--out-root", default=str(cp.CASES_DIR / "results"))
    p.add_argument("--bootstrap-resamples", type=int, default=D["bootstrap_resamples"])
    p.add_argument("--bootstrap-seed", type=int, default=D["bootstrap_seed"])
    p.add_argument("--dry-run", action="store_true", help="API を呼ばず、ケースと組み立てたメッセージを表示する")
    p.add_argument("--judge-selftest", action="store_true",
                   help="既知の不合格例をジャッジに採点させ、落とせるかを確認する（被評価モデルは呼ばない）")
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args(argv)
    a.judge_effort = a.judge_effort or None
    if a.trials < 1 or a.workers < 1 or a.judge_repeats < 1:
        p.error("--trials / --workers / --judge-repeats は1以上")
    return a


def main(argv=None) -> int:
    args = parse_args(argv)
    cases = cp.select_cases(cp.load_cases(), args.cases)
    lite = cp.LITE_PATH.read_text(encoding="utf-8")
    judge_system, judge_template = cp.load_judge_prompt()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    for c in conditions:
        if c not in cp.CONDITIONS:
            raise SystemExit(f"未知の条件: {c}（{cp.CONDITIONS}）")
    for m in models + [args.judge_model]:
        resolve_provider(m)

    if args.dry_run:
        dry_run(args, cases, models, conditions, lite, judge_system, judge_template)
        return 0

    jp, jm = resolve_provider(args.judge_model)
    if not has_key(jp):
        raise SystemExit(f"ジャッジ {jm} の {key_env(jp)} が未設定のため実行できません。")
    runnable = []
    for m in models:
        p, mid = resolve_provider(m)
        if has_key(p):
            runnable.append(m)
        else:
            print(f"[skip] {key_env(p)} が未設定のため {mid}（{p}）をスキップします。")
    if not runnable and not args.judge_selftest:
        raise SystemExit("実行できる被評価モデルがありません。")

    needed = {resolve_provider(m)[0] for m in runnable} | {jp}
    providers = {}
    for p in needed:
        providers[p] = (AnthropicProvider(args.timeout) if p == "anthropic"
                        else OpenAIProvider(args.timeout, os.environ.get("OPENAI_BASE_URL", DEFAULTS["openai_base_url"])))

    now = dt.datetime.now().astimezone()
    run_id = f"{now:%Y-%m-%d}-{secrets.token_hex(3)}" + ("-selftest" if args.judge_selftest else "")
    out_dir = Path(args.out_root) / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    runner = Runner(args, providers, lite, judge_system, judge_template, out_dir)

    meta = {
        "run_id": run_id, "executed_at": now.isoformat(timespec="seconds"),
        "harness_version": HARNESS_VERSION, "argv": sys.argv[1:],
        "args": vars(args), "models": [resolve_provider(m)[1] for m in runnable],
        "skipped_models": [m for m in models if m not in runnable],
        "judge_model": jm, "judge_effort": args.judge_effort if jp == "anthropic" else "n/a",
        "lite_sha256": sha256(lite), "judge_prompt_sha256": sha256(cp.JUDGE_PROMPT_PATH.read_text(encoding="utf-8")),
        "repo": repo_info(), "cases": [c.slug for c in cases],
        "sampling": (f"temperature={args.temperature}" if args.temperature is not None else "各 API の既定値（temperature 等は送らない）")
                    + (f"、effort={args.effort}" if args.effort else "")
                    + (f"、reasoning_effort={args.reasoning_effort}" if args.reasoning_effort else "")
                    + f"、max_tokens={args.max_tokens}",
        "framework_mode_desc": {
            "two-turn": "2往復（lite.md → 定型の受け答え「" + cp.FRAMEWORK_ACK + "」 → 質問）",
            "two-turn-live": "2往復（lite.md → 実際に生成した受け答え → 質問）",
            "inline": "lite.md と質問を同じ最初のメッセージに入れる",
        }[args.framework_mode],
        "sdk_versions": ", ".join(f"{p}: {prov.version()}" for p, prov in providers.items())
                        + f", Python {platform.python_version()}",
    }
    (out_dir / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"出力先: {out_dir}")

    if args.judge_selftest:
        rc = judge_selftest(args, runner, cases)
        print_usage(runner.usage)
        return rc

    tasks = [(c, m, cond, t) for c in cases for m in runnable for cond in conditions for t in range(1, args.trials + 1)]
    random.Random(DEFAULTS["shuffle_seed"]).shuffle(tasks)
    print(f"{len(tasks)}試行を {args.workers} 並列で実行します。")
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(runner.run_trial, *t) for t in tasks]
            for f in as_completed(futs):
                f.result()
                done += 1
                if done % 10 == 0 or done == len(tasks):
                    print(f"  {done}/{len(tasks)}")
    except KeyboardInterrupt:
        print("中断しました。ここまでの結果を保存します。", file=sys.stderr)
    finally:
        judged = sorted(runner.judged, key=lambda j: (j["case_id"], j["model"], j["condition"], j["trial"]))
        (out_dir / "judgements.json").write_text(
            json.dumps({"meta": meta, "judgements": judged}, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        summary = build_summary(meta, cases, runnable, conditions, judged, runner.outcomes, runner.usage, args)
        (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    print(f"\n集計: {out_dir / 'summary.md'}")
    print_usage(runner.usage)
    return 0


def print_usage(usage: dict):
    print("\n== トークン使用量（費用の目安）")
    tin = tout = 0
    total = 0.0
    for (role, model), u in sorted(usage.items()):
        c = cost_usd(model, u)
        total += c or 0
        tin += u["input_tokens"] + u["cache_read_tokens"] + u["cache_write_tokens"]
        tout += u["output_tokens"]
        print(f"  {role:7} {model:22} 呼出{u['calls']:>4}  入力{u['input_tokens']:>10,}"
              f"（キャッシュ読込{u['cache_read_tokens']:,}・書込{u['cache_write_tokens']:,}）  出力{u['output_tokens']:>10,}"
              f"  {'' if c is None else f'≈${c:,.2f}'}")
    print(f"  合計 入力{tin:,} / 出力{tout:,} トークン、価格表にあるモデル分の概算 ${total:,.2f}")


if __name__ == "__main__":
    sys.exit(main())
