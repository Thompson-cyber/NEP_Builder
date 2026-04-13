import json
import re
import os
import argparse


def extract_fim_data(diff_str: str):
    """
    解析 unified diff 字符串，提取新增代码的起始行、结束行和 ground_truth。
    （适用于 Single Hunk 模式）
    """
    lines = diff_str.splitlines()

    new_line_num = 0
    start_line = -1
    end_line = -1
    ground_truth = []

    for line in lines:
        if line.startswith('@@'):
            # 解析 @@ -old,old_len +new,new_len @@
            # 提取新文件的起始行号
            m = re.search(r'\+(\d+)(?:,\d+)?', line)
            if m:
                new_line_num = int(m.group(1))
        elif line.startswith('+') and not line.startswith('+++'):
            if start_line == -1:
                start_line = new_line_num
            # 去掉开头的 '+' 并保留缩进
            ground_truth.append(line[1:])
            end_line = new_line_num
            new_line_num += 1
        elif line.startswith('-') and not line.startswith('---'):
            # 删除的行不影响新文件的行号推进
            pass
        else:
            # 上下文行（以空格开头，或者空行）
            if not line.startswith('---') and not line.startswith('+++'):
                new_line_num += 1

    # 如果没有新增行（比如纯删除的 commit），则返回 None
    if start_line == -1:
        return None

    # 按照 moatless 的习惯，ground_truth 通常以换行符结尾
    return start_line, end_line, '\n'.join(ground_truth) + '\n'


def convert_to_completion_dataset(input_file: str, output_file: str):
    """
    将挖掘出的 candidate jsonl 转换为评估用的 task jsonl
    """
    if not os.path.exists(input_file):
        print(f"Error: Input file {input_file} not found.")
        return

    os.makedirs(os.path.dirname(os.path.abspath(output_file)) or '.', exist_ok=True)

    success_count = 0
    skip_count = 0

    with open(input_file, 'r', encoding='utf-8') as infile, \
            open(output_file, 'w', encoding='utf-8') as outfile:

        for line in infile:
            if not line.strip():
                continue

            commit_data = json.loads(line)
            repo_name = commit_data.get("repo_name", "unknown")
            commit_hash = commit_data.get("hash", "")
            repo_path = commit_data.get("repo_url", "")

            # 遍历所有的 source_changes (Single 模式下通常只有 1 个)
            for idx, change in enumerate(commit_data.get("source_changes", [])):
                diff = change.get("diff", "")
                file_path = change.get("new_path", "")

                fim_data = extract_fim_data(diff)
                if not fim_data:
                    skip_count += 1
                    continue

                start_line, end_line, ground_truth = fim_data

                # 根据文件后缀推断语言
                language = "python"
                if file_path.endswith(".java"):
                    language = "java"
                elif file_path.endswith(".ts"):
                    language = "typescript"

                # 构建唯一的 task_id
                task_id = f"{repo_name}_{commit_hash[:7]}_{idx}"

                # 构建符合 FillRequest 的数据结构
                completion_task = {
                    "task_id": task_id,
                    "hash": commit_hash,
                    "repo_path": repo_path,
                    "file_path": file_path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "language": language,
                    "max_iterations": 5,
                    "ground_truth": ground_truth
                }

                outfile.write(json.dumps(completion_task, ensure_ascii=False) + '\n')
                success_count += 1

    print(f"✅ Conversion complete!")
    print(f"🎉 Successfully generated {success_count} tasks.")
    if skip_count > 0:
        print(f"⚠️ Skipped {skip_count} changes (e.g., pure deletions without additions).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert mined commits to completion tasks")
    parser.add_argument("--input", type=str, default=r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase2\extracted_by_hash.jsonl",
                        help="Input JSONL file")
    parser.add_argument("--output", type=str, default=r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase2\extracted_by_hash_converted.jsonl", help="Output JSONL file")

    args = parser.parse_args()
    convert_to_completion_dataset(args.input, args.output)
