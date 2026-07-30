import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / "n8n" / "telegram-vacancy-ingest.json"
README_PATH = ROOT / "n8n" / "README.md"


def load_workflow():
    return json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))


def nodes_by_name(workflow):
    return {node["name"]: node for node in workflow["nodes"]}


def test_workflow_json_is_importable_and_graph_is_complete():
    workflow = load_workflow()
    nodes = nodes_by_name(workflow)

    assert workflow["name"]
    assert workflow["active"] is False
    assert len(nodes) == len(workflow["nodes"])
    assert len({node["id"] for node in workflow["nodes"]}) == len(workflow["nodes"])
    assert {
        "Telegram Trigger",
        "Normalize Forwarded Post",
        "POST Vacancy Event",
        "IF Duplicate",
        "Duplicate - Finish",
        "Created - Finish",
    } <= nodes.keys()

    expected_types = {
        "Telegram Trigger": "n8n-nodes-base.telegramTrigger",
        "Normalize Forwarded Post": "n8n-nodes-base.code",
        "POST Vacancy Event": "n8n-nodes-base.httpRequest",
        "IF Duplicate": "n8n-nodes-base.if",
        "Duplicate - Finish": "n8n-nodes-base.set",
        "Created - Finish": "n8n-nodes-base.set",
    }
    for name, node_type in expected_types.items():
        assert nodes[name]["type"] == node_type

    for source, connection_groups in workflow["connections"].items():
        assert source in nodes
        for group in connection_groups["main"]:
            for connection in group:
                assert connection["node"] in nodes
                assert connection["type"] == "main"
                assert connection["index"] == 0

    assert workflow["connections"]["Telegram Trigger"]["main"][0][0]["node"] == (
        "Normalize Forwarded Post"
    )
    assert workflow["connections"]["Normalize Forwarded Post"]["main"][0][0][
        "node"
    ] == "POST Vacancy Event"
    assert workflow["connections"]["POST Vacancy Event"]["main"][0][0]["node"] == (
        "IF Duplicate"
    )


def test_telegram_normalizer_contract_is_stable_and_preserves_forward_metadata():
    nodes = nodes_by_name(load_workflow())
    trigger = nodes["Telegram Trigger"]
    code = nodes["Normalize Forwarded Post"]["parameters"]["jsCode"]

    assert "channel_post" in trigger["parameters"]["updates"]
    assert trigger["parameters"]["additionalFields"]["download"] is False
    assert "update.channel_post" in code
    assert "update.message" in code
    assert "message.text ?? message.caption" in code
    assert "forward_origin" in code
    assert "forward_from_chat" in code
    assert "normalized_forward" in code
    assert "attachments" in code
    for attachment_type in (
        "photo",
        "document",
        "animation",
        "video",
        "audio",
        "voice",
        "video_note",
        "sticker",
    ):
        assert f"addAttachment('{attachment_type}'" in code

    assert "idempotency_key: `telegram:${chatId}:${messageId}`" in code
    assert "chat_id: chatId" in code
    assert "message_id: messageId" in code
    assert "message_url: messageUrl" in code
    assert "telegram_update: update" in code


def test_http_request_uses_bearer_credential_and_duplicate_branches():
    workflow = load_workflow()
    nodes = nodes_by_name(workflow)
    request = nodes["POST Vacancy Event"]["parameters"]

    assert request["method"] == "POST"
    assert "$env.PARSERUSERBOT_BASE_URL" in request["url"]
    assert request["url"].endswith("+ '/vacancy-events' }}")
    assert request["authentication"] == "genericCredentialType"
    assert request["genericAuthType"] == "httpBearerAuth"
    assert request["sendBody"] is True
    assert request["rawContentType"] == "application/json"
    assert "JSON.stringify($json)" in request["body"]

    duplicate_outputs = workflow["connections"]["IF Duplicate"]["main"]
    assert duplicate_outputs[0][0]["node"] == "Duplicate - Finish"
    assert duplicate_outputs[1][0]["node"] == "Created - Finish"

    condition = nodes["IF Duplicate"]["parameters"]["conditions"]["conditions"][0]
    assert "$json.status === 'duplicate'" in condition["leftValue"]
    assert "$json.duplicate === true" in condition["leftValue"]
    assert "$json.created === false" in condition["leftValue"]

    for node_name, expected_duplicate in (
        ("Duplicate - Finish", True),
        ("Created - Finish", False),
    ):
        assignments = nodes[node_name]["parameters"]["assignments"]["assignments"]
        by_field = {assignment["name"]: assignment for assignment in assignments}
        assert set(by_field) == {"intake_id", "status", "duplicate"}
        assert by_field["duplicate"]["value"] is expected_duplicate


def test_workflow_contains_no_real_secret():
    serialized = WORKFLOW_PATH.read_text(encoding="utf-8")

    secret_patterns = (
        # Telegram BotFather token.
        r"\b\d{6,12}:[A-Za-z0-9_-]{25,}\b",
        # Common OpenAI-style API key.
        r"\bsk-[A-Za-z0-9_-]{20,}\b",
        # JWT or other concrete bearer value embedded in a header.
        r"(?i)authorization[\"']?\s*:\s*bearer\s+(?:eyJ|[A-Za-z0-9_-]{20,})",
    )
    for pattern in secret_patterns:
        assert re.search(pattern, serialized) is None

    workflow = load_workflow()
    for node in workflow["nodes"]:
        credentials = node.get("credentials", {})
        for credential in credentials.values():
            assert "token" not in credential
            assert "password" not in credential
            assert "secret" not in credential
            assert "apiKey" not in credential


def test_n8n_readme_covers_secure_setup_and_offline_fallbacks():
    readme = README_PATH.read_text(encoding="utf-8")

    assert "Import from File" in readme
    assert "PARSERUSERBOT_BASE_URL" in readme
    assert "HTTP Bearer Auth" in readme
    assert "ADMIN_TOKEN" in readme
    assert "Manual Trigger" in readme
    assert "Webhook fallback" in readme
    assert "curl --fail-with-body" in readme
    assert "telegram:{chat_id}:{message_id}" in readme
    assert "Files are not downloaded" in readme
