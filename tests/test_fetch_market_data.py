import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))

import fetch_market_data as market_data


class FakeJQuantsClient:
    calls = []

    def __init__(self, *, api_key):
        self.api_key = api_key

    def get_drv_bars_daily_opt_225_range(self, *, start_dt, end_dt):
        self.calls.append((start_dt, end_dt))
        return pd.DataFrame(
            [
                {
                    "Date": "2026-08-12",
                    "SQD": "2026-09-11",
                    "PCDiv": "1",
                    "StrikePrice": 40000,
                    "OpenInterest": 1234,
                },
                {
                    "Date": "2026-08-13",
                    "SQD": "2026-09-11",
                    "PCDiv": "2",
                    "StrikePrice": 40500,
                    "OpenInterest": 2345,
                },
            ]
        )


class FetchMarketDataTests(unittest.TestCase):
    def setUp(self):
        FakeJQuantsClient.calls = []

    def test_environment_key_is_used_without_cli_argument(self):
        with patch.dict(os.environ, {"JQUANTS_API_KEY": "test-key"}, clear=False):
            self.assertEqual(market_data.resolve_jquants_api_key(None), "test-key")

    def test_cli_key_takes_precedence(self):
        with patch.dict(os.environ, {"JQUANTS_API_KEY": "environment-key"}, clear=False):
            self.assertEqual(market_data.resolve_jquants_api_key("cli-key"), "cli-key")

    def test_jquants_range_is_normalized_and_written(self):
        dates = pd.Series(pd.to_datetime(["2026-08-12", "2026-08-13"]))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            market_data, "JQuantsClient", FakeJQuantsClient
        ):
            out_dir = Path(tmp)
            market_data.fetch_jquants_options(out_dir, dates, api_key="test-key")
            result = pd.read_csv(out_dir / "options_oi.csv")

        self.assertEqual(FakeJQuantsClient.calls, [("20260812", "20260813")])
        self.assertEqual(list(result.columns), ["date", "expiry", "type", "strike", "open_interest"])
        self.assertEqual(result["type"].tolist(), ["P", "C"])
        self.assertEqual(result["open_interest"].tolist(), [1234, 2345])


if __name__ == "__main__":
    unittest.main()
