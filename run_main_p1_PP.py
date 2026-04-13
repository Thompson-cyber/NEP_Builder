import subprocess

# 定义参数列表，每个参数组为一个字典
params_list = [
    # {
    #     "repo": r"D:\Data\2025\信通院\repos\Python\AutoGPT",
    #     "repo_name": "AutoGPT",
    #     "output": r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase1\single_coarse_autogpt.jsonl",
    #     "limit": 100000
    # },
    {
        "repo": r"D:\Data\2025\CodeCompletion\Dataset\Repos\Python\dify",
        "repo_name": "dify",
        "output": r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase1\dify\single_coarse_dify.jsonl",
        "limit": 100000
    },
    {
        "repo": r"D:\Data\2025\CodeCompletion\Dataset\Repos\Python\langchain",
        "repo_name": "langchain",
        "output": r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase1\langchain\single_coarse_langchain.jsonl",
        "limit": 100000
    },
    {
        "repo": r"D:\Data\2025\CodeCompletion\Dataset\Repos\Python\pathway",
        "repo_name": "pathway",
        "output": r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase1\pathway\single_coarse_pathway.jsonl",
        "limit": 100000
    },
    # {
    #     "repo": r"D:\Data\2025\信通院\repos\Python\scikit-learn",
    #     "repo_name": "scikit-learn",
    #     "output": r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase1\single_coarse_scikit-learn.jsonl",
    #     "limit": 100000
    # },
    # 添加其他参数组
    # {
    #     "repo": r"D:\Data\2025\信通院\repos\TS\openclaw",
    #     "repo_name": "openclaw",
    #     "output": r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase1\single_coarse_openclaw.jsonl",
    #     "limit": 100000
    # },
    # {
    #     "repo": r"D:\Data\2025\信通院\repos\TS\n8n",
    #     "repo_name": "n8n",
    #     "output": r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase1\single_coarse_n8n.jsonl",
    #     "limit": 100000
    # },
    # {
    #     "repo": r"D:\Data\2025\信通院\repos\Java\elasticsearch",
    #     "repo_name": "elasticsearch",
    #     "output": r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase1\single_coarse_elasticsearch.jsonl",
    #     "limit": 100000
    # },
    # {
    #     "repo": r"D:\Data\2025\信通院\repos\Java\spring-boot",
    #     "repo_name": "spring-boot",
    #     "output": r"D:\Data\2025\CodeCompletion\Dataset\Outputs\Phase1\single_coarse_spring-boot.jsonl",
    #     "limit": 100000
    # },
]

# 串行运行每组参数
for params in params_list:
    command = [
        "python",
        r"D:/Data/pythonCodes/nep_builder/main_p1_collect_commits.py",
        "--repo", params["repo"],
        "--repo_name", params["repo_name"],
        "--output", params["output"],
        "--limit", str(params["limit"])
    ]

    # 调用子进程执行命令
    subprocess.run(command, check=True)
