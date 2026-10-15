import unittest

from app.routes.career_workspace import (
    AiJobSearchRecord,
    CareerOpsRecord,
    WorkspaceImportRequest,
    build_workspace_preview,
)


class CareerWorkspacePreviewTests(unittest.TestCase):
    def test_merges_same_role_and_uses_the_furthest_stage(self):
        preview = build_workspace_preview(WorkspaceImportRequest(
            ai_job_search=[AiJobSearchRecord(
                company="Acme", role="Data Engineer", status="applied", fit_rating=72,
            )],
            career_ops=[CareerOpsRecord(
                company=" acme ", role="data engineer", status="interview", score=4.5,
            )],
        ))

        self.assertEqual(preview.duplicate_count, 1)
        self.assertEqual(len(preview.applications), 1)
        application = preview.applications[0]
        self.assertEqual(application.stage, "interview")
        self.assertEqual(application.fit_score, 90)
        self.assertEqual(application.sources, ["ai-job-search", "career-ops"])

    def test_clamps_percentage_scores_and_closes_expired_records(self):
        preview = build_workspace_preview(WorkspaceImportRequest(
            ai_job_search=[AiJobSearchRecord(
                company="Example", role="Developer", status="expired", fit_rating=140,
            )],
        ))

        self.assertEqual(preview.applications[0].stage, "closed")
        self.assertEqual(preview.applications[0].fit_score, 100)


if __name__ == "__main__":
    unittest.main()
