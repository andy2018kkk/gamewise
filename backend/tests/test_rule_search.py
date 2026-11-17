"""检索存储与过滤使用向量替身，不下载或加载真实模型。"""

import pytest

np = pytest.importorskip("numpy")

from app.services.rule_chunks import RuleChunk
from app.services.rule_search import fuse_rankings, read_rule_context, search_rule_index, write_rule_index


class FakeEmbedding:
    identity = {"model_id": "test", "revision": "v1"}

    def count_passage(self, text):
        return len(text)

    def encode(self, texts, *, query=False):
        return np.asarray([[1, 0] if "伤害" in text else [0, 1] for text in texts], dtype=np.float32)


@pytest.fixture
def index(tmp_path, monkeypatch):
    chunks = [RuleChunk("a", "r1", "s1", "https://example.com/rules", "sanguosha-rules-3.0",
                        "基础规则参考", "伤害流程", "伤害被防止。", 0, 6),
              RuleChunk("b", "r2", "s2", "https://example.com/history", "sanguosha-rules-3.0",
                        "历史武将说明", "旧伤害技能", "伤害旧技能。", 0, 6),
              RuleChunk("c", "r3", "s3", "https://example.com/other", "other-rules",
                        "基础规则参考", "另一规则集", "伤害说明。", 0, 5)]
    monkeypatch.setattr("app.services.rule_search.load_rule_chunks", lambda *a, **kw: chunks)
    provider = FakeEmbedding()
    write_rule_index(tmp_path, tmp_path / "index", provider, provider.identity)
    return tmp_path / "index", provider


def test_scope_filtering_applies_to_vector_and_keyword(index):
    root, provider = index
    for method in ("keyword", "vector", "hybrid"):
        hits = search_rule_index(root, "伤害", provider=provider, top_k=10, method=method)
        assert [h["chunk_id"] for h in hits] == ["a"]
        assert hits[0]["source_url"] == "https://example.com/rules"
        assert hits[0]["text"] == "伤害被防止。" and not hits[0]["answerable"]
    assert search_rule_index(root, "伤害", provider=provider, category="不存在") == []


def test_model_and_file_mismatch_are_not_reported_as_no_evidence(index):
    root, provider = index
    provider.identity = {"model_id": "test", "revision": "v2"}
    with pytest.raises(ValueError, match="模型版本"):
        search_rule_index(root, "伤害", provider=provider)
    (root / "chunks.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="索引文件"):
        search_rule_index(root, "伤害", provider=None, method="keyword")


def test_keyword_no_match_and_invalid_inputs(index):
    root, provider = index
    assert search_rule_index(root, "zzzz", provider=None, method="keyword") == []
    for question, top_k in [("", 5), ("伤害", 0), ("伤害", 21)]:
        with pytest.raises(ValueError):
            search_rule_index(root, question, provider=provider, top_k=top_k)


def test_navigation_is_filtered_before_ranking_without_losing_body_on_index_page(tmp_path, monkeypatch):
    chunks = [RuleChunk("nav", "nav", "s", "https://example.com/glossary/index.html",
                        "sanguosha-rules-3.0", "基础规则参考", "用语 / 用语导引", "主公 体力上限", 0, 7),
              RuleChunk("body", "body", "s", "https://example.com/flow/index.html",
                        "sanguosha-rules-3.0", "基础规则参考", "流程 / 本部分提要", "主公的规则正文。", 0, 8)]
    monkeypatch.setattr("app.services.rule_search.load_rule_chunks", lambda *a, **kw: chunks)
    provider = FakeEmbedding()
    write_rule_index(tmp_path, tmp_path / "index", provider, provider.identity)
    for method in ("keyword", "vector", "hybrid"):
        hits = search_rule_index(tmp_path / "index", "主公 体力上限", provider=provider, method=method, top_k=1)
        assert [h["chunk_id"] for h in hits] == ["body"]
        assert hits[0]["content_type"] == "body"
    diagnostic = search_rule_index(tmp_path / "index", "主公 体力上限", provider=None,
                                   method="keyword", top_k=1, include_navigation=True)
    assert diagnostic[0]["chunk_id"] == "nav"


def test_fusion_deduplicates_shared_hits_and_uses_rank_not_incompatible_scores():
    keyword = [{"chunk_id": "a", "score": 1.0}, {"chunk_id": "b", "score": 0.5}]
    vector = [{"chunk_id": "b", "score": 0.99}, {"chunk_id": "c", "score": 0.98}]
    hits = fuse_rankings([keyword, vector], 3)
    assert [h["chunk_id"] for h in hits] == ["b", "a", "c"]
    assert hits[0]["score"] == pytest.approx(1 / 62 + 1 / 61)
    assert set(hits[0]["retrieval"]) == {"keyword", "vector"}


def test_context_only_reads_previously_retrieved_ids_and_marks_truncation(index):
    root, provider = index
    hits = search_rule_index(root, "伤害", provider=provider, method="hybrid")
    allowed = {h["chunk_id"] for h in hits}
    full = read_rule_context(root, "a", allowed_chunk_ids=allowed)
    assert full["text"] == "伤害被防止。" and not full["truncated"]
    short = read_rule_context(root, "a", allowed_chunk_ids=allowed, max_chars=2)
    assert short["text"] == "伤害" and short["truncated"]
    with pytest.raises(ValueError, match="本次召回"):
        read_rule_context(root, "b", allowed_chunk_ids=allowed)


def test_context_reconstructs_overlapping_chunks_exactly(tmp_path, monkeypatch):
    from app.services.rule_chunks import split_spans
    text = "条件甲。完整规则乙。特殊例外丙。" * 15
    chunks = [RuleChunk(str(i), "parent", "source", "https://example.com/rules", "sanguosha-rules-3.0",
                        "基础规则参考", "伤害流程", text[start:end], start, end)
              for i, (start, end) in enumerate(split_spans(text, "伤害流程", max_chars=45, overlap=10))]
    monkeypatch.setattr("app.services.rule_search.load_rule_chunks", lambda *a, **kw: chunks)
    provider = FakeEmbedding()
    root = tmp_path / "index"
    write_rule_index(tmp_path, root, provider, provider.identity)
    result = read_rule_context(root, chunks[1].chunk_id, allowed_chunk_ids={chunks[1].chunk_id})
    assert result["text"] == text and not result["truncated"]
