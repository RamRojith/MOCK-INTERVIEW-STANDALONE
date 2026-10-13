import json

from django.core.management.base import BaseCommand

from mock_interview.evaluation_benchmark.runner import load_cases, run_benchmark


class Command(BaseCommand):
    help = "Compare AI mock-interview evaluation against expert-scored cases."

    def add_arguments(self, parser):
        parser.add_argument(
            "dataset",
            help="Path to a JSONL benchmark dataset.",
        )
        parser.add_argument(
            "--total-tolerance",
            type=float,
            default=10.0,
            help="Allowed total-score error on the 0-100 scale.",
        )
        parser.add_argument(
            "--dimension-tolerance",
            type=float,
            default=1.0,
            help="Allowed dimension-score error on the 0-10 scale.",
        )
        parser.add_argument(
            "--output",
            help="Optional path to write full benchmark JSON results.",
        )

    def handle(self, *args, **options):
        cases = load_cases(options["dataset"])
        result = run_benchmark(
            cases,
            total_tolerance=options["total_tolerance"],
            dimension_tolerance=options["dimension_tolerance"],
        )
        summary_json = json.dumps(result["summary"], indent=2)
        self.stdout.write(summary_json)

        if options.get("output"):
            with open(options["output"], "w", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2)
            self.stdout.write(
                self.style.SUCCESS(f"Full benchmark written to {options['output']}")
            )
