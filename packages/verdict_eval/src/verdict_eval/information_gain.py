"""Information gain metrics for LLM responses.

"Information gain" in our context has three useful interpretations,
implementations of which differ in cost and what they tell you:

1. **Context faithfulness gain** (implemented). Did the model actually use
   the retrieved context, or could it have answered the same way without?
   Compares response-with-context against response-without-context using
   semantic distance. Useful for RAG observability.

2. **Prompt-response mutual information** (stubbed, v1). The dependency
   between the input prompt and the output response, measured as
   embedding-space mutual information. Tells you how much the response
   is anchored in the prompt vs. boilerplate / generic.

3. **Cross-time information gain** (stubbed, v1). How much new information
   does each response contribute over the conversation so far? Useful for
   conversation-quality analysis.

The implemented variant focuses on context faithfulness because it is directly
useful for RAG-style applications.

References:
- "Faithful Reasoning" — Creswell et al. 2022
- Vectara HHEM-2.1-Open (hallucination evaluation model)
- RAGAS faithfulness metric
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from verdict_eval.providers import CompletionRequest, LLMProvider


@dataclass
class FaithfulnessGain:
    """Did the retrieved context actually improve the response?

    For each (query, context, response_with_context) triplet:
    1. Re-run the same query WITHOUT context → response_without_context
    2. Compute semantic similarity between the two responses
    3. faithfulness_gain = 1 - similarity
       - high gain: context made a real difference (useful retrieval)
       - low gain: response was generic, context was wasted

    This is the metric a RAG operator actually cares about. Low gain across
    many queries means your retrieval isn't helping; high gain means it is.
    """

    provider: LLMProvider
    model: str
    embedder: object
    temperature: float = 0.0
    max_tokens: int = 512

    def compute(
        self,
        *,
        query: str,
        context: str,
        response_with_context: str,
    ) -> float:
        """Return faithfulness gain in [0, 1].

        0 = the response is identical with or without context (no gain)
        1 = the response is completely different (context was decisive)
        """
        # Get a response WITHOUT the context
        no_ctx_messages = [{"role": "user", "content": query}]
        no_ctx_resp = self.provider.complete(CompletionRequest(
            model=self.model,
            messages=no_ctx_messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        ))
        response_without_context = no_ctx_resp.text or ""

        # Compare via embedding similarity
        embeddings = self.embedder.embed([response_with_context, response_without_context])
        if embeddings.shape[0] < 2:
            return 0.0
        a, b = embeddings[0], embeddings[1]
        nu, nv = float(np.linalg.norm(a)), float(np.linalg.norm(b))
        if nu == 0 or nv == 0:
            return 0.0
        similarity = float(np.dot(a, b) / (nu * nv))
        return max(0.0, 1.0 - similarity)


# ---------------------------------------------------------------------------
# v1 stubs — methodology documented; not yet implemented
# ---------------------------------------------------------------------------


def prompt_response_mutual_information(*args, **kwargs) -> float:
    """v1 — mutual information between prompt and response embeddings.

    Methodology: estimate I(prompt_emb; response_emb) via a non-parametric
    k-NN estimator (Kraskov et al. 2004) on a window of (prompt, response)
    pairs. High MI = response depends on prompt; low MI = response is
    generic/boilerplate. Useful for catching when a model has degraded into
    canned responses.

    Deferred until there is a clear product requirement and enough conversation
    data to validate the metric.
    """
    raise NotImplementedError("Mutual information is v1; see methodology in docstring")


def cross_time_information_gain(*args, **kwargs) -> float:
    """v1 — how much new information each response adds across a conversation.

    Methodology: for each turn in a multi-turn conversation, compute the
    KL divergence between the response embedding distribution conditional
    on all-prior-turns vs. conditional on the immediately-prior turn. High
    cross-time gain = the conversation is making progress; low gain = stuck
    in a loop / repeating itself.

    Implementation requires multi-turn trace stitching that v0 doesn't yet
    do at the agent-run level (see v1-roadmap.md). Deferred to v1.
    """
    raise NotImplementedError("Cross-time information gain is v1; see methodology in docstring")
