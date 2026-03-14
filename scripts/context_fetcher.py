#!/usr/bin/env python3
"""
全栈变更分析脚本（Java 后端 + React 前端）

核心思路：
  - 后端：标注每个变动文件是否是入口层（Controller/Scheduled/MQ），输出路由路径
  - 前端：标注是否是页面级组件、是否涉及路由变更，输出 diff
  - 支持多仓库分析
  - 不做依赖追溯，让 Claude 直接读代码

用法：
  python3 context_fetcher.py                              # 今天
  python3 context_fetcher.py --since 2025-03-10           # 指定日期
  python3 context_fetcher.py --since 2025-03-01 --until 2025-03-12
  python3 context_fetcher.py --repos "backend:/path/to/backend,frontend:/path/to/frontend"
"""

import subprocess
import os
import re
import argparse
from datetime import datetime

from backend_analyzer import format_java_file, is_entry_file, is_ignored_file as is_be_ignored_file, is_sql_migration_file
from frontend_analyzer import format_frontend_file, is_router_file, is_page_file, is_ignored_file as is_fe_ignored_file

TOPIC_STOP_WORDS = {
    "src", "main", "java", "cn", "com", "org", "pages", "page", "components", "component",
    "service", "services", "impl", "api", "admin", "controller", "repository", "domain",
    "model", "entity", "dto", "vo", "request", "response", "query", "config", "common",
    "dashboard", "index", "utils", "hooks", "store", "context", "private", "public",
    "protected", "static", "application", "resource"
}


def run_cmd(cmd, cwd=None):
    try:
        return subprocess.check_output(
            cmd, shell=True, stderr=subprocess.DEVNULL, cwd=cwd
        ).decode("utf-8").strip()
    except subprocess.CalledProcessError:
        return ""


def classify_change_type(commit_msgs):
    """根据 commit message 启发式判断变更类型"""
    result = {"bugfix": [], "feature": [], "refactor": [], "other": []}

    for line in commit_msgs:
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        commit_hash = parts[0]
        commit_msg = parts[1].lower() if len(parts) > 1 else ""

        if any(kw in commit_msg for kw in ["fix", "bug", "repair", "解决", "修复", "修复了"]):
            result["bugfix"].append((commit_hash, commit_msg))
        elif any(kw in commit_msg for kw in ["feat", "feature", "新增", "新功能", "add", "功能"]):
            result["feature"].append((commit_hash, commit_msg))
        elif any(kw in commit_msg for kw in ["refactor", "重构", "优化", "improve", "性能"]):
            result["refactor"].append((commit_hash, commit_msg))
        else:
            result["other"].append((commit_hash, commit_msg))

    return result


def dedupe_keep_order(items):
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def extract_topic_candidates(file_paths):
    topics = []
    for file_path in file_paths:
        parts = re.split(r"[/_.-]", file_path)
        for part in parts:
            token = part.strip()
            if len(token) < 4:
                continue
            lower = token.lower()
            if lower in TOPIC_STOP_WORDS:
                continue
            if token.isupper() or token.islower():
                if lower.endswith(("controller", "service", "listener", "handler", "config", "repository", "query", "event")):
                    continue
            if lower.endswith(("java", "ts", "tsx", "jsx", "js", "vue", "less", "scss")):
                continue
            topics.append(token)
    return dedupe_keep_order(topics[:12])


MERGE_SKIP_PATTERNS = [
    "autocreatfromdevops",
]


def get_changed_files(since, until, repo_path=None):
    """获取日期范围内合并到主分支的变动文件，按语言分类
    
    Args:
        since: 开始日期
        until: 结束日期
        repo_path: 仓库路径
    """
    merge_commits = run_cmd(
        f'git log --merges --after="{since} 00:00:00" --before="{until} 23:59:59" '
        f'--format="%H %s"',
        cwd=repo_path
    ).split("\n")

    valid_commit_msgs = []
    changed = set()
    
    for line in merge_commits:
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        merge_hash = parts[0]
        commit_msg = parts[1] if len(parts) > 1 else ""
        msg_lower = commit_msg.lower()
        
        # 跳过无实质内容的 merge commit
        if any(pattern in msg_lower for pattern in MERGE_SKIP_PATTERNS):
            continue
        
        if any(kw in msg_lower for kw in ["refactor", "typo", "chore", "ci:", "build:"]):
            continue
        
        valid_commit_msgs.append(line)
        
        files = run_cmd(
            f'git diff --name-only {merge_hash}^..{merge_hash}',
            cwd=repo_path
        ).split("\n")
        
        for f in files:
            f = f.strip()
            if f:
                changed.add(f)

    if not changed:
        return [], [], [], []

    def file_exists(path, base_path):
        if base_path:
            return os.path.exists(os.path.join(base_path, path))
        return os.path.exists(path)

    java_files = sorted([f for f in changed if f.endswith(".java") and file_exists(f, repo_path) and not is_be_ignored_file(f)])
    frontend_files = sorted([
        f for f in changed
        if f.endswith((".ts", ".tsx", ".js", ".jsx", ".vue", ".css", ".scss"))
        and file_exists(f, repo_path)
        and not is_fe_ignored_file(f)
        and "node_modules" not in f
        and not f.endswith((".min.js", ".min.css"))
    ])
    sql_files = sorted([f for f in changed if is_sql_migration_file(f) and file_exists(f, repo_path)])

    return java_files, frontend_files, sql_files, valid_commit_msgs


def get_file_diff(file_path, since, until, repo_path=None):
    """获取文件在时间段内的累计 diff"""
    commits = run_cmd(
        f'git log --after="{since} 00:00:00" --before="{until} 23:59:59" '
        f'--no-merges --format="%H" -- "{file_path}"',
        cwd=repo_path
    ).split("\n")
    commits = [c.strip() for c in commits if c.strip()]

    if not commits:
        return ""
    if len(commits) == 1:
        return run_cmd(f'git show {commits[0]} -- "{file_path}"', cwd=repo_path)

    oldest, newest = commits[-1], commits[0]
    parent = run_cmd(f'git rev-parse {oldest}^', cwd=repo_path)
    if parent:
        return run_cmd(f'git diff {parent} {newest} -- "{file_path}"', cwd=repo_path)
    return run_cmd(f'git show {newest} -- "{file_path}"', cwd=repo_path)


def analyze_repo(repo_name, repo_path, since, until):
    """分析单个仓库，返回变更信息"""
    java_files, frontend_files, sql_files, commit_msgs = get_changed_files(since, until, repo_path)
    
    return {
        "name": repo_name,
        "path": repo_path,
        "java_files": java_files,
        "frontend_files": frontend_files,
        "sql_files": sql_files,
        "commit_msgs": commit_msgs,
        "topics": extract_topic_candidates(java_files + frontend_files + sql_files),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=datetime.today().strftime("%Y-%m-%d"))
    parser.add_argument("--until", default=None)
    parser.add_argument("--repo-path", default=None, help="单个仓库路径")
    parser.add_argument("--repos", default=None, help="多仓库，格式: name:path,name:path")
    args = parser.parse_args()

    since = args.since
    until = args.until or since

    # 解析仓库列表
    repos = []
    if args.repos:
        for item in args.repos.split(","):
            if ":" in item:
                name, path = item.split(":", 1)
                repos.append((name.strip(), path.strip()))
    elif args.repo_path:
        repos = [("default", args.repo_path)]
    else:
        repos = [("default", ".")]

    print(f"# 变更分析报告")
    print(f"# 分析范围：{since} ~ {until}")
    print(f"# 统计模式：按合并到主分支的时间")
    print(f"# 分析仓库数：{len(repos)}")
    
    all_results = []
    total_java = 0
    total_frontend = 0
    total_sql = 0
    all_commit_msgs = []
    
    for repo_name, repo_path in repos:
        result = analyze_repo(repo_name, repo_path, since, until)
        all_results.append(result)
        total_java += len(result["java_files"])
        total_frontend += len(result["frontend_files"])
        total_sql += len(result["sql_files"])
        all_commit_msgs.extend(result["commit_msgs"])
        
        print(f"\n# 仓库: {repo_name} ({repo_path})")
        print(f"#   后端变更：{len(result['java_files'])} 个 Java 文件")
        print(f"#   前端变更：{len(result['frontend_files'])} 个前端文件")
        print(f"#   数据库变更：{len(result['sql_files'])} 个 SQL 文件")
        if result["topics"]:
            print(f"#   主题候选：{', '.join(result['topics'][:8])}")

    print(f"\n# 总计：")
    print(f"#   后端变更：{total_java} 个 Java 文件")
    print(f"#   前端变更：{total_frontend} 个前端文件")
    print(f"#   数据库变更：{total_sql} 个 SQL 文件")

    if total_java == 0 and total_frontend == 0 and total_sql == 0:
        print("\nNO_CHANGES")
        return

    change_types = classify_change_type(all_commit_msgs)
    print(f"\n# 变更类型统计（根据 commit message 推断）：")
    print(f"# 🐛 Bug 修复：{len(change_types['bugfix'])} 个")
    print(f"# ✨ 新功能：{len(change_types['feature'])} 个")
    print(f"# 🔧 技术改造：{len(change_types['refactor'])} 个")
    print(f"# 📝 其他：{len(change_types['other'])} 个")

    for result in all_results:
        repo_path = result["path"]
        
        if result["java_files"]:
            entry_files = []
            other_files = []
            for f in result["java_files"]:
                if is_entry_file(f, repo_path):
                    entry_files.append(f)
                else:
                    other_files.append(f)

            print(f"\n\n{'#'*60}")
            print(f"# 后端变更 [{result['name']}]（入口层 {len(entry_files)} 个，中间层 {len(other_files)} 个）")
            print(f"{'#'*60}")
            if result["topics"]:
                print(f"# 产品主题候选：{', '.join(result['topics'][:10])}")

            for f in entry_files + other_files:
                diff = get_file_diff(f, since, until, repo_path)
                print(format_java_file(f, diff, repo_path))

        if result["sql_files"]:
            print(f"\n\n{'#'*60}")
            print(f"# 数据库变更 [{result['name']}]（SQL 迁移文件 {len(result['sql_files'])} 个）")
            print(f"{'#'*60}")

            for f in result["sql_files"]:
                diff = get_file_diff(f, since, until, repo_path)
                lines = []
                lines.append(f"\n{'='*60}")
                lines.append(f"[数据库] {f}")
                lines.append(f"{'='*60}")
                lines.append(f"\n▶ SQL 迁移文件（数据库表结构/数据变更）")
                lines.append(f"\n[Diff]")
                lines.append(diff if diff else "（无法获取 diff）")
                print("\n".join(lines))

        if result["frontend_files"]:
            router_files = [f for f in result["frontend_files"] if is_router_file(f)]
            page_files = [f for f in result["frontend_files"] if f not in router_files and is_page_file(f)]
            other_fe = [f for f in result["frontend_files"] if f not in router_files and f not in page_files]

            print(f"\n\n{'#'*60}")
            print(f"# 前端变更 [{result['name']}]（路由 {len(router_files)} 个，页面 {len(page_files)} 个，其他 {len(other_fe)} 个）")
            print(f"{'#'*60}")
            if result["topics"]:
                print(f"# 产品主题候选：{', '.join(result['topics'][:10])}")

            for f in router_files + page_files + other_fe:
                diff = get_file_diff(f, since, until, repo_path)
                print(format_frontend_file(f, diff))

    print(f"\n\n# ── 报告结束 ──")


if __name__ == "__main__":
    main()
