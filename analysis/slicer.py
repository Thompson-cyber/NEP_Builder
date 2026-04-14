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
        if not lines:
            return

        CONTEXT_LINES = 3  # 前后上下文行数，可配置

        # ── 第一阶段：预计算每行的行号偏移 ──────────────────────────────
        old_offsets = []
        new_offsets = []
        old_off, new_off = 0, 0
        for line in lines:
            old_offsets.append(old_off)
            new_offsets.append(new_off)
            if line.startswith('-'):
                old_off += 1
            elif line.startswith('+'):
                new_off += 1
            elif line.startswith(' '):
                old_off += 1
                new_off += 1

        # ── 第二阶段：识别纯变更簇 ───────────────────────────────────────
        # 每个簇记录: {'start_idx': int, 'end_idx': int}
        clusters = []
        current_cluster = None

        for idx, line in enumerate(lines):
            if line.startswith('+') or line.startswith('-'):
                if current_cluster is None:
                    current_cluster = {'start_idx': idx, 'end_idx': idx}
                else:
                    current_cluster['end_idx'] = idx
            else:
                if current_cluster is not None:
                    clusters.append(current_cluster)
                    current_cluster = None

        if current_cluster is not None:
            clusters.append(current_cluster)

        if not clusters:
            return

        # ── 第三阶段：为每个簇计算上下文窗口，生成 Hunk ─────────────────
        for i, cluster in enumerate(clusters):
            s = cluster['start_idx']
            e = cluster['end_idx']

            # 前窗口：不越过上一个簇的末尾
            if i > 0:
                prev_end = clusters[i - 1]['end_idx']
                ctx_start = max(s - CONTEXT_LINES, prev_end + 1)
            else:
                ctx_start = max(s - CONTEXT_LINES, 0)

            # 后窗口：不越过下一个簇的起始
            if i < len(clusters) - 1:
                next_start = clusters[i + 1]['start_idx']
                ctx_end = min(e + CONTEXT_LINES, next_start - 1)
            else:
                ctx_end = min(e + CONTEXT_LINES, len(lines) - 1)

            # 提取该 Hunk 的完整行（变更行 + 前后上下文）
            hunk_lines = lines[ctx_start: ctx_end + 1]

            # start_line 指向第一个变更行（不是上下文起始）
            final_old_start = meta['old_start'] + old_offsets[s]
            final_new_start = meta['new_start'] + new_offsets[s]

            # len 只统计变更行（不含上下文）
            calc_old_len = sum(1 for line in lines[s: e + 1] if line.startswith('-'))
            calc_new_len = sum(1 for line in lines[s: e + 1] if line.startswith('+'))

            hunk = Hunk(
                id=f"{file_path}:{final_new_start}",
                file_path=file_path,
                content="\n".join(hunk_lines),
                old_start_line=final_old_start,
                old_len=calc_old_len,
                new_start_line=final_new_start,
                new_len=calc_new_len,
                start_line=final_new_start,
                end_line=final_new_start + calc_new_len - 1 if calc_new_len > 0 else final_new_start
            )
            hunks.append(hunk)
