"""Public install readiness is separate from the provisioning operation.

API scenarios exercise placeholder reuse and filling end to end. These isolated
projection checks cover stale shares, publisher previews and bounded reads.
"""

import uuid
from unittest.mock import MagicMock

import pytest

from app.models.agents.agent import Agent
from app.models.credentials.credential import Credential, CredentialType
from app.services.credentials.credential_provisioner import SlotProvision
from app.services.skills.skill_credential_requirements import (
    SkillCredentialRequirements,
)


@pytest.mark.parametrize(
    "outcome,owned,placeholder,sharing,shared,expected",
    [
        ("already_linked", True, True, False, False, True),
        ("linked_existing", True, True, False, False, True),
        ("already_linked", True, False, False, False, False),
        ("linked_existing", True, False, False, False, False),
        ("linked_publisher", True, False, True, False, False),
        ("linked_publisher", False, False, True, False, False),
        ("linked_publisher", False, False, True, True, False),
        ("already_linked", False, False, True, True, False),
        ("linked_existing", False, False, True, True, False),
        ("already_linked", False, False, False, True, True),
        ("already_linked", False, False, True, False, True),
        ("linked_existing", False, True, True, True, True),
    ],
)
def test_readiness_and_name_visibility(
    outcome,
    owned,
    placeholder,
    sharing,
    shared,
    expected,
):
    agent = Agent(name="Consumer", owner_id=uuid.uuid4())
    credential = Credential(
        name="Connection",
        type=CredentialType.API_TOKEN,
        owner_id=agent.owner_id if owned else uuid.uuid4(),
        is_placeholder=placeholder,
        allow_sharing=sharing,
    )
    session = MagicMock()
    session.exec.return_value.all.side_effect = [
        [credential],
        [credential.id] if shared else [],
        [],  # group shares
    ]
    items = [
        SlotProvision(
            spec_name=f"slot-{index}",
            spec_type="api_token",
            slot=f"slot-{index}",
            provided_by="publisher" if outcome == "linked_publisher" else "user",
            description=None,
            outcome=outcome,
            credential_id=credential.id,
        )
        for index in range(20)
    ]

    result = SkillCredentialRequirements.provisions_to_public(
        session,
        agent=agent,
        items=items,
    )

    assert len(result) == 20
    assert all(item.needs_setup is expected for item in result)
    expected_name = credential.name if owned or shared else None
    assert all(item.credential_name == expected_name for item in result)
    assert session.exec.call_count == (1 if owned else 3)


@pytest.mark.parametrize(
    "outcome",
    ["placeholder_created", "template_materialised", "publisher_unavailable"],
)
def test_preview_of_a_new_placeholder_needs_setup_without_a_credential(outcome):
    session = MagicMock()
    item = SlotProvision(
        spec_name="erp",
        spec_type="api_token",
        slot="erp",
        provided_by="user",
        description=None,
        outcome=outcome,
        credential_id=None,
    )
    result = SkillCredentialRequirements.provisions_to_public(
        session,
        agent=Agent(name="Consumer", owner_id=uuid.uuid4()),
        items=[item],
    )
    assert result[0].needs_setup is True
    assert result[0].credential_name is None
    session.exec.assert_not_called()


@pytest.mark.parametrize("is_placeholder", [False, True, None])
def test_publisher_preview_keeps_readiness_when_credential_id_is_redacted(
    is_placeholder,
):
    session = MagicMock()
    item = SlotProvision(
        spec_name="erp",
        spec_type="api_token",
        slot="erp",
        provided_by="publisher",
        description=None,
        outcome="linked_publisher",
        credential_id=None,
        is_placeholder=is_placeholder,
    )
    result = SkillCredentialRequirements.provisions_to_public(
        session,
        agent=Agent(name="Consumer", owner_id=uuid.uuid4()),
        items=[item],
    )
    assert result[0].needs_setup is (is_placeholder is not False)
    assert result[0].credential_id is None
    assert result[0].credential_name is None
    session.exec.assert_not_called()
