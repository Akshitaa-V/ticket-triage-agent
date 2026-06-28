from ticket_agent import input_guard, agent


def test_guard_allows_valid_payload():
    result = input_guard({"ticket_id": "T-1", "text": "My app crashed"})
    assert result.allowed is True


def test_guard_blocks_missing_text():
    result = input_guard({"ticket_id": "T-2"})
    assert result.allowed is False
    assert "text" in result.reason


def test_guard_blocks_missing_ticket_id():
    result = input_guard({"text": "Something broke"})
    assert result.allowed is False
    assert "ticket_id" in result.reason


def test_guard_blocks_empty_string_field():
    result = input_guard({"ticket_id": "T-3", "text": "   "})
    assert result.allowed is False


def test_valid_billing_ticket_escalates():
    state = {"ticket_id": "T-4", "text": "refund my payment now", "category": "", "priority": "", "action": "", "response": ""}
    result = agent.invoke(state)
    assert result["action"] == "escalated_to_human"


def test_valid_general_ticket_auto_responds():
    state = {"ticket_id": "T-5", "text": "what are your hours", "category": "", "priority": "", "action": "", "response": ""}
    result = agent.invoke(state)
    assert result["action"] == "auto_acknowledged"