import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import sync_playwright

from interway_sync import (
    extract,
    infer_dates,
    parse_epacks,
    read_credentials,
    sync_google_calendar,
)


class Response:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self.data = data or {}
        self.ok = 200 <= status_code < 300

    def json(self):
        return self.data


class CalendarSession:
    def __init__(self):
        self.created = None
        self.events = {
            "manual": {"id": "manual", "summary": "Personnel"},
        }

    def get(self, url, params=None):
        if params is not None:
            return Response(200, {"items": list(self.events.values())})
        event = self.events.get(url.rsplit("/", 1)[-1])
        return Response(200, event) if event else Response(404)

    def post(self, url, params, json):
        self.created = json
        self.events[json["id"]] = json
        return Response(200, json)

    def put(self, url, params, json):
        self.events[json["id"]] = json
        return Response(200, json)

    def patch(self, url, params, json):
        event = self.events[url.rsplit("/", 1)[-1]]
        event.update(json)
        return Response(200, event)

    def delete(self, url, params):
        self.events.pop(url.rsplit("/", 1)[-1])
        return Response(204)


class InterwaySyncTest(unittest.TestCase):
    def test_credentials_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory, "credentials")
            path.write_text("123456\nsecret\n", encoding="utf-8")
            self.assertEqual(read_credentials(path), ("123456", "secret"))

    def test_epack_only_and_deduplicated(self):
        tooltip = """11:33 : EPACK - Vern-sur-Seiche / EP_Config99 - / : 0,25h / SV2603190555
20:00 : LOTO - BEAUSSAIS SUR MER / MCO - / écran figé : 0,15h / SV2606300009
11:33 : EPACK - Vern-sur-Seiche / EP_Config99 - / : 0,25h / SV2603190555"""

        self.assertEqual(
            parse_epacks(tooltip),
            [
                {
                    "type": "EPACK",
                    "heure": "11:33",
                    "ville": "Vern-sur-Seiche",
                    "activite": "EP_Config99",
                    "duree_heures": 0.25,
                    "duree_minutes": 15,
                    "reference": "SV2603190555",
                }
            ],
        )

    def test_dates_cross_months(self):
        self.assertEqual(
            [value.isoformat() for value in infer_dates([29, 30, 1, 2, 31, 1], 6, 2026)],
            [
                "2026-06-29",
                "2026-06-30",
                "2026-07-01",
                "2026-07-02",
                "2026-07-31",
                "2026-08-01",
            ],
        )

    def test_visible_calendar_extraction(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(
                """
                <h2>Technicien BUSINESS PARTNER (juin 2026 - juillet 2026)</h2>
                <table>
                  <tr><th>Lundi</th><th>Mardi</th><th>Dimanche</th></tr>
                  <tr>
                    <td id="epack">PM<br>29</td>
                    <td>NT<br>30</td>
                    <td>-<br>01</td>
                  </tr>
                </table>
                <table>
                  <tr><th>Technicien</th><th>Nom</th><th>29/06</th></tr>
                  <tr><td>123456</td><td>Technicien BUSINESS PARTNER</td><td>PM</td></tr>
                </table>
                <script>
                  document.querySelector('#epack').addEventListener('mouseenter', () => {
                    const tooltip = document.createElement('div');
                    tooltip.className = 'mud-tooltip';
                    tooltip.textContent =
                      '11:33 : EPACK - Vern-sur-Seiche / EP_Config99 - / : 0,25h / SV2603190555';
                    document.body.appendChild(tooltip);
                  }, {once: true});
                </script>
                """
            )

            result = extract(page, "123456", hover_ms=200, year=None)
            browser.close()

        self.assertEqual(result["jours"][0]["date"], "2026-06-29")
        self.assertEqual(result["jours"][0]["interventions"][0]["reference"], "SV2603190555")

    def test_google_calendar_event_creation(self):
        session = CalendarSession()
        planning = {
            "jours": [
                {
                    "date": "2026-07-06",
                    "interventions": [
                        {
                            "heure": "11:33",
                            "ville": "Vern-sur-Seiche",
                            "activite": "EP_Config99",
                            "duree_minutes": 15,
                            "reference": "SV2603190555",
                        }
                    ],
                }
            ]
        }

        result = sync_google_calendar(planning, Path("unused.json"), "calendar@example.com", session)

        self.assertEqual(
            result,
            {"created": 1, "updated": 0, "unchanged": 0, "pending_deletion": 0, "deleted": 0},
        )
        self.assertEqual(session.created["summary"], "EPACK - Vern-sur-Seiche")
        self.assertEqual(session.created["start"]["dateTime"], "2026-07-06T11:33:00+02:00")

        empty_planning = {"jours": [{"date": "2026-07-06", "interventions": []}]}
        first_miss = sync_google_calendar(empty_planning, Path("unused.json"), "calendar@example.com", session)
        second_miss = sync_google_calendar(empty_planning, Path("unused.json"), "calendar@example.com", session)

        self.assertEqual(first_miss["pending_deletion"], 1)
        self.assertEqual(second_miss["deleted"], 1)
        self.assertIn("manual", session.events)


if __name__ == "__main__":
    unittest.main()
