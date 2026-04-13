from abc import ABC, abstractmethod
from pydriller import Commit
from config.settings import MiningConfig

class BaseFilter(ABC):
    @abstractmethod
    def check(self, commit: Commit, config: MiningConfig) -> bool:
        """返回 True 表示保留该 Commit，False 表示丢弃"""
        pass
