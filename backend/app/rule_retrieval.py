"""规则检索实验的本地命令入口：建索引、查询、开发集对照；不生成答案。"""

import argparse
import json
from pathlib import Path
import time

from app.providers.local_embedding import LocalE5Embedding
from app.services.rule_search import search_rule_index, write_rule_index

WORKSPACE = Path(__file__).resolve().parents[2]
ROOT = WORKSPACE / "data/local/embedding-preview.local"


def evaluate(index_dir: Path, provider, dataset: Path) -> dict:
    data = json.loads(dataset.read_text(encoding="utf-8"))
    cases = data["cases"]
    positive_count = sum(bool(c["evidence"]) for c in cases)
    if not positive_count:
        raise ValueError("开发集没有预期证据")
    results, metrics = [], []
    for method in ("keyword", "vector", "hybrid"):
        for case in cases:
            started = time.perf_counter()
            hits = search_rule_index(index_dir, case["question"], provider=provider, top_k=10, method=method)
            for hit in hits:
                hit["matched_evidence"] = [i for i, expected in enumerate(case["evidence"])
                    if hit["source_url"].endswith("/" + expected["page"]) and expected["contains"] in hit["text"]]
            results.append({"method": method, "id": case["id"], "question": case["question"],
                            "expected": case["evidence"], "seconds": round(time.perf_counter() - started, 4),
                            "hits": hits})
        for k in (3, 5, 10):
            hit_total, recall_total, reciprocal_total = 0, 0.0, 0.0
            for row in [r for r in results if r["method"] == method and r["expected"]]:
                top = row["hits"][:k]
                covered = {i for h in top for i in h["matched_evidence"]}
                hit_total += bool(covered)
                recall_total += len(covered) / len(row["expected"])
                ranks = [h["rank"] for h in top if h["matched_evidence"]]
                reciprocal_total += 1 / min(ranks) if ranks else 0
            metrics.append({"method": method, "top_k": k, "questions": positive_count,
                            "hit_rate": hit_total / positive_count,
                            "evidence_recall": recall_total / positive_count,
                            "mrr": reciprocal_total / positive_count})
    return {"dataset": data["name"], "status": data["status"], "metrics": metrics, "results": results,
            "limitations": ["开发集结果，不是独立评测或答案准确率", "来源页与原文短语匹配，相关性仍需人工抽查",
                            "负例只展示返回结果，尚未实现可靠拒答", "未使用回答LLM、未生成正式答案"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=ROOT / "model")
    parser.add_argument("--index-dir", type=Path, default=ROOT / "index")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--processed-dir", type=Path, default=WORKSPACE / "data/local/rules-processed.local")
    query = commands.add_parser("query")
    query.add_argument("question")
    query.add_argument("--top-k", type=int, default=5)
    query.add_argument("--method", choices=["keyword", "vector", "hybrid"], default="vector")
    dev = commands.add_parser("evaluate")
    dev.add_argument("--dataset", type=Path, default=WORKSPACE / "evals/datasets/rule-retrieval-dev.json")
    dev.add_argument("--output", type=Path, default=ROOT / "retrieval-dev-results.json")
    args = parser.parse_args()
    provider = (None if args.command == "query" and args.method == "keyword"
                else LocalE5Embedding(args.model_dir))
    if args.command == "build":
        result = write_rule_index(args.processed_dir, args.index_dir, provider, provider.identity)
    elif args.command == "query":
        result = search_rule_index(args.index_dir, args.question, provider=provider,
                                   top_k=args.top_k, method=args.method)
    else:
        result = evaluate(args.index_dir, provider, args.dataset)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# 规则检索开发实验", "", "10个正例、2个范围外问题；预期证据为开发草案，尚未由用户验收。",
                 "命中定义：来源页与预设原文短语同时匹配；不是答案准确率，也可能漏算其他同样有效的证据。",
                 "关键词对照为简单中文双字词重合，不是BM25。各方式使用相同片段、规则集及类别过滤；hybrid使用RRF，候选各20条。", "",
                 "| 方式 | Top K | 至少命中一条预设证据 | 预设证据召回率 |",
                 "| --- | --- | --- | --- |"]
        for metric in result["metrics"]:
            lines.append(f"| {metric['method']} | {metric['top_k']} | {metric['hit_rate']:.0%} | {metric['evidence_recall']:.0%} |")
        lines.extend(["", "以下为纯向量与组合检索的实际Top 3，保留漏检与范围外结果；分数不是正确概率。", ""])
        for row in [r for r in result["results"] if r["method"] in {"vector", "hybrid"}]:
            lines.extend([f"## {row['method']} / {row['id']}：{row['question']}", ""])
            for hit in row["hits"][:3]:
                lines.extend([f"### 第{hit['rank']}名：{hit['heading']}", "",
                              f"来源：{hit['source_url']}；相似度：{hit['score']:.4f}", "", hit["text"], ""])
        args.output.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
        result = {"dataset": result["dataset"], "metrics": result["metrics"], "output": str(args.output)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
