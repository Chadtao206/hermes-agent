#!/usr/bin/env python3
from __future__ import annotations

from math import sqrt


def safe_div(numerator: float | int, denominator: float | int) -> float | None:
    if not denominator:
        return None
    return float(numerator) / float(denominator)


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if total <= 0:
        return (None, None)
    p = successes / total
    denom = 1.0 + (z * z) / total
    center = (p + (z * z) / (2.0 * total)) / denom
    margin = (z / denom) * sqrt((p * (1.0 - p) / total) + ((z * z) / (4.0 * total * total)))
    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    return (low, high)


def confidence_label(denominator: int, contaminated: bool = False) -> str:
    if contaminated or denominator < 3:
        return 'insufficient'
    if denominator >= 20:
        return 'high'
    if denominator >= 8:
        return 'medium'
    return 'low'


def aggregate_proportion(rows: list[dict], numerator_key: str, denominator_key: str) -> tuple[int, int, float | None]:
    numerator = sum(int(row.get(numerator_key) or 0) for row in rows)
    denominator = sum(int(row.get(denominator_key) or 0) for row in rows)
    return numerator, denominator, safe_div(numerator, denominator)


def aggregate_mean(rows: list[dict], total_key: str, count_key: str) -> tuple[float, int, float | None]:
    total = sum(float(row.get(total_key) or 0.0) for row in rows)
    count = sum(int(row.get(count_key) or 0) for row in rows)
    return total, count, safe_div(total, count)
