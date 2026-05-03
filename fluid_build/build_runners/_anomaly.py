# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Anomaly detection for ingestion runs.

Algorithms:
- ``ewma`` — exponentially weighted moving average ± k·σ.
- ``iqr`` — interquartile range outlier (k·IQR beyond Q1/Q3).
- ``exact`` — value equality / inequality (e.g. fingerprint changed).

A signal fires when the observed metric is outside the algorithm's
threshold; the result includes the anomaly score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from fluid_build.api.quality import AnomalyResult, AnomalySignal, Severity


@dataclass(frozen=True)
class EwmaState:
    mean: float
    variance: float


def ewma_update(state: Optional[EwmaState], x: float, alpha: float = 0.3) -> EwmaState:
    if state is None:
        return EwmaState(mean=x, variance=0.0)
    delta = x - state.mean
    new_mean = state.mean + alpha * delta
    new_var = (1 - alpha) * (state.variance + alpha * delta * delta)
    return EwmaState(mean=new_mean, variance=new_var)


def ewma_z_score(state: EwmaState, x: float) -> float:
    sd = math.sqrt(state.variance) if state.variance > 0 else 0.0
    if sd == 0:
        return 0.0
    return abs(x - state.mean) / sd


def iqr_score(history: Sequence[float], x: float) -> float:
    if len(history) < 4:
        return 0.0
    sorted_h = sorted(history)
    n = len(sorted_h)
    q1 = sorted_h[n // 4]
    q3 = sorted_h[(3 * n) // 4]
    iqr = q3 - q1
    if iqr == 0:
        return 0.0
    if x < q1:
        return (q1 - x) / iqr
    if x > q3:
        return (x - q3) / iqr
    return 0.0


def exact_score(prior: object, current: object) -> float:
    return 0.0 if prior == current else 1.0


def detect(
    signal: AnomalySignal,
    *,
    algorithm: str,
    sensitivity: float,
    severity: Severity,
    current: float = 0.0,
    history: Optional[Sequence[float]] = None,
    ewma_state: Optional[EwmaState] = None,
    prior_value: Optional[object] = None,
    current_value: Optional[object] = None,
) -> Optional[AnomalyResult]:
    """Run a single signal detector. Returns ``AnomalyResult`` if the signal
    fires (score > 0 and exceeds sensitivity threshold), else ``None``.
    """
    if algorithm == "ewma":
        if ewma_state is None:
            return None
        score = ewma_z_score(ewma_state, current)
        threshold = sensitivity
        if score > threshold:
            return AnomalyResult(
                signal=signal,
                score=score,
                severity=severity,
                threshold=threshold,
                details={"algorithm": "ewma", "current": current},
            )
        return None
    if algorithm == "iqr":
        if not history:
            return None
        score = iqr_score(history, current)
        threshold = sensitivity
        if score > threshold:
            return AnomalyResult(
                signal=signal,
                score=score,
                severity=severity,
                threshold=threshold,
                details={"algorithm": "iqr", "current": current},
            )
        return None
    if algorithm == "exact":
        score = exact_score(prior_value, current_value)
        if score > 0:
            return AnomalyResult(
                signal=signal,
                score=score,
                severity=severity,
                threshold=0.0,
                details={"algorithm": "exact", "prior": prior_value, "current": current_value},
            )
        return None
    return None
