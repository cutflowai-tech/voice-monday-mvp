"""Optional integration test against the REAL Monday API.

Creates one item named "[TEST] ..." on your configured board/group, verifies the
Idempotency-Key replay returns the same item, then archives it (recoverable from
the board's archive).

Run explicitly:

    RUN_MONDAY_INTEGRATION=1 pytest -m integration tests/integration
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

from voice_agent.config import load_settings
from voice_agent.connectors.base import ExecutionContext
from voice_agent.connectors.monday import MondayConfig, MondayGraphQLConnector

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_MONDAY_INTEGRATION") != "1",
        reason="set RUN_MONDAY_INTEGRATION=1 (and real MONDAY_* vars) to run",
    ),
]

ARCHIVE = "mutation Archive($id: ID!) { archive_item(item_id: $id) { id } }"


async def test_create_task_on_real_board():
    s = load_settings()
    connector = MondayGraphQLConnector(
        MondayConfig(
            api_token=s.monday_api_token,
            board_id=s.monday_board_id,
            group_id=s.monday_group_id,
            assignee_column=s.monday_assignee_column,
            assignee_column_type=s.monday_assignee_column_type,
            task_name_column=s.monday_task_name_column,
            idempotency_column=s.monday_idempotency_column,
            api_url=s.monday_api_url,
            api_version=s.monday_api_version,
        ),
        s.monday_person_ids(),
    )
    key = f"it_{uuid.uuid4().hex}"
    ctx = ExecutionContext(
        job_id="integration", idempotency_key=key, attempt=1, created_at=time.time()
    )
    payload = {
        "title": "[TEST] شراء هارد للسيرفر",
        "assignee": "Mohamed",
        "assignee_key": "mohamed",
    }
    try:
        first = await connector.execute("create_task", payload, ctx)
        second = await connector.execute("create_task", payload, ctx)  # same key -> replay
        assert first.external_id
        assert second.external_id == first.external_id
    finally:
        if "first" in locals():
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    s.monday_api_url,
                    headers={
                        "Authorization": s.monday_api_token,
                        "API-Version": s.monday_api_version,
                    },
                    json={"query": ARCHIVE, "variables": {"id": first.external_id}},
                )
        await connector.aclose()
