import time

from django.core.management.base import BaseCommand

from mock_interview.services import interview_worker


class Command(BaseCommand):
    help = (
        "Process pending mock-interview background work (answer evaluations, "
        "pool audio pre-warming, session finalization, report generation). "
        "Runs forever unless --once is given."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single round of pending work and exit.",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=None,
            help="Seconds between polling rounds (default: MOCK_INTERVIEW['INTERVIEW_WORKER_INTERVAL']).",
        )
        parser.add_argument(
            "--evaluations",
            type=int,
            default=8,
            help="Max answers to claim per round.",
        )
        parser.add_argument(
            "--prewarm",
            type=int,
            default=6,
            help="Max pool questions to pre-synthesize audio for per round.",
        )
        parser.add_argument(
            "--no-reclaim-stale",
            action="store_true",
            help="Do not reclaim evaluating answers left stale by a crashed worker.",
        )

    def handle(self, *args, **options):
        reclaim_stale = not options["no_reclaim_stale"]
        if options["once"]:
            processed = interview_worker.process_pending_tasks_once(
                evaluations=options["evaluations"],
                prewarm=options["prewarm"],
                reclaim_stale=reclaim_stale,
            )
            self.stdout.write(self.style.SUCCESS(f"One worker round completed ({processed} tasks)."))
            return

        interval = options["interval"]
        self.stdout.write(
            self.style.SUCCESS(
                f"Interview worker started (interval={interval or 'default'}s, "
                f"evaluations={options['evaluations']}, prewarm={options['prewarm']}). "
                "Press Ctrl+C to stop."
            )
        )
        try:
            interview_worker.worker_loop(
                interval=interval,
                evaluations=options["evaluations"],
                prewarm=options["prewarm"],
            )
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nWorker stopped by user."))
            time.sleep(0)