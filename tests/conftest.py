from datetime import UTC, datetime
from uuid import uuid4

import pytest

from demo.contracts import ChatTurn
from demo.evaluation.models import METRIC_NAMES, TurnEvaluation


@pytest.fixture
def turn():
    return ChatTurn(
        timestamp=datetime(2026, 9, 28, 8, 0, tzinfo=UTC),
        conversation_id=uuid4(),
        turn_id=uuid4(),
        trace_id="1" * 32,
        turn_index=1,
        query="How should I store apples?",
        response="Keep apples in a cool place.",
        context=[],
        prompt_version="chat-en-v2",
        model_deployment="apple-chat",
        model_version="test-model",
        duration_ms=123,
        usage=None,
    )


@pytest.fixture
def evaluation_result(turn) -> TurnEvaluation:
    return {
        "turn_id": str(turn.turn_id),
        "conversation_id": str(turn.conversation_id),
        "trace_id": turn.trace_id,
        "schema_version": "6",
        "technical_status": "completed",
        "metrics": {
            name: {
                "technical_status": "completed",
                "passed": True,
                "score": 4.0,
                "threshold": 3,
                "reason": None,
                "judge_model": "judge-test",
            }
            for name in METRIC_NAMES
        },
        "apple_scope_compliance": True,
        "reason": None,
        "eval_id": "eval-test",
        "run_id": "run-test",
    }
