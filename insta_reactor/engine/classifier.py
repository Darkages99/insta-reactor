"""Offline emotion classifier — the ML half of the hybrid brain.

The rule/emoji dictionary (`normalize.signals_for_comment`) is fast, offline,
and unbeatable on meme buckets like DEAD (💀) and FIRE (🔥) — but it only knows
the ~150 words we hand-wrote, so it misses the long tail of free-text comments.
This module adds a pretrained, *fully offline* emotion model to cover that tail
without hand-labelling anything.

Design
------
- `EmotionClassifier` is the seam: `classify_batch(texts) -> list[dict]`, one
  emotion->probability distribution per input comment (over our 7 buckets).
- `ModelClassifier` wraps a GoEmotions RoBERTa model via 🤗 transformers. It is
  imported lazily and only when `Settings.use_model` is on, so the deterministic
  core (and the whole test suite) never needs torch/transformers installed.
- If the model or its dependencies can't be loaded, `build_model` returns None
  and the pipeline silently falls back to rules-only — nothing crashes.

GoEmotions has 28 fine-grained labels; we fold them onto our buckets. DEAD has
no natural GoEmotions class (it's an internet-culture reaction, not a feeling),
so it is deliberately left to the rule layer.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ..models import Emotion

log = logging.getLogger("insta_reactor.classifier")


# GoEmotions label -> our canonical bucket. Unmapped labels (neutral, curiosity,
# embarrassment, realization, relief, disapproval-as-neutral, ...) contribute
# nothing, which is what we want. DEAD is intentionally absent.
GOEMOTION_TO_BUCKET: dict[str, str] = {
    "amusement": Emotion.LAUGH,
    "joy": Emotion.LAUGH,
    "love": Emotion.LOVE,
    "admiration": Emotion.LOVE,
    "caring": Emotion.LOVE,
    "gratitude": Emotion.LOVE,
    "surprise": Emotion.SHOCK,
    "fear": Emotion.SHOCK,
    "confusion": Emotion.SHOCK,
    "nervousness": Emotion.SHOCK,
    "sadness": Emotion.CRYING,
    "grief": Emotion.CRYING,
    "disappointment": Emotion.CRYING,
    "remorse": Emotion.CRYING,
    "anger": Emotion.ANGRY,
    "annoyance": Emotion.ANGRY,
    "disgust": Emotion.ANGRY,
    "disapproval": Emotion.ANGRY,
    "excitement": Emotion.FIRE,
    "pride": Emotion.FIRE,
    "approval": Emotion.FIRE,
    "desire": Emotion.FIRE,
    "optimism": Emotion.FIRE,
}


def _normalize(d: dict[str, float]) -> dict[str, float]:
    total = sum(d.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in d.items()}


class EmotionClassifier(Protocol):
    def classify_batch(self, texts: list[str]) -> list[dict[str, float]]:
        """Return one {bucket: probability} distribution per input text."""
        ...


class ModelClassifier:
    """GoEmotions RoBERTa, run offline on CPU. Lazy-loaded on first use."""

    def __init__(self, model_name: str, min_label_score: float = 0.10):
        self.model_name = model_name
        self.min_label_score = min_label_score
        self._pipe = None  # built on first classify_batch

    def _ensure_pipe(self) -> bool:
        if self._pipe is not None:
            return True
        try:  # heavy, optional deps — only imported when the model is enabled
            from transformers import pipeline
        except Exception as e:  # pragma: no cover - env dependent
            log.warning("transformers unavailable (%s); model disabled", e)
            return False
        try:
            # top_k=None => return scores for every label (multi-label sigmoid).
            self._pipe = pipeline(
                "text-classification", model=self.model_name, top_k=None
            )
        except Exception as e:  # pragma: no cover - env dependent
            log.warning("could not load %r (%s); model disabled",
                        self.model_name, e)
            return False
        return True

    def classify_batch(self, texts: list[str]) -> list[dict[str, float]]:
        if not texts or not self._ensure_pipe():
            return [{} for _ in texts]
        try:
            raw = self._pipe(texts, truncation=True)
        except Exception as e:  # pragma: no cover - runtime robustness
            log.warning("inference failed (%s); returning empty signal", e)
            return [{} for _ in texts]

        out: list[dict[str, float]] = []
        for row in raw:
            bucket: dict[str, float] = {}
            for item in row:
                score = float(item.get("score", 0.0))
                if score < self.min_label_score:
                    continue
                target = GOEMOTION_TO_BUCKET.get(item.get("label", ""))
                if target:
                    bucket[target] = bucket.get(target, 0.0) + score
            out.append(_normalize(bucket))
        return out


def build_model(settings) -> EmotionClassifier | None:
    """Return a model classifier if enabled+loadable, else None (rules only)."""
    if not getattr(settings, "use_model", False):
        return None
    weight = getattr(settings, "ensemble_model_weight", 0.0)
    if weight <= 0:
        return None
    return ModelClassifier(getattr(settings, "model_name",
                                   "SamLowe/roberta-base-go_emotions"))
