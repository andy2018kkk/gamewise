"""只使用合成元数据，验证批量校验行为，不声称真实资料已审核。"""

import json
from copy import deepcopy
from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas.source import SourceBatch


@pytest.fixture
def record() -> dict[str, object]:
    return {
        "source_id": "fixture-source-a",
        "title": "合成资料甲",
        "game_id": "fixture-game-a",
        "version_ids": ["fixture-version-a"],
        "topic": "合成测试主题",
        "content_path": "fixtures/not-a-real-file.md",
        "source_type": "web",
        "source_locator": "https://example.com/fixture-a",
        "collected_at": "2026-10-28",
        "content_hash": "synthetic-content-identifier",
        "usage_note": "仅为测试构造的元数据，不含真实网页内容",
        "review_status": "approved",
        "review_note": "模拟审核记录，不代表真实资料审核",
    }


def test_valid_json_batch_supports_multiple_games(record: dict[str, object]) -> None:
    other = {**record, "source_id": "fixture-source-b", "game_id": "fixture-game-b",
             "version_ids": ["fixture-version-b"]}
    batch = SourceBatch.model_validate_json(json.dumps({"sources": [record, other]}))

    assert len(batch.sources) == 2
    assert batch.sources[0].collected_at == date(2026, 10, 28)
    assert batch.sources[1].game_id == "fixture-game-b"
    assert batch.sources[0].content_path == "fixtures/not-a-real-file.md"


def test_unknown_version_defaults_to_pending(record: dict[str, object]) -> None:
    for key in ("version_ids", "review_status", "content_hash", "usage_note", "review_note"):
        record.pop(key)

    source = SourceBatch.model_validate({"sources": [record]}).sources[0]
    assert source.review_status == "pending"
    assert source.version_ids == []
    assert source.content_hash is None


def test_empty_batch_means_no_sources() -> None:
    assert SourceBatch.model_validate({"sources": []}).sources == []


@pytest.mark.parametrize("field", [
    "source_id", "title", "game_id", "topic", "content_path",
    "source_type", "source_locator", "collected_at",
])
def test_missing_required_field_is_rejected(record: dict[str, object], field: str) -> None:
    record.pop(field)
    with pytest.raises(ValidationError) as error:
        SourceBatch.model_validate({"sources": [record]})
    assert error.value.errors()[0]["loc"] == ("sources", 0, field)


@pytest.mark.parametrize("field", [
    "source_id", "title", "game_id", "topic", "content_path",
    "source_locator", "content_hash", "usage_note", "review_note",
])
def test_blank_text_is_rejected(record: dict[str, object], field: str) -> None:
    record[field] = " \t\n "
    with pytest.raises(ValidationError):
        SourceBatch.model_validate({"sources": [record]})


@pytest.mark.parametrize("url", ["not-a-url", "ftp://example.com/file", "file:///local.md"])
def test_web_source_requires_http_url(record: dict[str, object], url: str) -> None:
    record["source_locator"] = url
    with pytest.raises(ValidationError) as error:
        SourceBatch.model_validate({"sources": [record]})
    assert error.value.errors()[0]["loc"] == ("sources", 0, "source_locator")


def test_local_locator_is_not_required_to_be_a_url(record: dict[str, object]) -> None:
    record.update(source_type="local", source_locator="合成手册：第2节")
    assert SourceBatch.model_validate({"sources": [record]}).sources[0].source_type == "local"


@pytest.mark.parametrize("value", ["2026-02-30", "not-a-date", 0, True])
def test_invalid_dates_are_rejected(record: dict[str, object], value: object) -> None:
    record["collected_at"] = value
    with pytest.raises(ValidationError) as error:
        SourceBatch.model_validate({"sources": [record]})
    assert error.value.errors()[0]["loc"] == ("sources", 0, "collected_at")


@pytest.mark.parametrize("field", ["version_ids", "usage_note", "review_note", "content_hash"])
def test_approved_requires_review_information(record: dict[str, object], field: str) -> None:
    record.pop(field)
    with pytest.raises(ValidationError) as error:
        SourceBatch.model_validate({"sources": [record]})
    assert error.value.errors()[0]["loc"][:2] == ("sources", 0)
    assert field in error.value.errors()[0]["msg"]


@pytest.mark.parametrize("value", ["accepted", "APPROVED", True])
def test_unknown_review_status_is_rejected(record: dict[str, object], value: object) -> None:
    record["review_status"] = value
    with pytest.raises(ValidationError):
        SourceBatch.model_validate({"sources": [record]})


def test_rejected_record_with_unknown_version_cannot_bypass_pending(record: dict[str, object]) -> None:
    record.update(version_ids=[], review_status="rejected")
    with pytest.raises(ValidationError):
        SourceBatch.model_validate({"sources": [record]})


def test_blank_version_id_is_rejected(record: dict[str, object]) -> None:
    record["version_ids"] = [" "]
    with pytest.raises(ValidationError) as error:
        SourceBatch.model_validate({"sources": [record]})
    assert error.value.errors()[0]["loc"] == ("sources", 0, "version_ids", 0)


def test_duplicate_source_id_reports_second_record(record: dict[str, object]) -> None:
    other = deepcopy(record)
    other["source_id"] = "  fixture-source-a  "
    with pytest.raises(ValidationError) as error:
        SourceBatch.model_validate({"sources": [record, other]})
    assert error.value.errors()[0]["loc"] == ("sources", 1, "source_id")


def test_invalid_second_record_rejects_whole_batch_without_mutating_input(record: dict[str, object]) -> None:
    other = {**record, "source_id": "fixture-source-b", "title": "  "}
    payload = {"sources": [record, other]}
    original = deepcopy(payload)

    with pytest.raises(ValidationError) as error:
        SourceBatch.model_validate(payload)

    assert error.value.errors()[0]["loc"] == ("sources", 1, "title")
    assert payload == original


@pytest.mark.parametrize("level", ["batch", "record"])
def test_unknown_fields_are_rejected(record: dict[str, object], level: str) -> None:
    payload: dict[str, object] = {"sources": [record]}
    if level == "batch":
        payload["soruces"] = []
    else:
        record["game_name"] = "不能替代 game_id"
    with pytest.raises(ValidationError) as error:
        SourceBatch.model_validate(payload)
    assert error.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("payload", [{}, {"sources": None}, {"sources": "not-a-list"}])
def test_invalid_batch_structure_is_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SourceBatch.model_validate(payload)
