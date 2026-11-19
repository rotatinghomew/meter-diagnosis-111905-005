import csv
import unittest
from pathlib import Path


class FixtureTests(unittest.TestCase):
    def test_support_records_include_goals_contacts_and_permission_changes(self):
        path = Path(__file__).parents[1] / "fixtures" / "support.csv"
        with path.open(encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        types = {row["record_type"] for row in rows}
        self.assertTrue({"household", "member", "goal", "contact", "permission"} <= types)
        self.assertEqual(len(rows), len({row["record_id"] for row in rows}))


if __name__ == "__main__":
    unittest.main()
