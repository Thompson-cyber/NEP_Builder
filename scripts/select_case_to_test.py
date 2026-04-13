import json
from pathlib import Path

# ================= 配置 =================
INPUT_FILE  = r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase2\sklearn\p2_analyzed_commits.jsonl"
OUTPUT_FILE = r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase2\sklearn\p2_analyzed_commits_filter.jsonl"
# ========================================


def is_qualified(entry) -> bool:
    """
    硬性过滤条件（两者必须同时满足）：
      1. 跨文件协同修改：cross_file_dependencies > 0
      2. 依赖链长度：max_dependency_chain_length <= 2
    """
    metrics  = entry.get('new_metrics', {})
    topology = metrics.get('topology', {})

    # 条件 1：必须有跨文件依赖
    if metrics.get('cross_file_dependencies', 0) <= 0:
        return False

    # 条件 2：依赖链长度必须 <= 2
    chain_len = topology.get('max_dependency_chain_length', 0)
    if chain_len > 2:
        return False

    return True


def filter_cases():
    input_path  = Path(INPUT_FILE)
    output_path = Path(OUTPUT_FILE)

    if not input_path.exists():
        print(f"错误: 找不到输入文件 {INPUT_FILE}")
        return

    print(f"正在读取: {input_path.name} ...")

    qualified = []
    total = 0

    with open(input_path, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                total += 1
                if is_qualified(entry):
                    qualified.append(entry)
            except json.JSONDecodeError:
                continue

    # 写出结果
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        for entry in qualified:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    # 统计报告
    print("\n" + "=" * 35)
    print("过滤完成报告:")
    print(f"  输入总样本数 : {total}")
    print(f"  符合条件样本 : {len(qualified)}")
    print(f"  过滤掉样本数 : {total - len(qualified)}")
    print(f"  保留率       : {len(qualified)/total*100:.1f}%")
    print(f"  输出文件     : {output_path}")
    print("=" * 35)


if __name__ == "__main__":
    filter_cases()