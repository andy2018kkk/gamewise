"""开发评测区分命中、证据是否找齐及无答案问题，不调用模型。"""

import json

from app.rule_retrieval import evaluate


def test_top_k_counts_all_required_evidence_and_keeps_negative_cases(tmp_path, monkeypatch):
    def search(*args, **kwargs):
        return [{"rank": rank, "source_url": "https://example.com/" + ("a" if rank == 1 else "b"),
                 "text": "目标证据" if rank in {1, 6} else "其他内容"} for rank in range(1, 7)]
    monkeypatch.setattr("app.rule_retrieval.search_rule_index", search)
    dataset = {"name": "test", "status": "开发草案", "cases": [
        {"id": "positive", "question": "测试", "evidence": [{"page": "a", "contains": "目标证据"},
                                                            {"page": "b", "contains": "目标证据"}]},
        {"id": "negative", "question": "范围外", "evidence": []}]}
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")
    result = evaluate(tmp_path, None, path)
    for metric in result["metrics"]:
        assert metric["questions"] == 1 and metric["hit_rate"] == 1
        assert metric["evidence_recall"] == (1 if metric["top_k"] == 10 else 0.5)
    assert {r["method"] for r in result["results"] if not r["expected"]} == {"keyword", "vector", "hybrid"}
