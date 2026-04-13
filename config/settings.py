import os
from dotenv import load_dotenv

load_dotenv()
class MiningConfig:
    # --- 阈值设置 (仅针对 Source Files) ---
    # --- 源文件扩展名 ---
    SOURCE_EXTENSIONS = [".py", ".java", ".ts",".tsx",".go",".js",".cjs",".mjs",".jsx"]

    # --- 测试文件模式 ---
    TEST_FILE_PATTERNS = ["test","test_", "_test.py", "tests/","tests", ".spec.ts", ".test.ts"]  # 添加 Java 和 TS 测试文件模式
    FLAG = 'Multi'
    if FLAG == 'Single':
        MIN_SOURCE_LOC = 1  # 至少改了5行功能代码
        MAX_SOURCE_LOC = 50  # 功能代码变动不宜过大

        MIN_SOURCE_FILES = 1  # 至少改动1个功能文件
        MAX_SOURCE_FILES = 1  # 一次 Feature 不应修改太多功能文件

        MIN_SOURCE_HUNKS = 1  # 只有1个Hunk无法构建排序任务，直接丢弃
        MAX_SOURCE_HUNKS = 1
    else:
        # 我们只关心功能代码的规模
        MIN_SOURCE_LOC = 3  # 至少改了5行功能代码
        MAX_SOURCE_LOC = 20  # 功能代码变动不宜过大

        MIN_SOURCE_FILES = 1  # 至少改动1个功能文件
        MAX_SOURCE_FILES = 5  # 一次 Feature 不应修改太多功能文件

        MIN_SOURCE_HUNKS = 2  # 只有1个Hunk无法构建排序任务，直接丢弃
        MAX_SOURCE_HUNKS = 5
    REQUIRE_DEPENDENCY = True # 是否必须存在显式的 Def-Use 依赖链
    ALLOW_CYCLES = True      # 是否允许存在依赖环路（通常环路意味着逻辑复杂或提取错误）
    NO_ISOLATED_HUNKS = True

    IGNORE_FILES = ["setup.py", "__init__.py", "conftest.py", "docs/conf.py"]

    # 对于 Benchmark：必须 True
    REQUIRE_TEST_CHANGE = True

    # --- LLM Configuration ---
    LLM_API_KEY: str = os.environ.get("LLM_API_KEY", "")
    LLM_BASE_URL: str = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
    LLM_MODEL: str = os.environ.get("LLM_MODEL", "deepseek-reasoner")
    LLM_MAX_DIFF_LINES: int = int(os.environ.get("LLM_MAX_DIFF_LINES", "5000"))

    USE_LLM = False