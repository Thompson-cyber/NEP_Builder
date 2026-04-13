import re

from pydriller import Repository
from loguru import logger
from typing import List, Generator, Iterator, Optional

from core.types import CommitCandidate, FileChange
from config.settings import MiningConfig
from filters.base import BaseFilter


class RepoMiner:
    def __init__(self, repo_name:str,repo_path: str, filters: List[BaseFilter]):
        self.repo_name = repo_name
        self.repo_path = repo_path
        self.filters = filters
        self.config = MiningConfig()

    def mine(self) -> Iterator[CommitCandidate]:
        repo = Repository(self.repo_path, only_no_merge=False)

        stats = {"total": 0, "error": 0, "filter_rejected": 0, "extract_none": 0, "yielded": 0}

        for commit in repo.traverse_commits():
            stats["total"] += 1
            try:
                passed = True
                for f in self.filters:
                    if not f.check(commit, self.config):
                        passed = False
                        break

                if not passed:
                    stats["filter_rejected"] += 1
                    continue

                candidate = self._extract(commit)
                if candidate is None:
                    stats["extract_none"] += 1
                    continue

                stats["yielded"] += 1
                yield candidate

            except Exception as e:
                stats["error"] += 1
                logger.warning(
                    f"[{self.repo_name}] 未捕获异常: {e} "
                    f"| commit={commit.hash[:7]} "
                    f"| 已跳过，继续下一个"
                )
                continue  # ← 核心：跳过这个 commit，不终止整个迭代

        logger.info(
            f"[{self.repo_name}] Mining 完成 | "
            f"total={stats['total']} "
            f"rejected={stats['filter_rejected']} "
            f"error={stats['error']} "
            f"extract_none={stats['extract_none']} "
            f"yielded={stats['yielded']}"
        )

    def _extract(self, commit) -> Optional[CommitCandidate]:
        try:
            # [新增] 1. 提取 Issue ID
            # 常见模式: #123, gh-123, fix #123.
            # 这里使用通用正则提取所有 # 数字
            issue_pattern = re.compile(r'#(\d+)')
            found_issues = issue_pattern.findall(commit.msg)
            # 去重
            issue_ids = list(set(found_issues))


            source_changes = []
            test_changes = []

            for f in commit.modified_files:
                fname = f.filename.lower()

                # 判定类型
                is_test = False
                for pattern in self.config.TEST_FILE_PATTERNS:
                    if pattern in fname:
                        is_test = True
                        break

                # 如果是源代码文件，检查是否在忽略列表
                if any(fname.endswith(ext) for ext in self.config.SOURCE_EXTENSIONS):
                    is_ignored = False
                    for ignore_pattern in self.config.IGNORE_FILES:
                        if ignore_pattern in fname:
                            is_ignored = True
                            break
                    if is_ignored:
                        continue
                else:
                    continue

                # 构建 FileChange 对象
                change = FileChange(
                    old_path=f.old_path,
                    new_path=f.new_path,
                    change_type=f.change_type.name,
                    diff=f.diff,
                    source_code=f.source_code,
                    is_test=is_test
                )

                if is_test:
                    test_changes.append(change)
                else:
                    source_changes.append(change)

            # 双重检查：确保提取后依然满足非空条件
            if not source_changes or not test_changes:
                return None

            return CommitCandidate(
                repo_name=self.repo_name,
                hash=commit.hash,
                msg=commit.msg,
                author_date=str(commit.author_date),
                issue_ids=issue_ids,
                repo_url=self.repo_path,
                is_merge=commit.merge,
                source_changes=source_changes,
                test_changes=test_changes,
                source_files_count=len(source_changes),
                test_files_count=len(test_changes),
                metadata={"issue_details": {}}
            )
        except Exception as e:
            logger.error(f"Error extracting commit {commit.hash}: {e}")
            return None


import os
from github import Github
from typing import List, Dict


class IssueEnricher:
    def __init__(self, token: str, repo_slug: str):
        """
        :param token: GitHub Personal Access Token
        :param repo_slug: 格式 "owner/repo" (例如 "psf/requests")
        """
        self.gh = Github(token)
        self.repo = self.gh.get_repo(repo_slug)
        # 简单缓存，避免重复请求同一个 Issue
        self.cache: Dict[str, dict] = {}

    def enrich(self, candidate: CommitCandidate) -> CommitCandidate:
        """
        接收一个 Candidate，填充其 Issue 描述信息到 metadata 中
        """
        if not candidate.issue_ids:
            return candidate

        issue_details = {}

        for issue_id in candidate.issue_ids:
            # 检查缓存
            if issue_id in self.cache:
                issue_details[issue_id] = self.cache[issue_id]
                continue

            try:
                # 调用 API 获取 Issue 对象
                # 注意：这里是网络请求，可能会慢，生产环境建议使用异步或批量查询
                gh_issue = self.repo.get_issue(int(issue_id))

                info = {
                    "title": gh_issue.title,
                    "body": gh_issue.body,  # 这是你需要的 Issue 描述内容
                    "state": gh_issue.state,
                    "labels": [l.name for l in gh_issue.labels]
                }

                # 存入缓存和结果
                self.cache[issue_id] = info
                issue_details[issue_id] = info

                logger.info(f"Fetched issue #{issue_id} for commit {candidate.hash[:7]}")

            except Exception as e:
                logger.warning(f"Failed to fetch issue #{issue_id}: {e}")
                issue_details[issue_id] = {"error": str(e)}

        # 将获取到的数据存入 metadata
        candidate.metadata["issue_details"] = issue_details

        return candidate
