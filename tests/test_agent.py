from __future__ import annotations

import json

import httpx

from voice_agent.conversation.agent import StructuredTaskAgent


async def test_agent_uses_strict_schema_and_verified_directory(directory):
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["text"]["format"]["strict"] is True
        assert body["text"]["format"]["schema"]["additionalProperties"] is False
        assert body["model"] == "test-model"
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "action": "submit",
                                        "assignee_key": "ahmed",
                                        "task": "شراء هارد 2 تيرا",
                                        "question": None,
                                    }
                                ),
                            }
                        ]
                    }
                ],
            },
        )

    agent = StructuredTaskAgent(
        api_key="test",
        model="test-model",
        directory=directory,
        transport=httpx.MockTransport(respond),
    )
    decision = await agent.decide("حطي لأحمد يشتري هارد اتنين تيرا", None)
    assert decision.assignee_key == "ahmed"
    assert "2 تيرا" in decision.task
    await agent.aclose()


async def test_openrouter_uses_strict_chat_completion(directory):
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["provider"]["require_parameters"] is True
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "submit",
                                    "assignee_key": "mohamed",
                                    "task": "شراء هارد 2 تيرا",
                                    "question": None,
                                }
                            )
                        }
                    }
                ]
            },
        )

    agent = StructuredTaskAgent(
        api_key="test",
        model="openai/gpt-4.1-mini",
        provider="openrouter",
        directory=directory,
        transport=httpx.MockTransport(respond),
    )
    decision = await agent.decide("حطي لمحمد يشتري هارد", None)
    assert decision.assignee_key == "mohamed"
    await agent.aclose()
