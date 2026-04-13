import re
from typing import List, Optional, Dict
from core.types import Hunk


class DiffSlicer:
    """
    解析 Git Diff 文本，生成 Hunk 对象。
    修正：去除 Hunk 首尾的上下文（Context），只计算实际变更区域的 Start 和 Len。
    """

    FILE_HEADER_PATTERN = re.compile(r'^diff --git a/(.*) b/(.*)$')
    HUNK_HEADER_PATTERN = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*')

    def slice(self, diff_text: str) -> List[Hunk]:
        if not diff_text or not isinstance(diff_text, str):
            return []

        hunks: List[Hunk] = []
        lines = diff_text.splitlines()

        current_file = None
        current_hunk_lines = []
        current_hunk_meta: Optional[Dict[str, int]] = None

        i = 0
        while i < len(lines):
            line = lines[i]

            # --- 1. 检测文件头 ---
            if line.startswith("diff --git"):
                if current_hunk_meta and current_file:
                    self._add_hunk(hunks, current_file, current_hunk_meta, current_hunk_lines)
                    current_hunk_lines = []
                    current_hunk_meta = None

                match = self.FILE_HEADER_PATTERN.match(line)
                if match:
                    current_file = match.group(2)
                else:
                    parts = line.split()
                    if len(parts) >= 4:
                        current_file = parts[-1].lstrip('b/')
                i += 1
                continue

            # --- 2. 检测 Hunk 头 ---
            if line.startswith("@@"):
                if current_hunk_meta and current_file:
                    self._add_hunk(hunks, current_file, current_hunk_meta, current_hunk_lines)
                    current_hunk_lines = []

                match = self.HUNK_HEADER_PATTERN.match(line)
                if match:
                    try:
                        old_start = int(match.group(1))
                        new_start = int(match.group(3))
                        current_hunk_meta = {
                            'old_start': old_start,
                            'new_start': new_start,
                        }
                    except ValueError:
                        current_hunk_meta = None
                else:
                    current_hunk_meta = None

                # 注意：这里不把 @@ 行加入 current_hunk_lines，
                # 因为我们要手动计算偏移，不依赖原始 diff 的上下文结构
                i += 1
                continue

            # --- 3. 收集内容 ---
            if current_hunk_meta:
                # 过滤掉 Git 的元数据行
                if line.startswith("index ") or line.startswith("--- ") or line.startswith("+++ "):
                    i += 1
                    continue
                # 忽略 "No newline" 提示，它不影响行号逻辑
                if line.startswith("\\ No newline"):
                    i += 1
                    continue

                current_hunk_lines.append(line)

            i += 1

        # 结算最后一个 Hunk
        if current_hunk_meta and current_file:
            self._add_hunk(hunks, current_file, current_hunk_meta, current_hunk_lines)

        return hunks

    def _add_hunk(self, hunks: List[Hunk], file_path: str, meta: Dict[str, int], lines: List[str]):
        """
        创建 Hunk 对象。
        逻辑：找到第一个和最后一个变更行，裁剪掉首尾的上下文。
        """
        if not lines:
            return

        # 1. 找到实际变更的起始和结束索引
        first_change_idx = -1
        last_change_idx = -1

        for idx, line in enumerate(lines):
            if line.startswith('+') or line.startswith('-'):
                if first_change_idx == -1:
                    first_change_idx = idx
                last_change_idx = idx

        # 如果没有发现任何变更（全是上下文，理论上不应该发生），则忽略此 Hunk
        if first_change_idx == -1:
            return

        # 2. 计算首部上下文的行数偏移
        # lines[0 : first_change_idx] 都是上下文
        # 上下文行在旧版本和新版本中都存在，所以偏移量是一样的
        prefix_context_count = 0
        for i in range(first_change_idx):
            if lines[i].startswith(' '):
                prefix_context_count += 1

        # 3. 截取有效变更区域 (Trimmed Lines)
        # 包含从第一个变更到最后一个变更之间的所有行（包括中间夹杂的上下文）
        trimmed_lines = lines[first_change_idx: last_change_idx + 1]

        # 4. 计算有效区域内的长度
        calc_old_len = 0
        calc_new_len = 0

        for line in trimmed_lines:
            if line.startswith('-'):
                calc_old_len += 1
            elif line.startswith('+'):
                calc_new_len += 1
            elif line.startswith(' '):
                # 中间的上下文，同时计入旧版和新版长度
                calc_old_len += 1
                calc_new_len += 1

        # 5. 计算最终的 Start Line
        # 原始 Start + 首部被跳过的上下文行数
        final_old_start = meta['old_start'] + prefix_context_count
        final_new_start = meta['new_start'] + prefix_context_count

        # 6. 生成 ID (使用新版本行号)
        hunk_id = f"{file_path}:{final_new_start}"

        # 重新组合 content，只保留有效部分，或者保留全部但标记范围？
        # 通常为了显示方便，Content 最好还是保留上下文。
        # 但为了分析准确，Start/Len 必须是 Trim 过的。
        # 这里我们将 content 设置为 trimmed_lines，这样 content 和 len 是对应的。
        trimmed_content = "\n".join(trimmed_lines)

        hunk = Hunk(
            id=hunk_id,
            file_path=file_path,
            content=trimmed_content,  # 注意：现在的 content 不包含首尾的多余上下文

            old_start_line=final_old_start,
            old_len=calc_old_len,

            new_start_line=final_new_start,
            new_len=calc_new_len,

            # 兼容字段
            start_line=final_new_start,
            end_line=final_new_start + calc_new_len - 1 if calc_new_len > 0 else final_new_start
        )
        hunks.append(hunk)
