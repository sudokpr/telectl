import sys
from types import SimpleNamespace

from codex_llm import CodexLlmConfig, ask_codex_text


def test_codex_stream_relays_only_final_answer(monkeypatch) -> None:
    def event(method, payload):
        return SimpleNamespace(method=method, payload=payload)

    commentary = SimpleNamespace(
        type="agentMessage",
        id="commentary-1",
        phase=SimpleNamespace(value="commentary"),
    )
    final = SimpleNamespace(
        type="agentMessage",
        id="final-1",
        phase=SimpleNamespace(value="final_answer"),
        text="Safe final answer",
    )
    events = [
        event("item/started", SimpleNamespace(item=commentary)),
        event("item/agentMessage/delta", SimpleNamespace(item_id="commentary-1", delta="Private commentary")),
        event("item/started", SimpleNamespace(item=final)),
        event("item/agentMessage/delta", SimpleNamespace(item_id="final-1", delta="Safe final answer")),
        event("item/completed", SimpleNamespace(item=final)),
        event(
            "turn/completed",
            SimpleNamespace(turn=SimpleNamespace(status=SimpleNamespace(value="completed"), error=None)),
        ),
    ]

    class FakeTurn:
        def stream(self):
            return iter(events)

    class FakeThread:
        def turn(self, *_args, **_kwargs):
            return FakeTurn()

    class FakeCodex:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def thread_start(self, **_kwargs):
            return FakeThread()

    fake_module = SimpleNamespace(
        ApprovalMode=SimpleNamespace(deny_all="deny_all"),
        Codex=FakeCodex,
        LocalImageInput=lambda **kwargs: kwargs,
        TextInput=lambda **kwargs: kwargs,
        Sandbox=SimpleNamespace(read_only="read_only", workspace_write="workspace_write", full_access="full_access"),
    )
    monkeypatch.setitem(sys.modules, "openai_codex", fake_module)
    streamed: list[str] = []
    cfg = CodexLlmConfig(cwd=None, model=None, sandbox="read_only", ephemeral=True, base_instructions=None)

    answer = ask_codex_text("Question", cfg, on_text_delta=streamed.append)

    assert answer == "Safe final answer"
    assert streamed == ["Safe final answer"]
