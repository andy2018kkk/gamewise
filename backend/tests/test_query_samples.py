"""离线待审样本查询实验；全部资料由测试人工构造。"""

import json

import pytest
from pydantic import ValidationError

from experiments.query_samples import Game, load_samples, discover_catalog, search_records


@pytest.fixture
def samples(tmp_path):
    games = [Game(game_id="a", name="合成服甲"), Game(game_id="b", name="合成服乙"), Game(game_id="c", name="未收录服")]
    paths = []
    for game, modes in (("a", ("经典", "1V1")), ("b", ("经典",))):
        records = []
        for mode in modes:
            body = "令对手摸2张牌。" if mode == "1V1" else "令其他角色摸2张牌。"
            records.append({
                "record_id": f"{game}-{mode}",
                "data": {"character": "合成武将", "mode": mode, "skill_name": "合成技能", "skill_text": body},
                "evidence": [{"block_id": mode, "text": body, "verbatim_text": body, "locator": f"章节：{mode}"}],
            })
        payload = {"source": {
            "source_id": f"source-{game}", "title": "合成页面", "game_id": game,
            "version_ids": [], "topic": "合成测试", "content_path": "fixtures/synthetic.json",
            "source_type": "web", "source_locator": f"https://example.com/{game}",
            "collected_at": "2026-10-28", "review_status": "pending",
        }, "review_status": "pending", "records": records,
            "unprocessed_block_ids": [], "all_blocks_accounted_for": True}
        path = tmp_path / f"{game}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        paths.append(path)
    return games, paths


def test_catalog_defaults_to_all_registered_games_and_keeps_gaps(samples):
    games, paths = samples
    store = load_samples(paths, games)
    result = discover_catalog(store, character="合成武将")
    assert [(item.game_id, item.status, item.record_count) for item in result] == [
        ("a", "sample_available", 2), ("b", "sample_available", 1), ("c", "not_collected", 0)]
    assert result[0].modes == ["1V1", "经典"]


def test_search_limits_game_and_preserves_full_mode_text_and_source(samples):
    store = load_samples(samples[1], samples[0])
    result = search_records(store, game_ids=["a"], character="合成武将", mode="1V1")
    assert len(result) == 1
    assert len(result[0].records) == 1
    record = result[0].records[0]
    assert record.skill_text == "令对手摸2张牌。"
    assert record.source_locator == "https://example.com/a"
    assert record.locators == ["章节：1V1"]
    assert record.version_ids == []
    assert record.review_status == "pending"
    assert record.answerable is False


def test_skill_reverse_lookup_without_character(samples):
    store = load_samples(samples[1], samples[0])
    result = search_records(store, skill_name="合成技能")
    assert sum(len(item.records) for item in result) == 3
    assert result[2].status == "not_collected"


def test_filters_intersect_and_missing_match_does_not_mean_missing_game(samples):
    store = load_samples(samples[1], samples[0])
    result = search_records(store, game_ids=["a"], character="另一个武将", skill_name="合成技能")
    assert result[0].status == "no_match_in_sample"
    assert result[0].records == []


@pytest.mark.parametrize("filters", [
    {}, {"character": " "}, {"skill_name": ""}, {"character": "合成武将", "mode": " "},
    {"character": "合成武将", "game_ids": []},
    {"character": "合成武将", "game_ids": ["unknown"]},
    {"character": "合成武将", "game_ids": ["a", "a"]},
])
def test_invalid_query_is_rejected(samples, filters):
    store = load_samples(samples[1], samples[0])
    with pytest.raises((ValueError, ValidationError)):
        search_records(store, **filters)


def test_missing_and_invalid_files_are_errors_not_no_data(samples, tmp_path):
    with pytest.raises(FileNotFoundError):
        load_samples([tmp_path / "missing.json"], samples[0])
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_samples([bad], samples[0])


@pytest.mark.parametrize("mutation", ["unknown_game", "duplicate_record", "wrong_text", "no_evidence"])
def test_invalid_sample_is_rejected(samples, mutation):
    games, paths = samples
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    if mutation == "unknown_game":
        payload["source"]["game_id"] = "unregistered"
    elif mutation == "duplicate_record":
        payload["records"].append(payload["records"][0])
    elif mutation == "wrong_text":
        payload["records"][0]["data"]["skill_text"] = "改成摸100张牌。"
    else:
        payload["records"][0]["evidence"] = []
    paths[0].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load_samples(paths, games)


def test_duplicate_sources_and_games_rejected(samples):
    games, paths = samples
    with pytest.raises(ValueError):
        load_samples([paths[0], paths[0]], games)
    with pytest.raises(ValueError):
        load_samples(paths, games + [games[0]])


def test_query_results_cannot_mutate_loaded_samples(samples):
    store = load_samples(samples[1], samples[0])
    first = search_records(store, character="合成武将")
    first[0].records[0].skill_text = "被调用方修改"
    assert search_records(store, character="合成武将")[0].records[0].skill_text == "令其他角色摸2张牌。"


@pytest.fixture
def character_samples(samples):
    games, paths = samples
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for record in payload["records"]:
            old = record["data"]
            record["data"] = {
                "character": old["character"], "mode": old["mode"],
                "package_kind": "武将包", "package_name": "合成包甲" if payload["source"]["game_id"] == "a" else "合成包乙",
                "faction": "吴" if old["mode"] == "经典" else "其他势力", "hp_raw": "4/5",
                "skills": [{"skill_name": old["skill_name"], "skill_text": old["skill_text"], "tags": None}],
            }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return games, paths


def test_new_format_queries_by_package_and_faction(character_samples):
    store = load_samples(character_samples[1], character_samples[0], record_format="character")
    result = search_records(store, package_name="合成包甲", faction="吴")
    assert [len(item.records) for item in result] == [1, 0, 0]
    record = result[0].records[0]
    assert record.mode == "经典" and record.package_name == "合成包甲"
    assert record.faction == "吴" and record.hp_raw == "4/5"
    assert record.source_locator == "https://example.com/a" and not record.answerable
    assert record.tags is None


def test_new_catalog_filters_package_and_lists_available_attributes(character_samples):
    store = load_samples(character_samples[1], character_samples[0], record_format="character")
    result = discover_catalog(store, package_name="合成包甲", faction="吴")
    assert result[0].record_count == 1
    assert result[0].packages == ["合成包甲"] and result[0].factions == ["吴"]
    assert result[1].status == "no_match_in_sample" and result[2].status == "not_collected"


def test_multiple_skills_are_searchable_independently_with_distinct_ids(character_samples):
    games, paths = character_samples
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    payload["records"][0]["data"]["skills"].append({"skill_name": "另一技能", "skill_text": "准备阶段摸一张牌。"})
    payload["records"][0]["evidence"].append({"block_id": "other", "text": "准备阶段摸一张牌。", "locator": "另一技能原文"})
    paths[0].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    store = load_samples(paths, games, record_format="character")
    result = search_records(store, character="合成武将", game_ids=["a"])
    assert len(result[0].records) == 3
    assert len({record.record_id for record in result[0].records}) == 3
    reverse = search_records(store, skill_name="另一技能")
    assert len(reverse[0].records) == 1 and reverse[1].records == []
    assert reverse[0].records[0].skill_text == "准备阶段摸一张牌。"


def test_new_format_preserves_game_and_mode_isolation(character_samples):
    store = load_samples(character_samples[1], character_samples[0], record_format="character")
    result = search_records(store, game_ids=["a"], character="合成武将", mode="1V1", package_name="合成包甲")
    assert len(result) == 1 and len(result[0].records) == 1
    assert result[0].records[0].skill_text == "令对手摸2张牌。"


def test_old_format_does_not_invent_missing_attributes(samples):
    store = load_samples(samples[1], samples[0])
    assert all(not item.records for item in search_records(store, faction="吴"))
    record = search_records(store, character="合成武将")[0].records[0]
    assert record.faction is None and record.package_name is None


@pytest.mark.parametrize("filters", [{"package_name": " "}, {"faction": ""}])
def test_new_attribute_filters_reject_blank_values(character_samples, filters):
    store = load_samples(character_samples[1], character_samples[0], record_format="character")
    with pytest.raises(ValidationError):
        search_records(store, **filters)


def test_new_format_rejects_altered_nested_skill_text(character_samples):
    games, paths = character_samples
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    payload["records"][0]["data"]["skills"][0]["skill_text"] = "擅自改写100张牌。"
    paths[0].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load_samples(paths, games, record_format="character")


def test_format_must_be_explicit_and_valid(character_samples):
    games, paths = character_samples
    with pytest.raises(ValidationError):
        load_samples(paths, games)
    with pytest.raises(ValueError):
        load_samples(paths, games, record_format="guess")
