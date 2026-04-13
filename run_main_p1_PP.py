import subprocess

# 定义参数列表，每个参数组为一个字典
params_list = [
    {
        "repo": r"D:\Data\2025\CodeCompletion\Dataset\Repos\Python\transformers",
        "repo_name": "transformers",
        "output": r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase1\single_coarse_transformers.jsonl",
        "limit": 100000
    },
    {
        "repo": r"D:\Data\2025\CodeCompletion\Dataset\Repos\Python\django",
        "repo_name": "django",
        "output": r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase1\single_coarse_django.jsonl",
        "limit": 100000
    },

]

# 串行运行每组参数
for params in params_list:
    command = [
        "python",
        r"D:/Data/pythonCodes/nep_builder/stage1_collect_commits.py",
        "--repo", params["repo"],
        "--repo_name", params["repo_name"],
        "--output", params["output"],
        "--limit", str(params["limit"])
    ]

    # 调用子进程执行命令
    subprocess.run(command, check=True)
