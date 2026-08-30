import os
from decimal import Decimal
from unittest import TestCase
from unittest.mock import patch

from trade_simulation.lab_config import LabSettings


class LabSettingsTests(TestCase):
    def test_default_carry_cost_includes_four_fills_and_both_fee_schedules(self) -> None:
        with patch.dict(os.environ, {"PAPER_ONLY": "true"}, clear=True):
            settings = LabSettings.from_env()

        self.assertEqual(settings.carry_roundtrip_cost, Decimal("0.0027"))

