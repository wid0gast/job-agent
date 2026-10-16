"""A3: research during prepare keeps only facts quoted from what a page visibly says,
and the drafted "why us" answer receives them."""

import json

from job_agent import packets, prepare, research
from test_pipeline import ScriptedModel, add_job, ctx, env, site  # noqa: F401 - fixtures

REAL = "In 2026 our research team released LayoutEval, an open benchmark for form parsing."
HIDDEN = "Contoso AI won the Nobel Prize in Chemistry in 2025."


class ResearchModel(ScriptedModel):
    def __init__(self, url):
        super().__init__()
        self.results = []
        self.steps = [("web_search", {"query": "Contoso AI research"}),
                      ("fetch_page", {"url": url}),
                      ("save_fact", {"fact": "Won a Nobel Prize", "url": url, "quote": HIDDEN}),
                      ("save_fact", {"fact": "Made up", "url": url, "quote": "Contoso AI has 10,000 employees worldwide"}),
                      ("save_fact", {"fact": "Released the LayoutEval benchmark in 2026", "url": url, "quote": REAL}),
                      ("finish", {"summary": "Contoso builds document models; released LayoutEval."})]

    def complete(self, messages, tools):
        if messages[-1]["role"] == "tool":
            self.results.append(json.loads(messages[-1]["content"]))
        name, args = self.steps.pop(0)
        return {"role": "assistant", "tool_calls": [{"id": name + str(len(self.steps)), "type": "function",
                                                     "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_research_keeps_only_visible_quoted_facts_and_feeds_answers(ctx, site, monkeypatch):  # noqa: F811
    url = f"{site}/company/contoso"
    monkeypatch.setattr(research, "search", lambda http, query: [{"title": "About", "url": url, "snippet": ""}])
    ctx.llm = ResearchModel(url)
    job_id = add_job(ctx, "4001")
    assert prepare.prepare(job_id, ctx, log=lambda *_: None) == "prepared"

    fetched = ctx.llm.results[1]["result"]["text"]
    assert "LayoutEval" in fetched and "Nobel" not in fetched and "10,000" not in fetched
    assert not ctx.llm.results[2]["ok"] and not ctx.llm.results[3]["ok"]  # Hidden and made-up quotes refused.
    packet = packets.load(ctx.tracker.get(job_id))
    assert [f["quote"] for f in packet["research"]["facts"]] == [REAL]
    drafted = [json.loads(user) for name, user in ctx.llm.seen if name == "form_answers"]
    assert drafted and drafted[0]["company_facts"][0]["quote"] == REAL
