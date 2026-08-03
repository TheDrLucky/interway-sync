import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import sync_playwright

from interway_sync import extract, infer_dates, parse_epacks, read_credentials


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


if __name__ == "__main__":
    unittest.main()
