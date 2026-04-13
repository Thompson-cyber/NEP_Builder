```
# Phase 1 — 挖掘候选 Commit
python stage1_collect_commits.py \
    --repo      /path/to/your/repo \
    --repo_name pandas \
    --output    output/pandas/candidates.jsonl \
    --limit     500

# Phase 2 — 静态分析（支持 Ctrl+C 后断点续跑）
python stage2_call_graph_analysis.py --input     output/pandas/candidates.jsonl --output    output/pandas/analyzed.jsonl     --repo_path /path/to/your/repo

# Phase 2b — LLM 因果排序
python stage3_llm_analysis.py \
    --input     output/pandas/analyzed.jsonl \
    --old_format_output     output/pandas/old_analyzed.jsonl \
    --output    output/pandas/final_dataset.jsonl \
    --error_log output/pandas/llm_failures.jsonl
```

```
python stage2_call_graph_analysis.py  --input /home/data/yibowang/multi_completion_datas/data_collection/stage1/single_coarse_transformers.jsonl --output /home/data/yibowang/multi_completion_datas/data_collection/stage2/langchain/ --repo_path
```