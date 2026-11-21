import unittest
from dataclasses import asdict, dataclass

from fure.core.function import Function
from fure.core.implementations import PythonImplementation
from fure.core.revision import Revision
from fure.core.run import (
    BaseRunMeta,
    CancelRunMeta,
    FailRunMeta,
    Run,
    RunStatus,
    SucceedRunMeta,
)
from fure.core.runners import (
    BasePythonRunnerOptions,
    InlinePythonRunMeta,
    InlinePythonRunner,
    OpenAIJinjaRunMeta,
)


class RunMetaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.revision = Revision(
            PythonImplementation(
                "async def function(input: None) -> str: return 'done'"
            ),
            BasePythonRunnerOptions(timeout=None),
            Function("example"),
        )

    async def test_runner_result_can_complete_run_with_typed_meta(self):
        run = Run(self.revision, None)
        self.assertIsNone(run.meta)
        run.start()

        result = await InlinePythonRunner(self.revision.runner_options).run(
            self.revision.implementation, run.input
        )
        run.succeed(result.output, result.meta, result.duration)

        self.assertEqual(run.status, RunStatus.SUCCEEDED)
        self.assertEqual(run.output, "done")
        self.assertIsInstance(run.meta, InlinePythonRunMeta)
        self.assertIsInstance(run.meta, SucceedRunMeta)
        self.assertIsInstance(run.meta, BaseRunMeta)
        self.assertEqual(run.meta, result.meta)
        self.assertEqual(asdict(run.meta), {"tool_calls": (), "assistant_comments": ()})

    def test_fail_and_cancel_accept_their_metadata(self):
        for started in (False, True):
            for method, meta, status in (
                ("fail", FailRunMeta(), RunStatus.FAILED),
                ("cancel", CancelRunMeta(), RunStatus.CANCELLED),
            ):
                with self.subTest(started=started, method=method):
                    run = Run(self.revision, None)
                    if started:
                        run.start()

                    getattr(run, method)(meta, duration=None)

                    self.assertEqual(run.status, status)
                    self.assertEqual(run.meta, meta)
                    self.assertIsNotNone(run.finished_at)

    def test_restored_run_preserves_concrete_metadata_and_token_counts(self):
        meta = OpenAIJinjaRunMeta(input_tokens=5, output_tokens=3, total_tokens=8)

        run = Run(self.revision, None, status=RunStatus.SUCCEEDED, meta=meta)

        self.assertIsInstance(run.meta, OpenAIJinjaRunMeta)
        self.assertEqual(run.meta.input_tokens, 5)
        self.assertEqual(run.meta.output_tokens, 3)
        self.assertEqual(run.meta.total_tokens, 8)
        self.assertEqual(
            asdict(run.meta),
            {
                "input_tokens": 5,
                "output_tokens": 3,
                "total_tokens": 8,
                "tool_calls": (),
                "assistant_comments": (),
            },
        )

    def test_metadata_subclass_keeps_defensive_copy_behavior(self):
        @dataclass(frozen=True, slots=True)
        class DetailedFailRunMeta(FailRunMeta):
            errors: list[str]

        meta = DetailedFailRunMeta(errors=["function failed"])
        run = Run(self.revision, None)

        run.fail(meta, duration=None)
        meta.errors.append("changed after fail")
        exposed_meta = run.meta
        exposed_meta.errors.clear()

        self.assertEqual(run.meta, DetailedFailRunMeta(errors=["function failed"]))


if __name__ == "__main__":
    unittest.main()
