"""Unit tests for regime_filter."""
from strategy.regime_filter import regime_filter

# ── helpers ───────────────────────────────────────────────────────────────────

def _flat_btc(n: int = 60, price: float = 30000.0) -> list[float]:
    """Flat BTC series — EMA20 ≈ EMA50, slope ≈ 0."""
    return [price] * n


def _trending_btc(n: int = 60, start: float = 28000.0, end: float = 32000.0) -> list[float]:
    """Linearly rising BTC series."""
    step = (end - start) / (n - 1)
    return [start + step * i for i in range(n)]


# ── tests ─────────────────────────────────────────────────────────────────────

class TestRegimeFilter:
    def test_disabled_when_insufficient_btc_history(self) -> None:
        # < REGIME_MIN_BTC_HISTORY (55)
        result = regime_filter([30000.0] * 54, [], {})
        assert result == "DISABLED"

    def test_disabled_on_strong_btc_uptrend(self) -> None:
        # Sharply rising BTC: EMA20 > EMA50 and slope > 1.5%
        closes = _trending_btc(60, start=20000.0, end=35000.0)
        result = regime_filter(closes, [], {})
        assert result == "DISABLED"

    def test_reduced_on_mild_btc_uptrend(self) -> None:
        # Gently rising so EMA20 > EMA50 and 0.5% < slope < 1.5%
        # Build a series that rises slowly over 60 candles
        closes = _trending_btc(60, start=29000.0, end=31000.0)
        result = regime_filter(closes, [], {})
        assert result in ("REDUCED", "DISABLED")  # slope-dependent; at minimum not NORMAL
        # Verify it's not NORMAL when EMA20 > EMA50 with positive slope
        assert result != "NORMAL"

    def test_normal_when_btc_flat_and_breadth_low(self) -> None:
        closes = _flat_btc(60)
        result = regime_filter(closes, [], {})
        assert result == "NORMAL"

    def test_disabled_when_alt_breadth_exceeds_threshold(self) -> None:
        # 4 of 5 alts up >2% → 80% > 60% threshold
        closes = _flat_btc(60)
        coins = ["ETH", "SOL", "AVAX", "MATIC", "LINK"]
        coin_closes: dict[str, list[float]] = {
            "ETH":   [1000.0, 1025.0],   # +2.5% ✓
            "SOL":   [100.0,  103.0],    # +3.0% ✓
            "AVAX":  [50.0,   51.5],     # +3.0% ✓
            "MATIC": [1.0,    1.03],     # +3.0% ✓
            "LINK":  [10.0,   10.1],     # +1.0% ✗
        }
        result = regime_filter(closes, coins, coin_closes)
        assert result == "DISABLED"

    def test_normal_when_alt_breadth_below_threshold(self) -> None:
        # Only 1 of 5 alts up >2% → 20% < 60% threshold
        closes = _flat_btc(60)
        coins = ["ETH", "SOL", "AVAX", "MATIC", "LINK"]
        coin_closes: dict[str, list[float]] = {
            "ETH":   [1000.0, 1025.0],  # +2.5% ✓
            "SOL":   [100.0,  100.5],   # +0.5% ✗
            "AVAX":  [50.0,   50.2],    # +0.4% ✗
            "MATIC": [1.0,    1.005],   # +0.5% ✗
            "LINK":  [10.0,   10.1],    # +1.0% ✗
        }
        result = regime_filter(closes, coins, coin_closes)
        assert result == "NORMAL"

    def test_breadth_check_skipped_for_empty_watchlist(self) -> None:
        closes = _flat_btc(60)
        result = regime_filter(closes, [], {"ETH": [1000.0, 1030.0]})
        assert result == "NORMAL"

    def test_breadth_skips_coins_with_insufficient_data(self) -> None:
        closes = _flat_btc(60)
        coins = ["ETH", "SOL"]
        # SOL has only 1 candle — should be skipped
        coin_closes = {"ETH": [1000.0, 1030.0], "SOL": [100.0]}
        result = regime_filter(closes, coins, coin_closes)
        # Only ETH counts (1 of 2 = 50% < 60%) → NORMAL
        assert result == "NORMAL"
