#!/usr/bin/env python3
"""
root_cause_filter.py
--------------------
输入：包含 commit 数据的 .jsonl 文件（每行一个 JSON 对象）
输出：同目录下 <input>_filtered.jsonl
      仅保留 hash 以指定前缀开头（不区分大小写）的记录

用法：
    python root_cause_filter.py input.jsonl
    python root_cause_filter.py input.jsonl -o output.jsonl
"""

import json
import sys
import argparse
from pathlib import Path


# ─────────────────────────────────────────────
# 需要保留的 hash 前缀列表（不区分大小写）
# ─────────────────────────────────────────────
ALLOWED_HASH_PREFIXES = {
    "9d59e8ea",
    "1813b4a8",
    "dcf05103",
    "72a60497",
    "363c6330",
    "c0657ce6",
    "46060fed",
    "f0121b7b",
    "bc02bd52",
    "6252f99c",
    "aae87002",
    "86acf454",
    "f19ff9c5",
    "aa680bc4",
    "7baa11e9",
    "e1ec2cf3",
    "18a13f2c",
    "9f03c03f",
    "bdc5df95",
    "01c8e0be",
    "6231e1ed",
    "c0c504d1",
}


def is_allowed(hash_val: str) -> bool:
    """判断 hash 是否以允许的前缀开头（不区分大小写）"""
    if not hash_val:
        return False
    h = hash_val.lower()
    return any(h.startswith(prefix) for prefix in ALLOWED_HASH_PREFIXES)


def main():
    parser = argparse.ArgumentParser(
        description="过滤 jsonl 文件，仅保留 hash 在白名单中的记录"
    )
    parser.add_argument("input", help="输入 .jsonl 文件路径")
    parser.add_argument(
        "-o", "--output",
        help="输出文件路径（默认：<input>_filtered.jsonl）",
        default=None,
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[ERROR] 文件不存在: {input_path}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else (
        input_path.parent / (input_path.stem + "_filtered.jsonl")
    )

    total, kept, skipped_parse, skipped_filter = 0, 0, 0, 0

    with open(input_path, "r", encoding="utf-8") as fin, \
         open(output_path, "w", encoding="utf-8") as fout:

        for line_no, line in enumerate(fin, start=1):
            line = line.strip()
            if not line:
                continue

            total += 1

            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[WARN] 第 {line_no} 行 JSON 解析失败: {e}，已跳过")
                skipped_parse += 1
                continue

            hash_val = record.get("hash", "")
            if not is_allowed(hash_val):
                skipped_filter += 1
                continue

            fout.write(json.dumps(record, ensure_ascii=False) + "\n")
            kept += 1

            if kept % 500 == 0:
                print(f"  已保留 {kept} 条...")

    print(f"\n✅ 完成！")
    print(f"   总读取：{total} 条")
    print(f"   已保留：{kept} 条")
    print(f"   hash 不匹配跳过：{skipped_filter} 条")
    print(f"   JSON 解析失败跳过：{skipped_parse} 条")
    print(f"   输出文件：{output_path}")


if __name__ == "__main__":
    main()