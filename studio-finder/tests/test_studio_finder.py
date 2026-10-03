import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import studio_finder as sf  # noqa: E402

JST = sf.JST


def jst(h, m=0):
    return datetime(2026, 10, 20, h, m, tzinfo=JST)


class ParseIcalTest(unittest.TestCase):
    def test_utc_is_converted_to_jst(self):
        text = "BEGIN:VEVENT\nDTSTART:20261020T060000Z\nDTEND:20261020T080000Z\nEND:VEVENT\n"
        self.assertEqual(sf.parse_ical_events(text), [(jst(15), jst(17))])

    def test_tzid_and_folded_lines(self):
        text = ("BEGIN:VEVENT\r\nDTSTART;TZID=Asia/Tokyo:20261020T\r\n 100000\r\n"
                "DTEND;TZID=Asia/Tokyo:20261020T120000\r\nEND:VEVENT\r\n")
        self.assertEqual(sf.parse_ical_events(text), [(jst(10), jst(12))])

    def test_all_day_event_blocks_whole_day(self):
        text = "BEGIN:VEVENT\nDTSTART;VALUE=DATE:20261020\nEND:VEVENT\n"
        events = sf.parse_ical_events(text)
        self.assertTrue(sf.find_conflicts(events, jst(10), jst(12)))

    def test_transparent_event_is_ignored(self):
        text = ("BEGIN:VEVENT\nDTSTART:20261020T010000Z\nDTEND:20261020T030000Z\n"
                "TRANSP:TRANSPARENT\nEND:VEVENT\n")
        self.assertEqual(sf.parse_ical_events(text), [])


class CheckStudioTest(unittest.TestCase):
    def setUp(self):
        self.studios = sf.load_studios(ROOT / "studios.csv")

    def test_sample_results(self):
        results = {r.studio.name[:9]: r for r in
                   (sf.check_studio(s, jst(10), jst(14)) for s in self.studios)}
        self.assertEqual(results["サンプルスタジオA"].status, sf.STATUS_FREE)
        self.assertEqual(results["サンプルスタジオB"].status, sf.STATUS_UNKNOWN)
        self.assertEqual(results["サンプルスタジオC"].status, sf.STATUS_FREE)

        busy = sf.check_studio(self.studios[0], jst(14), jst(16))
        self.assertEqual(busy.status, sf.STATUS_BUSY)

    def test_back_to_back_booking_is_free(self):
        # 15:00〜17:00 の予約の直後 17:00〜 は空き扱い
        r = sf.check_studio(self.studios[0], jst(17), jst(18))
        self.assertEqual(r.status, sf.STATUS_FREE)

    def test_min_hours_applies_to_cost(self):
        # Cは最低3時間・¥7,000 → 1時間利用でも ¥21,000
        r = sf.check_studio(self.studios[2], jst(10), jst(11))
        self.assertEqual(r.cost, 21000)

    def test_fetch_error_is_reported(self):
        def broken(url):
            raise OSError("timeout")
        r = sf.check_studio(self.studios[0], jst(10), jst(12), fetch=broken)
        self.assertEqual(r.status, sf.STATUS_ERROR)

    def test_filters(self):
        self.assertEqual(len(sf.filter_studios(self.studios, area="渋谷区")), 2)
        self.assertEqual(len(sf.filter_studios(self.studios, max_price=5500)), 2)
        self.assertEqual(len(sf.filter_studios(self.studios, people=10)), 1)


if __name__ == "__main__":
    unittest.main()
