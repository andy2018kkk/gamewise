"""合成网页测试；自动测试不联网或下载真实攻略。"""

import json

import pytest

from experiments.preview_cheatsheet import extract_items, write_preview


def test_preserves_conditions_numbers_section_links_and_raw_html():
    html = '''<nav>忽略导航</nav>
<h3><a>区域甲</a></h3>
<li data-id="sample_1" class="s_ng+">When you have the <a href="/key">Key</a>,
 take <b>2</b> stones &amp; return.<script>不要读取这里</script></li>
<h3>区域乙</h3><li data-id="other">别的区域</li>'''
    item, = extract_items(html, ["sample_1"])
    assert item.text == "When you have the Key, take 2 stones & return."
    assert item.section == "区域甲"
    assert item.classes == "s_ng+"
    assert item.source_line == 3
    assert item.source_html == html[html.index('<li data-id="sample_1"'):html.index('</li>') + 5]
    assert item.linked_urls == ["https://zkjellberg.github.io/key"]


@pytest.mark.parametrize("html, ids", [
    ("<h3>区域</h3>", ["missing"]),
    ('<h3>区域</h3><li data-id="a">x</li><li data-id="a">y</li>', ["a"]),
    ('<li data-id="a">x</li>', ["a"]),
    ('<h3>区域</h3><li data-id="a">   </li>', ["a"]),
    ('<h3>区域</h3><li data-id="a">x', ["a"]),
    ('<h3>区域</h3><li data-id="a"><ul><li>子项</li></ul></li>', ["a"]),
    ('<h3>区域</h3><li data-id="a"><table></table></li>', ["a"]),
    ("", []),
    ("", ["a", "a"]),
])
def test_refuses_missing_ambiguous_or_unsupported_samples(html, ids):
    with pytest.raises(ValueError):
        extract_items(html, ids)


def test_writes_traceable_pending_preview_without_assuming_versions(tmp_path):
    snapshot = tmp_path / "input.html"
    snapshot.write_text('<h3>区域</h3><li data-id="a">Take 2 keys.</li>', encoding="utf-8")
    output = tmp_path / "output"
    write_preview(snapshot, output, ["a"])
    report = json.loads((output / "preview.json").read_text(encoding="utf-8"))
    source, = report["source_batch"]["sources"]
    assert source["review_status"] == "pending"
    assert source["version_ids"] == []
    assert len(source["content_hash"]) == 64
    assert report["items"][0]["source_html"] in snapshot.read_text(encoding="utf-8")
    assert report["items"][0]["text"] in (output / "preview.md").read_text(encoding="utf-8")


def test_invalid_sample_does_not_create_output(tmp_path):
    snapshot = tmp_path / "input.html"
    snapshot.write_text("<h3>区域</h3>", encoding="utf-8")
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        write_preview(snapshot, output, ["missing"])
    assert not output.exists()
