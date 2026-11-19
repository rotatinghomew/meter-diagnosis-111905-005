import csv
from pathlib import Path
import unittest


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
HEADERS = {'access_windows': ['meter', 'from_hour', 'until_hour'], 'calibration_findings': ['id', 'kit', 'affected_from', 'affected_until', 'recorded', 'result'], 'campaigns': ['id', 'kit', 'target', 'from_hour', 'until_hour', 'status', 'aim'], 'comparison_windows': ['id', 'target', 'base_from', 'base_until', 'test_from', 'test_until', 'expected_base', 'expected_test'], 'decisions': ['id', 'as_at', 'target', 'kind', 'status'], 'interventions': ['id', 'room', 'in_service', 'component', 'applied'], 'kits': ['id', 'available_from', 'available_to', 'kind'], 'observations': ['id', 'campaign', 'point', 'from_hour', 'until_hour', 'wh', 'connection_verified', 'recorded'], 'operations': ['hour', 'active_a', 'active_b', 'temperature', 'service'], 'policy': ['wh_delta', 'duration_h', 'min_comparables', 'temp_band', 'uncertain_margin'], 'readings': ['id', 'meter', 'hour', 'wh', 'recorded', 'kind'], 'savings_claims': ['id', 'interventions', 'boundary', 'reference_from', 'reference_until', 'after_from', 'after_until', 'claimed_wh', 'evidence', 'signed'], 'synchronized_readings': ['id', 'session', 'part', 'kit', 'meter', 'from_hour', 'until_hour', 'wh', 'verified', 'received'], 'synchronized_sessions': ['session', 'comparison', 'part', 'kit', 'meter', 'from_hour', 'until_hour', 'status'], 'topology': ['id', 'meter', 'parent', 'served', 'from_hour', 'until_hour', 'recorded', 'replaces']}
LINKS = [('campaigns', 'kit', 'kits', 'id'), ('observations', 'campaign', 'campaigns', 'id'), ('synchronized_sessions', 'comparison', 'comparison_windows', 'id'), ('synchronized_sessions', 'kit', 'kits', 'id'), ('synchronized_readings', 'session', 'synchronized_sessions', 'session'), ('synchronized_readings', 'kit', 'kits', 'id'), ('calibration_findings', 'kit', 'kits', 'id')]


def read(name):
    with (FIXTURES / (name + ".csv")).open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        return reader.fieldnames, list(reader)


class SourceRecords(unittest.TestCase):
    def test_records_match_source_columns(self):
        for name, expected in HEADERS.items():
            with self.subTest(table=name):
                header, rows = read(name)
                self.assertEqual(header, expected)
                self.assertTrue(rows)
                self.assertTrue(all(None not in row and None not in row.values() for row in rows))
                if "id" in header:
                    ids = [row["id"] for row in rows]
                    self.assertTrue(all(ids))
                    self.assertEqual(len(ids), len(set(ids)))

    def test_record_references_exist(self):
        for source, field, target, key in LINKS:
            with self.subTest(table=source, column=field):
                _, source_rows = read(source)
                _, target_rows = read(target)
                keys = {row[key] for row in target_rows}
                self.assertTrue(all(row[field] in keys for row in source_rows))
