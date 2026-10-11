"""Signals from the approval hub to VideoJobWorkflow (Temporal) or a fake for tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ytcore.workflow.models import HubDecision
from ytcore.workflow.video_job import VideoJobWorkflow, workflow_id_for


class WorkflowSignaler(Protocol):
    async def send(self, job_id: str, decision: HubDecision) -> None: ...


@dataclass
class FakeSignaler:
    sent: list[tuple[str, HubDecision]] = field(default_factory=list)

    async def send(self, job_id: str, decision: HubDecision) -> None:
        self.sent.append((job_id, decision))


class TemporalSignaler:
    def __init__(self, client: object) -> None:
        from temporalio.client import Client

        if not isinstance(client, Client):
            raise TypeError("TemporalSignaler needs a temporalio Client")
        self._client = client

    @classmethod
    async def connect(cls, address: str, namespace: str) -> TemporalSignaler:
        from temporalio.client import Client

        return cls(await Client.connect(address, namespace=namespace))

    async def send(self, job_id: str, decision: HubDecision) -> None:
        handle = self._client.get_workflow_handle(workflow_id_for(job_id))
        await handle.signal(VideoJobWorkflow.hub_decision, decision)
