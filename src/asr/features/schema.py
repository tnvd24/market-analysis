"""The feature schema — the one place that names the indicator columns.

Kept free of pandas-ta (and of any heavy import) so that `storage` can build its DDL
from the same list the indicator layer computes, without dragging the whole technical
analysis stack into every process that only wants to open the database.
"""

from __future__ import annotations

#: Every column the feature layer produces, in table order. The `features` DDL follows this.
FEATURE_COLUMNS = [
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "sma_20",
    "sma_50",
    "sma_200",
    "ema_20",
    "atr_14",
    "bb_lower",
    "bb_mid",
    "bb_upper",
    "bb_pct",
    "obv",
    "ret_1d",
    "volume_sma_20",
    "rel_volume",
]

#: Bars needed before the slowest indicator (SMA-200) is defined.
MIN_HISTORY = 200
