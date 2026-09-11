from __future__ import annotations

import pytest
from pydantic import ValidationError

from myloware.api.routes.studio import DecisionBody
from myloware.studio.planning_store import PlanRevisionRequest


@pytest.mark.parametrize("ordinals", [[True], [0], [13], [1, 1], [], ["1"]])
def test_revision_ordinals_are_strict_bounded_unique_positions(ordinals) -> None:
    with pytest.raises(ValidationError):
        PlanRevisionRequest(replace_ordinals=ordinals)


def test_feedback_only_belongs_to_an_idea_revision() -> None:
    body = {
        "gate": "ideas",
        "decision": "revise",
        "revision": 1,
        "subject_hash": "a" * 64,
        "request_key": "valid-request-key",
        "revision_feedback": {"note": "  Keep the shape.  "},
    }
    parsed = DecisionBody(**body)
    assert parsed.revision_feedback.replace_ordinals == list(range(1, 13))
    assert parsed.revision_feedback.note == "Keep the shape."
    for gate, decision in [("ideas", "approve"), ("final", "keep_reviewing"), ("final", "approve")]:
        with pytest.raises(ValidationError):
            DecisionBody(**(body | {"gate": gate, "decision": decision}))
    with pytest.raises(ValidationError):
        DecisionBody(**(body | {"revision_feedback": {"note": "x" * 501}}))
