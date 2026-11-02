"""链接仅提供候选关系，不代表已采集、同一身份或可回答。"""

import pytest

from experiments.bwiki_relations import CatalogCandidate, discover_related
from experiments.preview_bwiki import SampleTree


def test_only_same_branch_catalog_relations_are_candidates():
    html = ('<a href="/sgs/%E7%A5%9E%E5%88%98%E5%A4%87#技能">神刘备</a>'
            '<a href="/sgs/曹操">曹操</a><a href="/sgsol/威刘备">威刘备</a>'
            '<a href="https://example.com/sgs/威刘备">威刘备</a>')
    tree = SampleTree(html)
    tree.feed(html)
    candidates = [CatalogCandidate(branch=b, page_id=i, title=n, source_group=g) for b, i, n, g in [
        ("sgs", 1, "刘备", "刘备"), ("sgs", 2, "神刘备", "刘备"),
        ("sgs", 3, "威刘备", "刘备"), ("sgs", 4, "曹操", "曹操"), ("sgsol", 5, "威刘备", "刘备")]]
    result = discover_related(tree, "sgs", 1, "刘备", candidates)
    assert [(r.title, r.linked_from_page) for r in result] == [("神刘备", True), ("威刘备", False)]
    assert all(r.status == "not_in_batch" for r in result)


def test_unknown_identity_fails_and_empty_group_never_guesses_prefix():
    tree = SampleTree('<a href="/sgs/神刘备">神刘备</a>')
    tree.feed(tree.html)
    candidates = [CatalogCandidate(branch="sgs", page_id=1, title="刘备"),
                  CatalogCandidate(branch="sgs", page_id=2, title="神刘备")]
    assert discover_related(tree, "sgs", 1, "刘备", candidates) == []
    with pytest.raises(ValueError, match="候选目录"):
        discover_related(tree, "sgs", 99, "刘备", candidates)
