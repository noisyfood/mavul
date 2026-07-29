import unittest

from system.envelope import (
    ResultEnvelope,
    SystemAction,
    SystemEnvelope,
    TaskEnvelope,
)
from system.mailbox import PriorityMailbox


class EnvelopeMailboxTest(unittest.TestCase):
    def test_system_priority_and_fifo_within_priority(self):
        mailbox = PriorityMailbox()
        first = TaskEnvelope(sender="o", recipient="r", content="first")
        second = TaskEnvelope(sender="o", recipient="r", content="second")
        system = SystemEnvelope(
            sender="o",
            recipient="r",
            event="interrupt",
            action=SystemAction.INTERRUPT_AGENT,
        )

        mailbox.put(first)
        mailbox.put(second)
        mailbox.put(system)

        self.assertIs(mailbox.get(), system)
        self.assertIs(mailbox.get(), first)
        self.assertIs(mailbox.get(), second)

    def test_envelope_has_routing_header_and_concrete_type(self):
        result = ResultEnvelope(
            sender="reverser",
            recipient="orchestrator",
            task_id="task-1",
            agent_signature="reverser",
            status="complete",
            summary="Mapped parser",
        )

        self.assertEqual(result.envelope_type, "result")
        self.assertEqual(result.task_id, "task-1")
        self.assertIsNotNone(result.envelope_id)
        self.assertIsNotNone(result.created_at.tzinfo)

    def test_closed_mailbox_rejects_delivery(self):
        mailbox = PriorityMailbox()
        mailbox.close()

        with self.assertRaises(RuntimeError):
            mailbox.put(TaskEnvelope(sender="o", recipient="r", content="task"))

    def test_remove_agent_mail_preserves_remaining_fifo(self):
        mailbox = PriorityMailbox()
        first = TaskEnvelope(
            sender="o",
            recipient="r",
            task_id="remove",
            content="first",
        )
        second = TaskEnvelope(
            sender="o",
            recipient="r",
            task_id="keep",
            content="second",
        )
        mailbox.put(first)
        mailbox.put(second)

        removed = mailbox.remove_agent(
            lambda envelope: envelope.task_id == "remove"
        )

        self.assertEqual(removed, [first])
        self.assertIs(mailbox.get(), second)


if __name__ == "__main__":
    unittest.main()
