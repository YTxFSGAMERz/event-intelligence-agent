import io
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from main import build_alert, build_photo_caption, generate_opportunity_card


MLH_ITEM = {
    "title": "HackNC",
    "link": "https://hacknc.com",
    "summary": "Official MLH upcoming hackathon/event calendar listing.",
    "adapter_type": "mlh_events_html",
    "event_schedule": "OCT 09 - 11, 2026",
    "event_location": "Chapel Hill, North Carolina, US In-Person",
}


class AlertContentTests(unittest.TestCase):
    def test_mlh_photo_caption_separates_event_dates_from_application_deadline(self):
        caption = build_photo_caption(
            MLH_ITEM,
            ["hackathons_buildathons"],
            "Not found — verify on official page",
            "UNKNOWN",
            primary="hackathons_buildathons",
        )

        self.assertIn("Application deadline: Not found", caption)
        self.assertIn("Event dates (calendar listing; unverified): OCT 09 - 11, 2026", caption)
        self.assertIn(
            "Listed location (unverified): Chapel Hill, North Carolina, US In-Person",
            caption,
        )
        self.assertNotIn("📅 Deadline: OCT 09 - 11", caption)
        self.assertIn("https://hacknc.com", caption)
        self.assertLessEqual(len(caption), 1024)

    def test_mlh_text_alert_labels_calendar_schedule_and_location_as_unverified(self):
        alert = build_alert(
            MLH_ITEM,
            ["hackathons_buildathons"],
            "Not found — verify on official page",
            "UNKNOWN",
            "No explicit status phrase in feed item",
            "REACHABLE",
            "HTTP 200",
        )

        self.assertIn("Application deadline: Not found", alert)
        self.assertIn("Event dates (MLH calendar listing; unverified): OCT 09 - 11, 2026", alert)
        self.assertIn(
            "Listed location (unverified): Chapel Hill, North Carolina, US In-Person",
            alert,
        )
        self.assertIn("Official/source link: https://hacknc.com", alert)

    def test_non_calendar_caption_keeps_existing_deadline_semantics(self):
        item = {
            "title": "Example Scholarship",
            "link": "https://scholarship.example/apply",
            "summary": "Applications are open.",
        }
        caption = build_photo_caption(
            item,
            ["scholarships_fellowships"],
            "October 31, 2026",
            "OPEN (phrase detected)",
            primary="scholarships_fellowships",
        )

        self.assertIn("Application deadline: October 31, 2026", caption)
        self.assertNotIn("Event dates (calendar listing", caption)
        self.assertNotIn("Listed location (unverified)", caption)

    def test_generated_card_uses_event_date_panel_for_calendar_listings(self):
        captured_text = []
        real_draw = ImageDraw.Draw

        class DrawProxy:
            def __init__(self, draw):
                self._draw = draw

            def text(self, xy, value, *args, **kwargs):
                captured_text.append(str(value))
                return self._draw.text(xy, value, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._draw, name)

        with patch("PIL.ImageDraw.Draw", side_effect=lambda image: DrawProxy(real_draw(image))):
            png = generate_opportunity_card(
                MLH_ITEM,
                ["hackathons_buildathons"],
                "Not found — verify on official page",
                "UNKNOWN",
                primary="hackathons_buildathons",
            )

        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (1200, 675))
        self.assertIn("EVENT DATES · UNVERIFIED", captured_text)
        self.assertIn("OCT 09 - 11, 2026", captured_text)
        self.assertTrue(any(text.startswith("Listed location (unverified): ") for text in captured_text))

    def test_generated_card_still_shows_application_deadline_for_other_sources(self):
        item = {
            "title": "Example Scholarship",
            "link": "https://scholarship.example/apply",
            "summary": "Applications are open.",
        }
        captured_text = []
        real_draw = ImageDraw.Draw

        class DrawProxy:
            def __init__(self, draw):
                self._draw = draw

            def text(self, xy, value, *args, **kwargs):
                captured_text.append(str(value))
                return self._draw.text(xy, value, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._draw, name)

        with patch("PIL.ImageDraw.Draw", side_effect=lambda image: DrawProxy(real_draw(image))):
            generate_opportunity_card(
                item,
                ["scholarships_fellowships"],
                "October 31, 2026",
                "OPEN (phrase detected)",
                primary="scholarships_fellowships",
            )
        self.assertIn("APPLICATION DEADLINE", captured_text)
        self.assertIn("October 31, 2026", captured_text)
        self.assertNotIn("EVENT DATES · UNVERIFIED", captured_text)


if __name__ == "__main__":
    unittest.main()
