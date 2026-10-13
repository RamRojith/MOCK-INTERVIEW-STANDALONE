import logging
from datetime import timedelta

from django.utils import timezone

from mock_interview.models import (
    InterviewAssignment,
    InterviewSession,
    MockInterview,
)

logger = logging.getLogger(__name__)


class SchedulerError(Exception):
    pass


class InterviewScheduler:
    """Enforces interview time windows and manages scheduling state."""

    def get_interview_status(self, interview: MockInterview) -> str:
        """Determine the current status of an interview based on time."""
        now = timezone.now()

        if interview.status == "cancelled":
            return "cancelled"
        if interview.status == "completed":
            return "completed"
        if interview.status == "draft":
            return "draft"

        if interview.start_time and now < interview.start_time:
            return "scheduled"
        if interview.end_time and now > interview.end_time:
            return "expired"
        if interview.start_time and interview.end_time:
            if interview.start_time <= now <= interview.end_time:
                return "open"

        return interview.status

    def can_student_start(self, interview: MockInterview) -> tuple[bool, str]:
        """Check if a student is allowed to start the interview now."""
        status = self.get_interview_status(interview)

        if status == "draft":
            return False, "This interview has not been published yet."
        if status == "cancelled":
            return False, "This interview has been cancelled."
        if status == "completed":
            return False, "This interview has already been completed."
        if status == "expired":
            return False, "The time window for this interview has passed."
        if status == "scheduled":
            start_str = interview.start_time.strftime("%I:%M %p %Z")
            return False, f"This interview opens at {start_str}."
        if status == "open":
            return True, "Interview is available now."
        return False, "Interview status is unknown."

    def get_time_info(self, interview: MockInterview) -> dict:
        """Return time information for display."""
        now = timezone.now()
        status = self.get_interview_status(interview)

        info = {
            "status": status,
            "now": now.isoformat(),
            "start_time": (
                interview.start_time.isoformat() if interview.start_time else None
            ),
            "end_time": (
                interview.end_time.isoformat() if interview.end_time else None
            ),
        }

        if interview.start_time and now < interview.start_time:
            delta = interview.start_time - now
            info["opens_in_minutes"] = int(delta.total_seconds() / 60)
        if interview.end_time and now < interview.end_time:
            delta = interview.end_time - now
            info["closes_in_minutes"] = int(delta.total_seconds() / 60)

        return info

    def expire_overdue_interviews(self):
        """Mark expired interviews and assignments."""
        now = timezone.now()
        expired_interviews = MockInterview.objects.filter(
            status="open",
            end_time__lt=now,
        )
        count = expired_interviews.update(status="completed")

        expired_assignments = InterviewAssignment.objects.filter(
            status__in=("assigned", "in_progress"),
            interview__end_time__lt=now,
        )
        expired_count = expired_assignments.update(status="expired")

        if count or expired_count:
            logger.info(
                "Expired %d interviews and %d assignments", count, expired_count
            )
        return count, expired_count

    def validate_interview_times(self, start_time, end_time) -> None:
        """Validate that interview times are sensible."""
        if not start_time or not end_time:
            return
        if start_time >= end_time:
            raise SchedulerError("End time must be after start time.")
        if end_time - start_time < timedelta(minutes=5):
            raise SchedulerError("Interview must be at least 5 minutes long.")
        if start_time < timezone.now():
            raise SchedulerError("Start time cannot be in the past.")
