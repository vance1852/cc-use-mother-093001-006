import unittest

from creative_program_foundation.acceptance_allocation import run


class AllocationAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"], msg=result)
        self.assertTrue(result["audit_valid"])
        for name, passed in result["checks"].items():
            self.assertTrue(passed, msg=f"{name} 未通过: {result}")


if __name__ == "__main__":
    unittest.main()
