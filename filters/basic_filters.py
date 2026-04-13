from .base import BaseFilter
from pydriller import Commit
from config.settings import MiningConfig
import os


class MergeCommitFilter(BaseFilter):
    """排除 Merge Commit"""

    def check(self, commit: Commit, config: MiningConfig) -> bool:
        return not commit.merge


class RelevantCodeFilter(BaseFilter):
    """
    核心过滤器：决定一个 Commit 是否值得保留。
    逻辑：
    1. 必须包含至少一个 源代码 文件修改。
    2. 该文件不能在忽略列表 (IGNORE_FILES) 中。
    3. 如果 REQUIRE_TEST_CHANGE 为 True，则还必须包含测试文件。
    """

    def check(self, commit: Commit, config: MiningConfig) -> bool:
        has_valid_source = False
        has_test = False

        for f in commit.modified_files:
            fname = f.filename.lower()

            # 1. 检查是否是测试
            is_current_file_test = False
            for pattern in config.TEST_FILE_PATTERNS:
                if pattern in fname:
                    has_test = True
                    is_current_file_test = True
                    break

            # 2. 检查是否是有效源码
            # 条件：是 .py 结尾 + 不是测试文件 + 不是黑名单文件
            if fname.endswith('.py') and not is_current_file_test:
                is_ignored = False
                for ignore_pattern in config.IGNORE_FILES:
                    if ignore_pattern in fname:
                        is_ignored = True
                        break

                if not is_ignored:
                    has_valid_source = True

        # 决策逻辑
        if config.REQUIRE_TEST_CHANGE:
            return has_valid_source and has_test
        else:
            # 宽松模式：只要有有效源码修改即可
            # 注意：我们通常不希望只抓取测试文件的修改（那是测试重构，不是代码演进）
            # 所以这里强制要求 has_valid_source
            return has_valid_source


class SizeFilter(BaseFilter):
    """
    筛选修改规模。
    注意：只统计 .py 文件的规模，忽略文档等杂项。
    """

    def check(self, commit: Commit, config: MiningConfig) -> bool:
        # 筛选出所有 Python 文件 (包括测试)
        py_files = [
            f for f in commit.modified_files
            if f.filename.endswith('.py')
        ]

        # 检查文件数量
        if not (config.MIN_MODIFIED_FILES <= len(py_files) <= config.MAX_MODIFIED_FILES):
            return False

        # 检查代码行数变动 (只计算 Python 文件)
        total_change = 0
        for f in py_files:
            total_change += (f.added_lines + f.deleted_lines)

        if not (config.MIN_LOC_CHANGE <= total_change <= config.MAX_LOC_CHANGE):
            return False

        return True