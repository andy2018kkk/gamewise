"""切分不丢原文，长度受限且重叠可追溯；不加载真实模型。"""

import pytest

from app.services.rule_chunks import split_spans


def test_long_text_is_covered_without_gaps_and_offsets_are_exact():
    text = "条件：若受到伤害，执行效果。\n例外：若伤害被防止，则不执行。\n" * 30
    spans = split_spans(text, "测试章节", max_chars=100, overlap=20)
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(0 < end - start <= 100 for start, end in spans)
    assert all(a < c <= b < d for (a, b), (c, d) in zip(spans, spans[1:]))
    covered = set(i for start, end in spans for i in range(start, end))
    assert covered == set(range(len(text)))
    assert split_spans(text, "测试章节", max_chars=100, overlap=20) == spans


def test_token_budget_includes_heading_and_never_silently_truncates():
    text = "完整规则。" * 50
    spans = split_spans(text, "标题", token_count=len, max_tokens=40)
    assert all(len(f"标题\n{text[start:end]}") <= 40 for start, end in spans)
    assert spans[-1][1] == len(text)
    with pytest.raises(ValueError, match="标题过长"):
        split_spans(text, "标题" * 100, token_count=len, max_tokens=10)


def test_invalid_split_config_is_rejected():
    with pytest.raises(ValueError):
        split_spans("正文", "标题", max_chars=10, overlap=10)


def test_overlap_starts_at_sentence_boundary_when_available():
    text = "第一条。第二条。第三条。第四条。第五条。"
    spans = split_spans(text, "标题", max_chars=10, overlap=3)
    assert all(start == 0 or text[start - 1] == "。" for start, _ in spans)
    assert spans[-1][1] == len(text)
