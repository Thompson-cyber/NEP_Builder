import json
import os
from pathlib import Path

# ================= 配置区域 =================
# 输入包含 .jsonl 文件的文件夹路径
INPUT_DIR = r"D:\Data\pythonCodes\nep_builder\output\sklearn\parse_p2"

# 输出结果的文件夹路径
OUTPUT_DIR = r"D:\Data\pythonCodes\nep_builder\output\sklearn\parse_out_p2"

# Hunk 行数阈值 (超过此行数将被过滤)
MAX_HUNK_LINES = 20


# ===========================================

def process_dataset():
    # 1. 准备输入和输出目录
    input_path = Path(INPUT_DIR)
    output_path = Path(OUTPUT_DIR)
    output_path.mkdir(parents=True, exist_ok=True)

    # 2. 定义输出文件路径
    file_paths = {
        "all": output_path / "all_filtered.jsonl",  # 汇总文件
        "new_only": output_path / "split_new_only.jsonl",  # 仅新依赖
        "both": output_path / "split_both.jsonl",  # 新旧依赖都有
        "others": output_path / "split_others.jsonl"  # 其他情况
    }

    # 3. 打开所有输出文件的句柄 (使用 'w' 模式，每次运行会覆盖旧文件)
    # 使用 exit_stack 管理多个文件上下文是一个优雅的做法，但为了简单直观，这里直接打开
    files_handles = {k: open(v, 'w', encoding='utf-8') for k, v in file_paths.items()}

    # 统计计数器
    stats = {
        "total_read": 0,
        "filtered_out": 0,
        "kept_total": 0,
        "split_new_only": 0,
        "split_both": 0,
        "split_others": 0
    }

    print(f"开始处理目录: {input_path}")
    print(f"行数过滤阈值: {MAX_HUNK_LINES}")

    # 4. 遍历目录下所有 jsonl 文件
    jsonl_files = list(input_path.glob("*.jsonl"))
    if not jsonl_files:
        print("警告: 输入目录中没有找到 .jsonl 文件")
        return

    for jsonl_file in jsonl_files:
        print(f"正在处理文件: {jsonl_file.name} ...")

        with open(jsonl_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                stats["total_read"] += 1
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    print(f"跳过无效的 JSON 行: {line[:50]}...")
                    continue

                # --- 步骤 A: 过滤逻辑 ---
                should_discard = False
                # 检查 ordered_hunks 中的每一个 hunk
                for hunk in entry.get('ordered_hunks', []):
                    # 只要有一个 hunk 的旧代码或新代码超过阈值，整条数据丢弃
                    if hunk.get('old_len', 0) > MAX_HUNK_LINES or hunk.get('new_len', 0) > MAX_HUNK_LINES:
                        should_discard = True
                        break

                if should_discard:
                    stats["filtered_out"] += 1
                    continue

                # --- 步骤 B: 写入汇总文件 ---
                stats["kept_total"] += 1
                # 重新转为 json 字符串写入，确保格式统一
                json_str = json.dumps(entry, ensure_ascii=False)
                files_handles["all"].write(json_str + '\n')

                # --- 步骤 C: 根据依赖关系分流 ---
                label = entry.get('dependency_label', 'UNKNOWN')

                if label == 'NEW_ONLY':
                    files_handles["new_only"].write(json_str + '\n')
                    stats["split_new_only"] += 1
                elif label == 'BOTH':
                    files_handles["both"].write(json_str + '\n')
                    stats["split_both"] += 1
                else:
                    # 包含 OLD_ONLY 或其他未标记的情况
                    files_handles["others"].write(json_str + '\n')
                    stats["split_others"] += 1

    # 5. 关闭所有文件句柄
    for f in files_handles.values():
        f.close()

    # 6. 打印最终报告
    print("\n" + "=" * 30)
    print("处理完成！统计报告：")
    print(f"读取总数: {stats['total_read']}")
    print(f"过滤掉 (行数过大): {stats['filtered_out']}")
    print(f"保留总数: {stats['kept_total']}")
    print("-" * 20)
    print(f"  [分流] NEW_ONLY: {stats['split_new_only']}")
    print(f"  [分流] BOTH:     {stats['split_both']}")
    print(f"  [分流] OTHERS:   {stats['split_others']}")
    print("=" * 30)
    print(f"输出文件已保存在: {output_path}")


if __name__ == "__main__":
    process_dataset()