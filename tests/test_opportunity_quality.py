import unittest

from opportunity_quality import assess_discovery_quality


class OpportunityQualityTests(unittest.TestCase):
    def test_rejects_generic_scholarship_roundups(self):
        samples = (
            "Top Fully Funded Scholarships in Ireland 2027 - Shiksha.com",
            "Top Scholarships in Germany For Indian Students 2027",
            "Scholarship for 12th Class Students 2026-27: Government and Private Schemes, Eligibility, Amount and How to Apply",
            "Scholarships for Indian students to study in England, Wales, Scotland and Ireland",
            "10 best tech internships students should apply for",
        )
        for title in samples:
            with self.subTest(title=title):
                result = assess_discovery_quality(
                    title, "Fully funded scholarships and student funding information.",
                    ["scholarships_fellowships"],
                )
                self.assertFalse(result["accepted"])
                self.assertEqual(result["status"], "roundup_or_listicle")

    def test_keeps_a_named_hackathon_from_curated_catalogue(self):
        result = assess_discovery_quality(
            "Nebius x NVIDIA Global AI Hackathon — Online, 26 Aug – 30 Oct 2026",
            "Prize pool: $50,000. Submit a working AI system.",
            ["hackathons_buildathons", "prizes_cash_rewards"],
            adapter_type="hackalendar_rss",
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "specific_opportunity")

    def test_keeps_actionable_funded_student_program_announcement(self):
        result = assess_discovery_quality(
            "US Embassy opens applications for fully funded YES Programme 2027",
            "Applications are open to eligible students.",
            ["funded_travel_abroad", "scholarships_fellowships"],
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "actionable_update")

    def test_keeps_a_deadline_extension_as_actionable(self):
        result = assess_discovery_quality(
            "NMMSS 2026-27: Education Ministry extends scholarship application deadline to October 31",
            "Fresh and renewal applications are accepted.",
            ["scholarships_fellowships"],
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "actionable_update")

    def test_keeps_school_grade_scholarship_exam_with_application_deadline(self):
        result = assess_discovery_quality(
            "NMMSS 2026: Delhi Class 8 scholarship exam on December 5, applications till October 5",
            "Students in Class 8 can apply for the examination.",
            ["scholarships_fellowships"],
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "actionable_update")

    def test_rejects_ranked_roundup_with_subject_words_between_number_and_type(self):
        result = assess_discovery_quality(
            "10 best tech internships students should apply for",
            "A list of internships from several organizations.",
            ["internships_training"],
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "roundup_or_listicle")

    def test_rejects_results_only_news(self):
        result = assess_discovery_quality(
            "Scholarship results announced for 2026",
            "Applicants can check result status on the portal.",
            ["scholarships_fellowships"],
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "non_actionable_news")

    def test_rejects_keywords_only_in_generic_article_body(self):
        result = assess_discovery_quality(
            "How universities are responding to economic pressures",
            "A scholarship, tech competition, prizes and internships are discussed in the report.",
            ["scholarships_fellowships", "competitions_challenges", "prizes_cash_rewards"],
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "low_specificity")

    def test_keeps_name_only_event_from_curated_mlh_calendar(self):
        result = assess_discovery_quality(
            "HackNC",
            "Official MLH upcoming hackathon/event calendar listing. Calendar section year: 2026.",
            ["hackathons_buildathons"],
            adapter_type="mlh_events_html",
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "specific_opportunity")
        self.assertEqual(result["rule_version"], 4)

    def test_keeps_named_nsp_scheme_with_portal_deadline(self):
        result = assess_discovery_quality(
            "AICTE - Swanath Scholarship Scheme (Technical Degree)",
            "National Scholarship Portal (NSP), academic year 2026-27; official listing, scheme details not independently verified. Student application deadline: October 31, 2026.",
            ["scholarships_fellowships"],
            adapter_type="nsp_scholarships_html",
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "specific_opportunity")
        self.assertEqual(result["rule_version"], 4)

    def test_keeps_named_devfolio_open_hackathon(self):
        result = assess_discovery_quality(
            "Wild Bugs",
            "Devfolio platform listing; not independently verified as an organizer page. Listing section: Open. Schedule/status as displayed: Starts 14/10/26. Format as displayed: Online.",
            ["hackathons_buildathons"],
            adapter_type="devfolio_html",
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "specific_opportunity")
        self.assertEqual(result["rule_version"], 4)

    def test_quality_result_has_a_rule_version_and_a_reason(self):
        result = assess_discovery_quality(
            "A real example student hackathon",
            "Submit a project for prizes.",
            ["hackathons_buildathons"],
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["rule_version"], 3)
        self.assertTrue(result["reason"])
        self.assertIn("matched_categories", result)


if __name__ == "__main__":
    unittest.main()
