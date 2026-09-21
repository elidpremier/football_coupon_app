"""Tests : métriques de performance (Brier, log loss, calibration)."""
from __future__ import annotations

import pytest

from football.metrics import (
    CalibRecord,
    brier_score,
    calibration_bins,
    hit_rate,
    log_loss,
    per_market,
    summarize,
)


@pytest.fixture
def records():
    return [
        CalibRecord(0.60, 1),
        CalibRecord(0.60, 1),
        CalibRecord(0.40, 0),
        CalibRecord(0.40, 1),
        CalibRecord(0.90, 1),
        CalibRecord(0.20, 0),
    ]


class TestBrier:
    def test_perfect_predictions_zero(self):
        recs = [CalibRecord(1.0 - 1e-9, 1), CalibRecord(1e-9, 0)]
        assert brier_score(recs) < 1e-8

    def test_known_value(self):
        recs = [CalibRecord(0.5, 1), CalibRecord(0.5, 0)]
        assert brier_score(recs) == pytest.approx(0.25)

    def test_empty_none(self):
        assert brier_score([]) is None

    def test_invalid_probability_rejected(self):
        with pytest.raises(ValueError):
            brier_score([CalibRecord(1.5, 1)])
        with pytest.raises(ValueError):
            brier_score([CalibRecord(0.0, 1)])

    def test_invalid_observed_rejected(self):
        with pytest.raises(ValueError):
            brier_score([CalibRecord(0.5, 2)])


class TestLogLoss:
    def test_empty_none(self):
        assert log_loss([]) is None

    def test_positive(self):
        assert log_loss([CalibRecord(0.5, 1)]) > 0

    def test_confident_correct_is_lower(self):
        confident = log_loss([CalibRecord(0.95, 1)])
        hesitant = log_loss([CalibRecord(0.51, 1)])
        assert confident < hesitant

    def test_epsilon_avoids_log_zero(self):
        # 0.0 est rejeté, mais près de 0 reste fini
        v = log_loss([CalibRecord(1e-9, 0)])
        assert v is not None and v < 100


class TestHitRate:
    def test_mixed(self, records):
        # 0.6→1 ✓, 0.6→1 ✓, 0.4→0 ✓, 0.4→1 ✗, 0.9→1 ✓, 0.2→0 ✓
        assert hit_rate(records) == pytest.approx(5 / 6)

    def test_empty_none(self):
        assert hit_rate([]) is None


class TestCalibration:
    def test_bins_structure(self, records):
        bins = calibration_bins(records, bins=10)
        assert len(bins) == 10
        total_n = sum(b["n"] for b in bins)
        assert total_n == len(records)

    def test_bin_hit_rate_matches(self, records):
        bins = calibration_bins(records, bins=10)
        b = bins[int(0.60 * 10)]  # tranche 0.6-0.7
        assert b["n"] == 2
        assert b["hit_rate"] == pytest.approx(1.0)

    def test_empty(self):
        assert calibration_bins([]) == []


class TestPerMarketAndSummarize:
    def test_per_market(self, records):
        out = per_market({"match_winner": records[:4],
                          "over_under_2_5": records[4:]})
        assert out["match_winner"]["n"] == 4
        assert out["over_under_2_5"]["n"] == 2
        assert out["match_winner"]["brier"] is not None

    def test_summarize_keys(self, records):
        s = summarize(records)
        assert {"n", "brier", "log_loss", "hit_rate", "calibration"} <= set(s)
        assert s["n"] == 6
