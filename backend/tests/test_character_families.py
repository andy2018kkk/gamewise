"""来源关联只用于发现候选；精确技能、发行分支与待审边界保持不变。"""

import json
import asyncio
import hashlib
from datetime import date

import pytest

from app.schemas.query import Game
from app.schemas.character import CharacterRecord
from app.schemas.source import SourceRecord
from app.services.data_processor import DataProcessor, EvidenceBlock, ManualExtractor, SourceDocument
from app.services.character_families import CharacterFamilies
from app.services.import_characters import import_character_files
from app.services.knowledge_tools import KnowledgeTools
from test_knowledge_store import save_batch


def write_catalog(root, *, duplicate=False, conflict=False):
    branches = []
    for branch, titles in [("a", ["合成武将", "界合成武将", "神合成武将"]), ("b", ["合成武将", "SP合成武将"])]:
        entries = [titles]
        if branch == "a" and (duplicate or conflict):
            entries.append(titles + ["另一个条目"] if conflict else titles)
        lines = ["{{消歧义|合成武将|" + "、".join(names) + "}}" for names in entries]
        (root / f"{branch}-relations.json").write_text(json.dumps({"parse": {
            "revid": 7, "wikitext": {"*": "\n".join(lines)}}}, ensure_ascii=False), encoding="utf-8")
        branches.append({"branch": branch, "groups": [{"label": "合成武将", "titles": names,
            "source_line": i + 1, "source_revision": 7, "source_raw": lines[i],
            "source_url": "https://example.com/" + branch} for i, names in enumerate(entries)]})
    (root / "review.json").write_text(json.dumps({"branches": branches}, ensure_ascii=False), encoding="utf-8")
    config = root / "family-config.json"
    config.write_text(json.dumps({"catalog_review": "review.json", "branch_game_ids": {"a": "a", "b": "b"}}), encoding="utf-8")
    return config


@pytest.fixture
def setup(tmp_path):
    database = tmp_path / "knowledge.sqlite3"
    paths = [save_batch(tmp_path, "a-root"),
        save_batch(tmp_path, "a-child", character="界合成武将", text="出牌阶段，弃1张牌。"),
        save_batch(tmp_path, "b-root", "b", text="另一发行分支的原文。"),
        save_batch(tmp_path, "b-child", "b", character="SP合成武将")]
    import_character_files(database, paths, [Game(game_id="a", name="甲"), Game(game_id="b", name="乙")])
    config = write_catalog(tmp_path)
    return database, config


def discover(session, **kwargs):
    return session.invoke("discover_catalog", json.dumps({"character": "合成武将", "include_related": True, **kwargs}))


def shared_page(root, source_id, members, game_id="a"):
    blocks, entries = [], []
    for i, name in enumerate(members):
        text = f"出牌阶段，摸{i + 1}张牌。"
        blocks += [EvidenceBlock(block_id=f"meta-{i}", text=f"{name} 经典 武将包 包甲 吴", locator=f"第{i}节属性"),
                   EvidenceBlock(block_id=f"skill-{i}", text=text, verbatim_text=text, locator=f"第{i}节技能")]
        data = CharacterRecord(character=name, mode="经典", package_kind="武将包", package_name="包甲", faction="吴",
                               skills=[{"skill_name": "合成技能", "skill_text": text}])
        entries.append({"data": data.model_dump(), "block_ids": [f"meta-{i}", f"skill-{i}"]})
    body = "\n".join(b.text for b in blocks)
    digest = hashlib.sha256(body.encode()).hexdigest()
    raw = root / f"{source_id}-{digest[:8]}.txt"
    raw.write_bytes(body.encode("utf-8"))
    source = SourceRecord(source_id=source_id, title=source_id, game_id=game_id, topic="合成同页测试", source_type="local",
        source_locator="https://example.com/" + source_id, content_path=str(raw), collected_at=date(2026, 11, 18), content_hash=digest)
    batch = DataProcessor(CharacterRecord).process(SourceDocument(source=source, blocks=tuple(blocks)), ManualExtractor(entries))
    path = root / f"{source_id}-{digest[:8]}.json"
    path.write_text(batch.model_dump_json(), encoding="utf-8")
    return path


def test_missing_series_is_added_from_same_snapshot_with_separate_evidence(setup, tmp_path):
    database, config = setup
    paths = [shared_page(tmp_path, "shared-a", ("合成武将", "新系列合成武将")),
             shared_page(tmp_path, "shared-b", ("合成武将", "乙服特有条目"), "b"),
             save_batch(tmp_path, "unrelated", character="谋合成武将")]
    import_character_files(database, paths, [Game(game_id="a", name="甲"), Game(game_id="b", name="乙")])
    session = KnowledgeTools(database, ["a"], preview=True, character_families=CharacterFamilies.from_config(config))
    family = discover(session).results[0].family
    assert [c.character for c in family.candidates] == ["合成武将", "界合成武将", "神合成武将", "新系列合成武将"]
    extra = family.candidates[-1]
    assert extra.same_page_evidence[0].related_to == "合成武将"
    assert extra.same_page_evidence[0].source_id == "shared-a"
    assert len(extra.same_page_evidence[0].content_hash) == 64
    assert family.source.source_url == "https://example.com/a"
    assert not family.candidates[0].same_page_evidence
    exact = session.invoke("search_records", '{"character":"新系列合成武将"}')
    assert {r.character for r in exact.results[0].records} == {"新系列合成武将"}


def test_same_source_id_across_snapshots_does_not_invent_a_relation(setup, tmp_path):
    database, config = setup
    paths = [shared_page(tmp_path, "changed-page", ("合成武将",)),
             shared_page(tmp_path, "changed-page", ("另一人物",))]
    import_character_files(database, paths, [Game(game_id="a", name="甲")])
    result = discover(KnowledgeTools(database, ["a"], character_families=CharacterFamilies.from_config(config)))
    assert "另一人物" not in [c.character for c in result.results[0].family.candidates]


def test_discovery_has_source_missing_candidate_and_no_skill_body(setup):
    database, config = setup
    session = KnowledgeTools(database, ["a"], character_families=CharacterFamilies.from_config(config))
    response = discover(session)
    result, = response.results
    assert result.catalog.characters == ["合成武将", "界合成武将"]
    assert [c.character for c in result.family.candidates] == ["合成武将", "界合成武将", "神合成武将"]
    assert result.family.candidates[-1].coverage == "not_loaded"
    assert result.family.source.source_url == "https://example.com/a"
    assert result.family.source.revision == 7 and result.family.source.lines == [1]
    assert result.family.review_status == "pending" and not response.answerable
    assert not result.records and "skill_text" not in response.model_dump_json()


def test_exact_catalog_and_skill_query_still_do_not_expand(setup):
    database, config = setup
    session = KnowledgeTools(database, ["a"], preview=True, character_families=CharacterFamilies.from_config(config))
    exact = discover(session, include_related=False).results[0]
    assert exact.catalog.characters == ["合成武将"] and exact.family is None
    response = session.invoke("search_records", '{"character":"界合成武将"}')
    assert [r.character for r in response.results[0].records] == ["界合成武将"]
    assert response.results[0].records[0].skill_text == "出牌阶段，弃1张牌。"


def test_default_multiple_branches_stay_separate_and_unauthorized_branch_is_denied(setup):
    database, config = setup
    families = CharacterFamilies.from_config(config)
    both = discover(KnowledgeTools(database, ["a", "b"], character_families=families))
    assert [r.game_id for r in both.results] == ["a", "b"]
    assert "界合成武将" in both.results[0].catalog.characters
    assert "界合成武将" not in both.results[1].catalog.characters
    assert "SP合成武将" in both.results[1].catalog.characters
    denied = discover(KnowledgeTools(database, ["a"], character_families=families), game_ids=["b"])
    assert denied.error.code == "scope_denied" and not denied.results


@pytest.mark.parametrize("name,args", [
    ("discover_catalog", {"include_related": True}),
    ("discover_catalog", {"include_related": True, "character": "合成武将", "package_name": "包甲"}),
    ("discover_catalog", {"include_related": True, "record_type": "rule", "character": "合成武将"}),
    ("search_records", {"include_related": True, "character": "合成武将"}),
])
def test_related_option_cannot_broaden_other_queries(setup, name, args):
    database, config = setup
    response = KnowledgeTools(database, ["a"], character_families=CharacterFamilies.from_config(config)).invoke(name, json.dumps(args))
    assert response.error.code == "invalid_arguments" and not response.results


def test_unknown_group_and_unconfigured_catalog_do_not_guess_prefixes(setup):
    database, config = setup
    missing = discover(KnowledgeTools(database, ["a"]))
    assert missing.error.code == "scope_denied"
    response = discover(KnowledgeTools(database, ["a"], character_families=CharacterFamilies.from_config(config)), character="新合成武将")
    assert response.results[0].family.status == "not_registered"
    assert not response.results[0].family.candidates


def test_identical_duplicate_groups_keep_both_source_lines(setup):
    database, config = setup
    write_catalog(config.parent, duplicate=True)
    response = discover(KnowledgeTools(database, ["a"], character_families=CharacterFamilies.from_config(config)))
    assert len(response.results[0].family.candidates) == 3
    assert response.results[0].family.source.lines == [1, 2]


def test_conflicting_groups_do_not_silently_merge_members(setup):
    database, config = setup
    write_catalog(config.parent, conflict=True)
    response = discover(KnowledgeTools(database, ["a"], character_families=CharacterFamilies.from_config(config)))
    result = response.results[0]
    assert result.family.status == "conflict" and not result.family.candidates
    assert result.catalog.characters == ["合成武将"]


@pytest.mark.parametrize("field,value", [("source_revision", 99), ("titles", ["捏造候选"])])
def test_modified_relation_manifest_is_rejected(setup, field, value):
    _, config = setup
    path = config.parent / "review.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["branches"][0]["groups"][0][field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="不一致"):
        CharacterFamilies.from_config(config)




def skill_records(response):
    return [record for result in response.results for record in result.records]


def test_person_skill_call_expands_registered_family_and_reports_missing(setup):
    database, config = setup
    session = KnowledgeTools(database, ["a", "b"], preview=True, max_records=1,
                             character_families=CharacterFamilies.from_config(config))
    response = session.invoke("get_character_skills", '{"characters":["合成武将"],"game_ids":["a"]}')
    assert response.error is None
    records = skill_records(response)
    assert {r.character for r in records} == {"合成武将", "界合成武将"}
    assert {r.game_id for r in records} == {"a"}
    assert any("神合成武将" in note for note in response.notes)
    assert all(result.target.mode == "经典" for result in response.results if result.records)
    for record in records:
        assert session.resolve_citation(record.citation_id).skill_text == record.skill_text


def test_specific_series_stays_exact_and_overlapping_requests_do_not_duplicate(setup):
    database, config = setup
    session = KnowledgeTools(database, ["a"], preview=True, character_families=CharacterFamilies.from_config(config))
    exact = session.invoke("get_character_skills", '{"characters":["界合成武将"]}')
    assert {r.character for r in skill_records(exact)} == {"界合成武将"}
    combined = session.invoke("get_character_skills", '{"characters":["合成武将","界合成武将"]}')
    records = skill_records(combined)
    assert len(records) == 2 and len({r.record_id for r in records}) == 2


def test_person_call_preserves_all_modes_without_old_record_limit(setup, tmp_path):
    database, config = setup
    paths = [save_batch(tmp_path, "root-national", mode="国战"), save_batch(tmp_path, "root-duel", mode="1V1")]
    import_character_files(database, paths, [Game(game_id="a", name="甲")])
    session = KnowledgeTools(database, ["a"], preview=True, max_records=1,
                             character_families=CharacterFamilies.from_config(config))
    all_modes = session.invoke("get_character_skills", '{"characters":["合成武将"]}')
    root_records = [r for r in skill_records(all_modes) if r.character == "合成武将"]
    assert {r.mode for r in root_records} == {"经典", "国战", "1V1"}
    classic = session.invoke("get_character_skills", '{"characters":["合成武将"],"mode":"经典"}')
    assert {r.mode for r in skill_records(classic)} == {"经典"}


def test_family_expansion_rejects_unallowed_branch_and_preserves_pending_boundary(setup):
    database, config = setup
    family = CharacterFamilies.from_config(config)
    session = KnowledgeTools(database, ["a"], preview=True, character_families=family)
    denied = session.invoke("get_character_skills", '{"characters":["合成武将"],"game_ids":["b"]}')
    assert denied.error.code == "scope_denied" and not denied.results
    pending = KnowledgeTools(database, ["a"], character_families=family).invoke("get_character_skills", '{"characters":["合成武将"]}')
    assert not skill_records(pending)
    assert any(result.status == "pending_review" for result in pending.results)


def test_expansion_output_failure_does_not_publish_partial_citations(setup):
    database, config = setup
    session = KnowledgeTools(database, ["a"], preview=True, max_characters=1,
                             character_families=CharacterFamilies.from_config(config))
    response = session.invoke("get_character_skills", '{"characters":["合成武将"]}')
    assert response.error.code == "output_limit" and not response.results
    assert session._evidence == {}


def test_complete_family_return_can_disable_character_limit(setup):
    database, config = setup
    session = KnowledgeTools(database, ["a", "b"], preview=True, max_characters=None,
                             character_families=CharacterFamilies.from_config(config))
    response = session.invoke("get_character_skills", '{"characters":["合成武将"]}')
    assert response.error is None
    records = skill_records(response)
    assert {r.game_id for r in records} == {"a", "b"}
    assert {r.character for r in records} == {"合成武将", "界合成武将", "SP合成武将"}
