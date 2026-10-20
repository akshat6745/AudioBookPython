"""Ensemble label math from tag_novel.tag_rows, without loading a model.

``prob_sums[head][row]`` is the sum of per-model softmax rows. The label is the argmax
of that sum. Confidence is ``max(sum) / n_models``, rounded to 3 decimals, matching
``tag_rows`` (not the average of each model's own max).
"""

from __future__ import annotations

HEADS = {
    "emotion": [
        "neutral", "affection", "amusement", "anger", "awe", "bitterness", "confusion", "contemplation",
        "contentment", "determination", "disgust", "elation", "enthusiasm", "fear", "helplessness",
        "longing", "pride", "relief", "sadness", "shame", "surprise",
    ],
    "style": ["none", "whispering", "shouting"],
    "sfx": ["none", "laughter", "sigh", "crying", "sniff", "screaming", "cough"],
}


def labels_from_sums(prob_sums: dict[str, list[list[float]]], n_models: int, heads: dict[str, list[str]] | None = None) -> list[dict]:
    if n_models < 1:
        raise ValueError("n_models must be positive")
    heads = heads or HEADS
    count = len(next(iter(prob_sums.values())))
    out = []
    for index in range(count):
        labels = {}
        confidences = {}
        for head, names in heads.items():
            row = prob_sums[head][index]
            winner = max(range(len(row)), key=lambda i: row[i])
            labels[head] = names[winner]
            confidences[head] = round(float(row[winner]) / n_models, 3)
        out.append({
            "emotion": labels["emotion"],
            "style": labels["style"],
            "sfx": labels["sfx"],
            "confidence": confidences["emotion"],
            "style_conf": confidences["style"],
            "sfx_conf": confidences["sfx"],
        })
    return out


def add_softmax(total: dict[str, list[list[float]]] | None, probs: dict[str, list[list[float]]]) -> dict[str, list[list[float]]]:
    if total is None:
        return {head: [list(row) for row in rows] for head, rows in probs.items()}
    out = {}
    for head, rows in probs.items():
        out[head] = [[a + b for a, b in zip(left, right)] for left, right in zip(total[head], rows)]
    return out
