from .base import BaseFilter
from pydriller import Commit
from config.settings import MiningConfig

"""
忽略 DELETE/ADD 操作	只关注 MODIFY 类型变更
测试文件检测	匹配 TEST_FILE_PATTERNS
源码文件检测	.py 结尾 + 非测试 + 非忽略
必须有测试	has_test == True
源文件数量限制	MIN_SOURCE_FILES ≤ count ≤ MAX_SOURCE_FILES
源码行数变动限制	MIN_SOURCE_LOC ≤ loc ≤ MAX_SOURCE_LOC
"""


def count_hunks(modified_file) -> int:
    """
    计算一个 ModifiedFile 中连续修改块（Hunk）的数量。
    将 added 和 deleted 的行号合并后，按连续性分组计数。
    """
    parsed = modified_file.diff_parsed
    added_lines = [lineno for lineno, _ in parsed.get("added", [])]
    deleted_lines = [lineno for lineno, _ in parsed.get("deleted", [])]

    all_lines = sorted(set(added_lines + deleted_lines))

    if not all_lines:
        return 0

    hunk_count = 1
    for i in range(1, len(all_lines)):
        if all_lines[i] - all_lines[i - 1] > 1:
            hunk_count += 1

    return hunk_count


class BenchmarkFilter(BaseFilter):
    """
    Benchmark 专用过滤器

    逻辑：
    1. 必须是 Merge Commit
    2. 必须包含至少一个 Source File 修改 (作为 Input/Target)
    3. 必须包含至少一个 Test File 修改 (作为 Verifier)
    4. Source Files 的规模必须在阈值范围内 (Test Files 的规模不限)
    """

    def check(self, commit: Commit, config: MiningConfig) -> bool:

        source_files_count = 0
        test_files_count = 0
        source_loc_change = 0
        source_hunk_count = 0
        has_test = False

        for f in commit.modified_files:
            if f.change_type.name == 'DELETE':
                return False
            if f.change_type.name == 'ADD':
                return False
            if len([lineno for lineno, _ in f.diff_parsed.get("added", [])]) == 0:
                return False

            fname = f.filename.lower()

            # 判定是否为测试文件
            is_test = False
            for pattern in config.TEST_FILE_PATTERNS:
                if pattern in fname:
                    is_test = True
                    has_test = True
                    test_files_count += 1
                    break

            # 判定是否为有效源码文件
            if any(fname.endswith(ext) for ext in config.SOURCE_EXTENSIONS) and not is_test:
                is_ignored = False
                for ignore_pattern in config.IGNORE_FILES:
                    if ignore_pattern in fname:
                        is_ignored = True
                        break

                if not is_ignored:
                    source_files_count += 1
                    source_loc_change += (f.added_lines + f.deleted_lines)
                    source_hunk_count += count_hunks(f)
        # 2. 核心检查逻辑
        # print(f"测试文件数量：{test_files_count};源码文件数量:{source_files_count}")
        # A. 必须有测试
        # if not has_test:
        #     return False
        # B. 必须有源码，且数量符合要求
        if not (config.MIN_SOURCE_FILES <= source_files_count <= config.MAX_SOURCE_FILES):
            return False

        # C. 源码修改规模符合要求
        if not (config.MIN_SOURCE_LOC <= source_loc_change <= config.MAX_SOURCE_LOC):
            return False
        # D. Hunk 数量必须符合要求
        if not (config.MIN_SOURCE_HUNKS <= source_hunk_count <= config.MAX_SOURCE_HUNKS):
            return False
        return True
