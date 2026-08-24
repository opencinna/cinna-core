"""
App Agent Router — the dict-shaped adapter over the one classifier.

The rendering, parsing and trace emission this module used to own now live in
``app.services.routing.agent_classifier``, so that Channel Pass 1, Channel
Pass 2, App MCP Stage 1 and Identity Stage 2 share a single implementation
instead of three near-copies (plan §8 — and Bug 1, which was one of those copies
dropping ``prompt_examples`` before the prompt was rendered).

What is left here is the **adapter**: ``list[dict]`` in, ``RouteToAgentResult``
out. It exists because ``available_agents`` as a list of dicts is the published
shape of ``AIFunctionsService.route_to_agent`` and of ``app.agents``'s own
export, and both have callers outside routing. New routing consumers should
build :class:`~app.services.routing.agent_classifier.Candidate` objects and call
``AgentClassifier.classify`` directly — the dict form cannot be type-checked and
is exactly how a field goes missing in one caller and not in the others.
"""
import logging

from app.services.routing.agent_classifier import (
    AgentClassifier,
    Candidate,
    ClassificationResult,
)

logger = logging.getLogger(__name__)

#: The router's result **is** the classification result — one class under two
#: names, not two dataclasses kept in step. A second definition would be one
#: field behind the first the next time the prompt contract grows, which is the
#: same failure this whole module was collapsed to avoid.
RouteToAgentResult = ClassificationResult


def _as_candidate(agent: dict) -> Candidate:
    """One ``available_agents`` dict as a :class:`Candidate`."""
    return Candidate(
        ref_id=str(agent.get("id") or ""),
        name=str(agent.get("name") or ""),
        trigger_prompt=str(agent.get("trigger_prompt") or ""),
        prompt_examples=agent.get("prompt_examples") or None,
    )


def route_to_agent(
    message: str,
    available_agents: list[dict],
    provider_kwargs: dict | None = None,
) -> ClassificationResult | None:
    """Classify a user message and pick the best matching agent.

    Args:
        message: The user's message to classify.
        available_agents: List of dicts with keys: id, name, trigger_prompt and
            optionally prompt_examples.
        provider_kwargs: Optional kwargs passed to the provider manager.

    Returns:
        RouteToAgentResult with agent_id, optional transformed_message and the
        advisory confidence / reason / runner_up_id, or None if no agent fits or
        on parse failure.
    """
    return AgentClassifier.classify(
        [_as_candidate(agent) for agent in available_agents],
        message,
        provider_kwargs=provider_kwargs,
    )
