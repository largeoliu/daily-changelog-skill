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
  python3 context_fetcher.py --repo-path /path/to/project-root
  python3 context_fetcher.py --repos "backend:/path/to/backend,frontend:/path/to/frontend"

说明：
  - --repo-path 可以传单个 git 仓库，也可以传包含多个子仓库的项目目录
  - 当输入路径本身不是 git 仓库时，脚本会自动发现子目录中的 git 仓库
"""

import subprocess
import os
import re
import argparse
import shlex
import sys
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

DISCOVERY_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "target",
    "coverage",
}


def run_cmd(cmd, cwd=None):
    try:
        return subprocess.check_output(
            cmd, shell=True, stderr=subprocess.DEVNULL, cwd=cwd
        ).decode("utf-8").strip()
    except subprocess.CalledProcessError:
        return ""


def resolve_git_root(path):
    """返回路径所在 git 仓库的根目录；若不是 git 仓库则返回空字符串。"""
    if not path:
        return ""
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return ""
    quoted = shlex.quote(abs_path)
    root = run_cmd(f"git -C {quoted} rev-parse --show-toplevel")
    if not root:
        return ""
    return os.path.abspath(root)


def sanitize_repo_name(name, used_names):
    base = re.sub(r"[^A-Za-z0-9]+", "-", (name or "repo").strip()).strip("-").lower() or "repo"
    candidate = base
    index = 2
    while candidate in used_names:
        candidate = f"{base}-{index}"
        index += 1
    used_names.add(candidate)
    return candidate


def discover_git_repos(path, max_depth=2):
    """从给定路径自动发现 git 仓库。

    优先将输入路径本身识别为仓库；若不是仓库，则递归扫描子目录。
    """
    target = os.path.abspath(path)
    if not os.path.exists(target):
        return [], "missing"

    direct_root = resolve_git_root(target)
    if direct_root:
        return [(os.path.basename(direct_root), direct_root)], "direct"

    discovered = []
    seen_paths = set()
    for current_root, dirnames, _ in os.walk(target):
        rel_path = os.path.relpath(current_root, target)
        depth = 0 if rel_path == "." else rel_path.count(os.sep) + 1

        dirnames[:] = [
            d for d in dirnames
            if d not in DISCOVERY_SKIP_DIRS and not d.startswith(".")
        ]

        if depth > max_depth:
            dirnames[:] = []
            continue

        if current_root == target:
            continue

        repo_root = resolve_git_root(current_root)
        if not repo_root:
            continue

        repo_root = os.path.abspath(repo_root)
        try:
            if os.path.commonpath([target, repo_root]) != target:
                continue
        except ValueError:
            continue

        if repo_root in seen_paths:
            dirnames[:] = []
            continue

        seen_paths.add(repo_root)
        discovered.append((os.path.basename(repo_root), repo_root))
        dirnames[:] = []

    return sorted(discovered, key=lambda item: item[1]), "discovered"


def expand_repo_target(name, path, used_names):
    """把用户提供的路径展开成一个或多个 git 仓库。"""
    repo_name = name.strip() if name else ""
    repos, mode = discover_git_repos(path)
    if not repos:
        return [], mode, f"未在路径下发现 Git 仓库：{os.path.abspath(path)}"

    expanded = []
    if len(repos) == 1:
        final_name = sanitize_repo_name(repo_name or repos[0][0], used_names)
        expanded.append((final_name, repos[0][1]))
        return expanded, mode, ""

    for discovered_name, repo_path in repos:
        base_name = f"{repo_name}-{discovered_name}" if repo_name else discovered_name
        expanded.append((sanitize_repo_name(base_name, used_names), repo_path))
    return expanded, mode, ""


def resolve_repo_inputs(args):
    """解析命令行中的仓库输入，支持单仓库、项目目录和显式多仓库。"""
    notices = []
    errors = []
    repos = []
    used_names = set()

    if args.repos:
        for item in args.repos.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" not in item:
                errors.append(f"仓库参数格式错误：{item}")
                continue
            raw_name, raw_path = item.split(":", 1)
            expanded, mode, error = expand_repo_target(raw_name, raw_path.strip(), used_names)
            if error:
                errors.append(error)
                continue
            repos.extend(expanded)
            if mode == "discovered":
                notices.append(
                    f"# 仓库发现：{os.path.abspath(raw_path.strip())} 不是 Git 仓库，已自动识别 {len(expanded)} 个子仓库"
                )
    else:
        raw_path = args.repo_path or "."
        expanded, mode, error = expand_repo_target("", raw_path, used_names)
        if error:
            errors.append(error)
        else:
            repos.extend(expanded)
            if mode == "discovered":
                notices.append(
                    f"# 仓库发现：{os.path.abspath(raw_path)} 不是 Git 仓库，已自动识别 {len(expanded)} 个子仓库"
                )

    deduped = []
    seen_paths = set()
    for repo_name, repo_path in repos:
        if repo_path in seen_paths:
            continue
        seen_paths.add(repo_path)
        deduped.append((repo_name, repo_path))

    return deduped, notices, errors


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
    parser.add_argument("--repo-path", default=None, help="单个仓库路径，或包含多个子仓库的项目目录")
    parser.add_argument("--repos", default=None, help="多仓库，格式: name:path,name:path")
    args = parser.parse_args()

    since = args.since
    until = args.until or since

    repos, _notices, errors = resolve_repo_inputs(args)

    if errors:
        print("REPO_DISCOVERY_ERROR")
        for message in errors:
            print(f"# {message}")
        sys.exit(2)

    all_results = []
    total_java = 0
    total_frontend = 0
    total_sql = 0
    
    for repo_name, repo_path in repos:
        result = analyze_repo(repo_name, repo_path, since, until)
        all_results.append(result)
        total_java += len(result["java_files"])
        total_frontend += len(result["frontend_files"])
        total_sql += len(result["sql_files"])
        
    if total_java == 0 and total_frontend == 0 and total_sql == 0:
        print("NO_CHANGES")
        return

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

if __name__ == "__main__":
    main()
