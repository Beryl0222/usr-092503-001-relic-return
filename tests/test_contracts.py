import unittest

from src.relic_case import CaseStage, validate_external_id


class ContractTests(unittest.TestCase):
    def test_stage_values_are_stable(self):
        self.assertEqual(CaseStage.HANDOVER.value, "handover")

    def test_external_id_rejects_short_value(self):
        with self.assertRaises(ValueError):
            validate_external_id("short")


if __name__ == "__main__":
    unittest.main()
