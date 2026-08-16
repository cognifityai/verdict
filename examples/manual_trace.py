"""Manual instrumentation with @verdict.trace.

Auto-instrumentation captures the LLM call. Use @verdict.trace for the
surrounding business logic — retrieval, reranking, tool execution.
"""

import verdict
from anthropic import Anthropic

verdict.init(service_name="example-rag-bot", storage="sqlite:///./verdict.db")

client = Anthropic()


@verdict.trace("retrieve_documents", index="prod-kb")
def retrieve(query: str) -> list[str]:
    # Pretend we hit a vector DB
    return [
        "Observability is the practice of instrumenting systems to expose internal state.",
        "It comes from control theory and is now standard practice in modern infrastructure.",
    ]


@verdict.trace("rerank_documents")
def rerank(query: str, docs: list[str]) -> list[str]:
    return sorted(docs, key=lambda d: -d.lower().count(query.lower().split()[0]))


def ask(query: str) -> str:
    docs = retrieve(query)
    docs = rerank(query, docs)
    context = "\n\n".join(f"- {d}" for d in docs)
    prompt = f"Context:\n{context}\n\nQuestion: {query}\n\nAnswer concisely."
    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    print(ask("What is observability?"))
