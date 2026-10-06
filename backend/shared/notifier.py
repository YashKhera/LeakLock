"""Notifier implementations for LeakLock.

:class:`SnsNotifier` is the production :class:`Notifier` (see
``backend.shared.alerts``): it publishes to an SNS topic. Tests use
``FakeNotifier`` from ``alerts.py`` or moto-mocked SNS.
"""

from typing import Any

from backend.shared.alerts import Notifier

MAX_SUBJECT_CHARS = 100


def sanitise_subject(subject: str) -> str:
    """Collapse a subject to one SNS-safe line of at most 100 characters."""
    one_line = " ".join(str(subject).split())
    if len(one_line) > MAX_SUBJECT_CHARS:
        return one_line[: MAX_SUBJECT_CHARS - 3] + "..."
    return one_line


class SnsNotifier(Notifier):
    """Deliver alert emails via SNS. Publish failures propagate: callers
    such as :func:`process_alert` already catch and record them."""

    def __init__(self, topic_arn: str, sns_client: Any) -> None:
        if not topic_arn:
            raise ValueError("topic_arn is required")
        self.topic_arn = topic_arn
        self.sns_client = sns_client

    def send(self, subject: str, body: str) -> None:
        self.sns_client.publish(
            TopicArn=self.topic_arn,
            Subject=sanitise_subject(subject),
            Message=body,
        )
