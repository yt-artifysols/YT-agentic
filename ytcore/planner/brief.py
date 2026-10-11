"""Brief generator: NEW owner inputs first, then the approved playbook as a fallback.

Claiming inputs links them to the job (provenance) and marks them used; goals are linked as
standing context and stay available to every later job of the channel.
"""

from __future__ import annotations

import re

from ytcore.agents.models import Brief, FootageRef
from ytcore.agents.untrusted import clean_text
from ytcore.inputs.models import InputKind, OwnerInput
from ytcore.inputs.store import InputStore
from ytcore.knowledge.models import KnowledgeKind, KnowledgeSnapshot

_URL = re.compile(r"https://[^\s<>\"')\]]{3,500}")


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


class BriefGenerator:
    def __init__(self, inputs: InputStore) -> None:
        self._inputs = inputs

    def channel_goals(self, channel_id: str) -> list[OwnerInput]:
        return self._inputs.goals(channel_id)

    def generate(
        self, job_id: str, channel_id: str, knowledge: KnowledgeSnapshot, *, origin: str
    ) -> Brief:
        picked = self._inputs.claim_for_job(job_id, channel_id, actor="planner")
        goals = self.channel_goals(channel_id)
        ideas = [i for i in picked if i.kind is InputKind.IDEA]
        notes = [i for i in picked if i.kind is InputKind.NOTE]
        footage = [i for i in picked if i.kind is InputKind.FOOTAGE]
        texts = [i.body or "" for i in ideas + notes]
        if ideas:
            topic = _first_line(ideas[0].body or "")
        elif notes:
            topic = _first_line(notes[0].body or "")
        else:
            playbook = knowledge.lines(KnowledgeKind.PLAYBOOK)
            topic = playbook[0] if playbook else f"Channel update for {channel_id}"
        urls: list[str] = []
        for text in texts:
            for url in _URL.findall(text):
                if url not in urls:
                    urls.append(url.rstrip(".,;"))
        return Brief(
            job_id=job_id,
            channel_id=channel_id,
            origin=origin,
            topic=clean_text(topic, limit=500) or "Untitled",
            notes=[clean_text(t, limit=2000) for t in texts[1:] if t],
            goals=[clean_text(g.body or g.uri or "", limit=500) for g in goals],
            footage=[
                FootageRef(
                    input_id=f.id,
                    uri=f.uri or "",
                    sha256=f.sha256,
                    note=clean_text(f.body or "", limit=500),
                )
                for f in footage
            ],
            source_urls=urls[:10],
            owner_input_ids=sorted(i.id for i in picked),
            goal_input_ids=sorted(g.id for g in goals),
        )
