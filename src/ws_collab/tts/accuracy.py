"""TTS transcription accuracy metrics (task section 14).

Given known TTS output text (the reference) and what the STT pipeline produced,
compute WER, CER, word accuracy, normalized exact match, and the counts of
insertions/deletions/substitutions with word-level diffs. Metrics are reported
per engine and for the final resolved transcript, including whether the final
disambiguator improved or regressed relative to the best single engine. Semantic
similarity is offered only as a clearly-labelled secondary metric, never as the
sole measure of accuracy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..stt.base import normalize_text


@dataclass
class EditResult:
    substitutions: int
    deletions: int
    insertions: int
    ops: list[dict[str, str]]
    ref_len: int
    hyp_len: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions


def _edit_align(ref: list[str], hyp: list[str]) -> EditResult:
    """Levenshtein alignment with backtrace, returning S/D/I and diff ops."""

    n, m = len(ref), len(hyp)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = i
    for j in range(1, m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j - 1], dp[i - 1][j], dp[i][j - 1])

    i, j = n, m
    subs = dels = ins = 0
    ops: list[dict[str, str]] = []
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref[i - 1] == hyp[j - 1]:
            ops.append({"op": "equal", "ref": ref[i - 1], "hyp": hyp[j - 1]})
            i, j = i - 1, j - 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            ops.append({"op": "sub", "ref": ref[i - 1], "hyp": hyp[j - 1]})
            subs += 1
            i, j = i - 1, j - 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            ops.append({"op": "del", "ref": ref[i - 1], "hyp": ""})
            dels += 1
            i -= 1
        else:
            ops.append({"op": "ins", "ref": "", "hyp": hyp[j - 1]})
            ins += 1
            j -= 1
    ops.reverse()
    return EditResult(substitutions=subs, deletions=dels, insertions=ins, ops=ops, ref_len=n, hyp_len=m)


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref = normalize_text(reference).split()
    hyp = normalize_text(hypothesis).split()
    if not ref:
        return 0.0 if not hyp else 1.0
    return _edit_align(ref, hyp).errors / len(ref)


def character_error_rate(reference: str, hypothesis: str) -> float:
    ref = list(normalize_text(reference).replace(" ", ""))
    hyp = list(normalize_text(hypothesis).replace(" ", ""))
    if not ref:
        return 0.0 if not hyp else 1.0
    return _edit_align(ref, hyp).errors / len(ref)


def _semantic_similarity(reference: str, hypothesis: str) -> float:
    """Secondary metric only: token Jaccard overlap. Never used as sole measure."""

    ref = set(normalize_text(reference).split())
    hyp = set(normalize_text(hypothesis).split())
    if not ref and not hyp:
        return 1.0
    if not ref or not hyp:
        return 0.0
    return len(ref & hyp) / len(ref | hyp)


def evaluate(reference: str, hypothesis: str, *, latency_ms: float = 0.0) -> dict[str, Any]:
    ref_tokens = normalize_text(reference).split()
    hyp_tokens = normalize_text(hypothesis).split()
    alignment = _edit_align(ref_tokens, hyp_tokens)
    ref_len = len(ref_tokens) or 1
    wer = alignment.errors / ref_len
    correct = sum(1 for op in alignment.ops if op["op"] == "equal")
    missing = [op["ref"] for op in alignment.ops if op["op"] in ("del", "sub")]
    return {
        "wer": round(wer, 4),
        "cer": round(character_error_rate(reference, hypothesis), 4),
        "word_accuracy": round(correct / ref_len, 4),
        "exact_match": normalize_text(reference) == normalize_text(hypothesis),
        "substitutions": alignment.substitutions,
        "deletions": alignment.deletions,
        "insertions": alignment.insertions,
        "missing_words": missing,
        "latency_ms": round(latency_ms, 2),
        "semantic_similarity_secondary": round(_semantic_similarity(reference, hypothesis), 4),
        "reference_normalized": normalize_text(reference),
        "hypothesis_normalized": normalize_text(hypothesis),
        "diff": alignment.ops,
    }


def evaluate_pipeline(
    reference: str,
    engine_hypotheses: dict[str, str],
    final_text: str,
    *,
    engine_latencies: dict[str, float] | None = None,
    final_latency_ms: float = 0.0,
) -> dict[str, Any]:
    """Evaluate each engine and the final transcript, plus final improvement."""

    engine_latencies = engine_latencies or {}
    per_engine = {
        name: evaluate(reference, text, latency_ms=engine_latencies.get(name, 0.0))
        for name, text in engine_hypotheses.items()
    }
    final = evaluate(reference, final_text, latency_ms=final_latency_ms)
    best_engine_wer = min((metrics["wer"] for metrics in per_engine.values()), default=1.0)
    improvement = round(best_engine_wer - final["wer"], 4)
    return {
        "reference": reference,
        "per_engine": per_engine,
        "final": final,
        "best_engine_wer": round(best_engine_wer, 4),
        "final_improvement_vs_best_engine": improvement,
        "final_regressed": improvement < 0,
    }


@dataclass
class AccuracyAccumulator:
    """Rolling accuracy grouped by an arbitrary condition key."""

    groups: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, group: str, metrics: dict[str, Any], example: dict[str, Any] | None = None) -> None:
        bucket = self.groups.setdefault(
            group,
            {"count": 0, "wer_sum": 0.0, "cer_sum": 0.0, "worst": None},
        )
        bucket["count"] += 1
        bucket["wer_sum"] += metrics.get("wer", 0.0)
        bucket["cer_sum"] += metrics.get("cer", 0.0)
        worst = bucket["worst"]
        if example is not None and (worst is None or metrics.get("wer", 0.0) > worst.get("wer", 0.0)):
            bucket["worst"] = {**example, "wer": metrics.get("wer", 0.0)}

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for group, bucket in self.groups.items():
            count = max(1, bucket["count"])
            result[group] = {
                "count": bucket["count"],
                "avg_wer": round(bucket["wer_sum"] / count, 4),
                "avg_cer": round(bucket["cer_sum"] / count, 4),
                "worst_example": bucket["worst"],
            }
        return result
