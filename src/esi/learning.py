"""Masked action-specific ridge recursive least squares; no LLM teachers."""

import numpy as np

from esi.models import ActionKind

DIMENSIONS = 15
PRIORS = {
    ActionKind.WAIT: [0.15, 0, 0],
    ActionKind.REPROCESS: [0.05, 0.15, 0],
    ActionKind.THINK: [0, 0.2, 0],
    ActionKind.INQUIRE: [0, 0.45, 0],
    ActionKind.SPEAK: [0, 0.05, 0],
    ActionKind.REST: [0.5, 0, 0],
}


class Predictor:
    def __init__(self, ridge=1):
        self.weights = {a.value: np.zeros((3, DIMENSIONS)) for a in ActionKind}
        self.covariance = {
            a.value: np.repeat((np.eye(DIMENSIONS) / ridge)[None], 3, axis=0) for a in ActionKind
        }
        self.counts = {a.value: [0, 0, 0] for a in ActionKind}
        for a in ActionKind:
            self.weights[a.value][:, 0] = PRIORS[a]

    def predict(self, action: ActionKind, features):
        return self.weights[action.value] @ np.asarray(features, dtype=float)

    def update(self, action: ActionKind, features, values):
        x = np.asarray(features, dtype=float)
        if x.shape != (DIMENSIONS,) or not np.isfinite(x).all():
            raise ValueError("invalid features")
        changes = []
        for dimension, target in enumerate(values):
            if target is None:
                continue
            p = self.covariance[action.value][dimension]
            w = self.weights[action.value][dimension]
            prediction = float(w @ x)
            gain = p @ x / (1 + x @ p @ x)
            self.weights[action.value][dimension] = w + gain * (target - prediction)
            self.covariance[action.value][dimension] = p - np.outer(gain, x @ p)
            self.counts[action.value][dimension] += 1
            changes.append(
                {
                    "dimension": dimension,
                    "target": target,
                    "before": prediction,
                    "error": target - prediction,
                    "after": float(self.weights[action.value][dimension] @ x),
                }
            )
        return changes

    def dump(self):
        return {
            "weights": {k: v.tolist() for k, v in self.weights.items()},
            "covariance": {k: v.tolist() for k, v in self.covariance.items()},
            "counts": {k: list(v) for k, v in self.counts.items()},
        }

    def restore(self, data):
        self.weights = {k: np.asarray(v, dtype=float) for k, v in data["weights"].items()}
        self.covariance = {k: np.asarray(v, dtype=float) for k, v in data["covariance"].items()}
        self.counts = {k: list(v) for k, v in data["counts"].items()}
        if set(self.weights) != {a.value for a in ActionKind}:
            raise ValueError("invalid predictor action set")
        for a in ActionKind:
            if self.weights[a.value].shape != (3, DIMENSIONS) or self.covariance[a.value].shape != (
                3,
                DIMENSIONS,
                DIMENSIONS,
            ):
                raise ValueError("invalid predictor snapshot shape")
            if (
                not np.isfinite(self.weights[a.value]).all()
                or not np.isfinite(self.covariance[a.value]).all()
            ):
                raise ValueError("non-finite predictor snapshot")
