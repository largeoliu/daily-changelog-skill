#!/usr/bin/env python3
"""
全栈变更分析脚本（Java 后端 + React 前端）

核心思路：
  - 后端：标注每个变动文件是否是入口层（Controller/Scheduled/MQ），输出路由路径
  - 前端：标注是否是页面级组件、是否涉及路由变更，输出 diff
  - 支持多仓库分析
  - 按主线 first-parent 历史覆盖 merge、squash merge、rebase merge 和直接提交
  - 不做依赖追溯，让 agent 直接读取代码证据

用法：
  python3 context_fetcher.py                              # 今天
  python3 context_fetcher.py --since 2025-03-10           # 指定日期
  python3 context_fetcher.py --since 2025-03-01 --until 2025-03-12
  python3 context_fetcher.py --since earliest             # 从仓库最早提交日期开始
  python3 context_fetcher.py --repo-path /path/to/project-root --compact
  python3 context_fetcher.py --repos "backend:/path/to/backend,frontend:/path/to/frontend"

说明：
  - --repo-path 可以传单个 git 仓库，也可以传包含多个子仓库的项目目录
  - 当输入路径本身不是 git 仓库时，脚本会自动发现子目录中的 git 仓库
  - --compact 会输出关键证据片段，减少大段 raw diff
"""

import subprocess
import os
import re
import json
import argparse
import shlex
import sys
from collections import defaultdict
from datetime import datetime

from backend_analyzer import format_java_file, inspect_backend_file, is_entry_file, is_ignored_file as is_be_ignored_file, is_sql_migration_file
from diff_evidence import build_compact_evidence, is_changed_diff_line
from frontend_analyzer import classify_frontend_file, format_frontend_file, get_page_theme_root, inspect_frontend_file, is_router_file, is_page_file, is_ignored_file as is_fe_ignored_file

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


def discover_git_repos(path, max_depth=None):
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

        if max_depth is not None and depth > max_depth:
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


def expand_repo_target(name, path, used_names, max_depth=None):
    """把用户提供的路径展开成一个或多个 git 仓库。"""
    repo_name = name.strip() if name else ""
    repos, mode = discover_git_repos(path, max_depth=max_depth)
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
            expanded, mode, error = expand_repo_target(
                raw_name,
                raw_path.strip(),
                used_names,
                max_depth=args.repo_discovery_depth,
            )
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
        expanded, mode, error = expand_repo_target(
            "",
            raw_path,
            used_names,
            max_depth=args.repo_discovery_depth,
        )
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


def git_ref_exists(ref_name, repo_path=None):
    if not ref_name:
        return False
    quoted = shlex.quote(f"{ref_name}^{{commit}}")
    return bool(run_cmd(f"git rev-parse --verify --quiet {quoted}", cwd=repo_path))


def resolve_main_ref(repo_path=None):
    remote_head = run_cmd("git symbolic-ref --quiet refs/remotes/origin/HEAD", cwd=repo_path)
    if remote_head.startswith("refs/remotes/"):
        candidate = remote_head[len("refs/remotes/"):]
        if git_ref_exists(candidate, repo_path):
            return candidate

    for candidate in ["origin/main", "origin/master", "main", "master", "trunk"]:
        if git_ref_exists(candidate, repo_path):
            return candidate

    return "HEAD"


def get_earliest_commit_date(repo_path=None, ref_name=None):
    ref_name = ref_name or resolve_main_ref(repo_path)
    quoted_ref = shlex.quote(ref_name)
    date_value = run_cmd(
        f'git log {quoted_ref} --first-parent --reverse --format="%cs"',
        cwd=repo_path,
    ).split("\n")
    for item in date_value:
        item = item.strip()
        if item:
            return item
    return ""


def resolve_since_value(raw_since, repos):
    normalized = (raw_since or "").strip().lower()
    if normalized not in {"", "auto", "earliest", "launch"}:
        return raw_since, []

    notices = []
    candidates = []
    for repo_name, repo_path in repos:
        main_ref = resolve_main_ref(repo_path)
        earliest = get_earliest_commit_date(repo_path, main_ref)
        if earliest:
            candidates.append(earliest)
            notices.append(f"# 起始日期自动解析：[{repo_name}] 使用 {main_ref} 的最早提交日期 {earliest}")

    if not candidates:
        fallback = datetime.today().strftime("%Y-%m-%d")
        notices.append(f"# 起始日期自动解析失败：回退到今天 {fallback}")
        return fallback, notices

    return min(candidates), notices


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


HIGH_RISK_TERMS_HINT = (
    "# 使用提醒：下面的技术证据只能用于理解变更，不能直接抄写进最终日志；"
    "请优先改写成页面、模块、角色、场景等产品语言。"
)

FRONTEND_THEME_HINT = (
    "# 主题归并提醒：同一路由或页面主题下的子组件、图表、筛选器、弹窗、地图控件、时间轴、数据层文件，"
    "默认都应并入同一条主功能，不要按文件逐条拆分。"
)

BACKEND_THEME_HINT = (
    "# 主题归并提醒：只有 Controller、Dubbo、定时任务、导出/下载入口等独立入口或流程可单列；"
    "DTO/VO/Query/Repository/Mapper/Service 默认视为支撑证据，不要单独写成功能。"
)

ASCII_THEME_STOP_WORDS = {
    "api", "app", "city", "common", "component", "components", "config", "controller", "dashboard",
    "data", "department", "employee", "employees", "frontend", "index", "list", "main",
    "manage", "model", "module", "page", "pages", "query", "record", "repo", "report", "request",
    "response", "routes", "screen", "screens", "service", "statistic", "statistics", "track", "trajectory",
    "view", "work", "works",
}

GENERIC_ASCII_TITLE_WORDS = {
    "check", "config", "dashboard", "detail", "index", "info", "list", "message", "module",
    "new", "page", "place", "process", "screen", "version", "view", "welcome",
}

CJK_THEME_STOP_WORDS = {
    "当前", "信息", "字段", "接口", "模块", "页面", "功能", "系统", "能力", "数据", "处理", "支持", "管理",
    "记录", "规则", "条件", "查询", "列表", "详情", "类型", "结果", "错误", "异常", "格式", "机制",
}

ANCHOR_HINT_WORDS = {
    "页面", "列表", "地图", "看板", "模块", "轨迹", "筛选", "详情", "时间线", "视图",
    "卡片", "图例", "图标", "表格", "周视图", "日视图", "配置", "管理", "入口", "中心",
}

STRONG_PRODUCT_TERMS = {
    "轨迹", "看板", "分析", "统计", "筛选", "地图", "图例", "图标", "时间线", "列表", "详情", "视图",
    "配置", "管理", "入口", "中心", "流程", "台账",
}

GENERIC_BACKEND_LABEL_PATTERNS = [
    r"当前登录.*信息",
    r"不能为空",
    r"失败",
    r"异常",
    r"错误响应",
    r"校验",
]

ABSTRACT_ENTRY_PATTERNS = [
    r"统一.*异常处理",
    r"错误响应格式",
    r"更规范",
    r"精准锁定",
    r"选择体验",
]

NOISE_TITLE_PATTERNS = [
    r"^[a-z]{2}-[A-Z]{2}$",
    r"^rgba\(",
    r"^hsl\(",
    r"^calc\(",
    r"^var\(",
    r"^dayjs/",
    r"^fetch[A-Za-z]",
    r"^get[A-Za-z]",
    r"^query[A-Za-z]",
    r"^load[A-Za-z]",
    r"^[A-Z0-9_:-]{3,}$",
    r"^[a-z][A-Za-z0-9-]+(?:\s+[a-z][A-Za-z0-9-]+)+$",
]

LOW_QUALITY_TITLE_PATTERNS = [
    r"^(请输入|请选择|请至少|请使用|暂无|暂未|缺少).+",
    r"^请先.+",
    r"^点击下方.+",
    r"^在.+中.+",
    r"^是否.+[？?]$",
    r"^加载.+",
    r"^已发起.+",
    r"^已复制.+",
    r"^周[一二三四五六日天]$",
    r"^加载.+失败[:：]?$",
    r"^获取.+失败[:：]?.*",
    r"^登录异常.*",
    r"^查询.+不能为空$",
    r"^文件类型错误.+",
    r"^是否通过该员工的信息申报\?$",
    r"^.+前后不能包含空格$",
    r"^YYYY年MM月DD日(?: HH:mm(?::ss)?)?$",
    r"^解析 .+失败[:：]?$",
    r"^未加好友.+",
    r"^1分钟内不能再次签退哦$",
    r"^查询数据失败$",
    r"^欢迎来到.+",
    r"^.+(?:还|还|未)没有.+",
    r"^.+(?:还|未)未配置.+",
    r"^该.+还?没有?.+",
    r"^请确认.+后再提交",
    r"^请.+后再.+",
    r"^.+(?:暂时|暂)不.+",
    r"^.+(?:请|请先)联系.+",
    r"^.+请在.+分钟内.+",
    r"^.+(?:不支持|不支持).+类型",
    r"^导入失败.+",
    r"^.+(?:必须|需要).+小于.+(?:MB|GB)",
    r"^二维码失效$",
    r"^固定时间名称$",
    r"^选择加密类型$",
    r"^文件大小必须.+",
    r"^.+(?:成功|失败|错误|异常).+(?:已|已更|已更新|已生效)$",
    r"^.+(?:菜单|导航|按钮)$",
    r"^查看.+菜单$",
    r"^.+(?:设置|配置)菜单$",
    r"^导入成功.+",
    r"^目标导入成功$",
    r"^.+(?:已|已经)更新$",
    r"^导入.+失败$",
    r"^.+(?:导入|上报|提交).+失败$",
    r"^.+(?:失败|错误|异常)[a-zA-Z]*$",
    r"^.+不能为空$",
    r"^.+未初始化$",
    r"^.+初始化$",
    r"^.+时间:$",
    r"^.+时间：$",
    r"^新增.+新增$",
    r"^.{30,}$",
]

GENERIC_FILTER_LABELS = {
    "全部", "确定", "重置", "请选择", "上一周", "下一周", "返回整周", "关闭提示",
}

MERGE_LOG_SEPARATOR = "\x1f"
DIFF_HEADER_RE = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$')


def parse_mainline_commits(since, until, repo_path=None):
    """获取主线在日期范围内的提交列表。

    使用 first-parent 历史同时覆盖 merge、squash merge、rebase merge 和直接提交。
    """
    main_ref = resolve_main_ref(repo_path)
    quoted_ref = shlex.quote(main_ref)
    raw_lines = run_cmd(
        f'git log {quoted_ref} --first-parent --after="{since} 00:00:00" --before="{until} 23:59:59" '
        f'--format="%H{MERGE_LOG_SEPARATOR}%cs{MERGE_LOG_SEPARATOR}%s{MERGE_LOG_SEPARATOR}%P"',
        cwd=repo_path,
    ).split("\n")

    commits = []
    for line in raw_lines:
        line = line.strip()
        if not line or MERGE_LOG_SEPARATOR not in line:
            continue

        parts = line.split(MERGE_LOG_SEPARATOR)
        if len(parts) == 3:
            parts.append("")
        if len(parts) < 4:
            continue

        commit_hash, commit_date, commit_msg, parent_text = parts[0], parts[1], parts[2], parts[3]
        parents = [item for item in parent_text.split() if item]
        commits.append(
            {
                "hash": commit_hash,
                "date": commit_date,
                "subject": commit_msg,
                "parents": parents,
                "is_merge": len(parents) > 1,
            }
        )

    return commits, main_ref


def normalize_git_diff_path(path):
    path = path.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    return path.replace('\\/', '/')


def split_diff_by_file(diff_text):
    """将一次 git diff 的输出拆分为按文件聚合的 patch。"""
    file_diffs = {}
    current_path = None
    current_lines = []

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current_path and current_lines:
                file_diffs[current_path] = "\n".join(current_lines)

            current_path = None
            current_lines = [line]

            match = DIFF_HEADER_RE.match(line)
            if match:
                old_path = normalize_git_diff_path(match.group(1))
                new_path = normalize_git_diff_path(match.group(2))
                current_path = old_path if new_path == "/dev/null" else new_path
            continue

        if current_path is not None:
            current_lines.append(line)

    if current_path and current_lines:
        file_diffs[current_path] = "\n".join(current_lines)

    return file_diffs


def collect_repo_file_diffs(since, until, repo_path=None):
    """按主线提交收集仓库中各文件的 diff。"""
    mainline_commits, main_ref = parse_mainline_commits(since, until, repo_path)
    diff_chunks = defaultdict(list)
    file_meta = {}

    for commit in mainline_commits:
        commit_hash = commit["hash"]
        parents = commit.get("parents") or []
        first_parent = parents[0] if parents else ""

        if first_parent:
            diff_text = run_cmd(
                f'git diff --find-renames {first_parent} {commit_hash} --',
                cwd=repo_path,
            )
        else:
            diff_text = run_cmd(
                f'git show --format= --find-renames --root {commit_hash}',
                cwd=repo_path,
            )

        if not diff_text:
            continue

        for file_path, patch in split_diff_by_file(diff_text).items():
            if not patch:
                continue
            diff_chunks[file_path].append(patch)
            file_meta[file_path] = {
                "fallback_ref": first_parent,
                "last_commit": commit_hash,
                "exists_in_worktree": file_exists(file_path, repo_path),
            }

    commit_msgs = [f"{item['hash']} {item['subject']}" for item in mainline_commits]
    return {
        file_path: "\n\n".join(chunks)
        for file_path, chunks in diff_chunks.items()
    }, mainline_commits, file_meta, main_ref, commit_msgs


def collect_repo_file_diffs_by_day(since, until, repo_path=None):
    """按主线提交收集仓库中各日期的 diff。"""
    mainline_commits, main_ref = parse_mainline_commits(since, until, repo_path)
    grouped = {}

    for commit in mainline_commits:
        commit_hash = commit["hash"]
        parents = commit.get("parents") or []
        first_parent = parents[0] if parents else ""

        commit_date = commit.get("date") or ""
        if not commit_date:
            continue

        if first_parent:
            diff_text = run_cmd(
                f'git diff --find-renames {first_parent} {commit_hash} --',
                cwd=repo_path,
            )
        else:
            diff_text = run_cmd(
                f'git show --format= --find-renames --root {commit_hash}',
                cwd=repo_path,
            )

        if not diff_text:
            continue

        bucket = grouped.setdefault(
            commit_date,
            {
                "diff_chunks": defaultdict(list),
                "file_commits": defaultdict(list),
                "file_meta": {},
                "commit_msgs": [],
                "commit_count": 0,
            },
        )
        bucket["commit_msgs"].append(f"{commit_hash} {commit['subject']}")
        bucket["commit_count"] += 1

        for file_path, patch in split_diff_by_file(diff_text).items():
            if not patch:
                continue
            bucket["diff_chunks"][file_path].append(patch)
            bucket["file_commits"][file_path].append(f"{commit_hash} {commit['subject']}")
            bucket["file_meta"][file_path] = {
                "fallback_ref": first_parent,
                "last_commit": commit_hash,
                "exists_in_worktree": file_exists(file_path, repo_path),
            }

    day_results = {}
    for commit_date, payload in grouped.items():
        file_diffs = {
            file_path: "\n\n".join(chunks)
            for file_path, chunks in payload["diff_chunks"].items()
        }
        java_files, frontend_files, sql_files = classify_changed_files(file_diffs, repo_path)
        day_results[commit_date] = {
            "file_diffs": file_diffs,
            "file_commits": {
                file_path: dedupe_keep_order(messages)
                for file_path, messages in payload["file_commits"].items()
            },
            "file_meta": payload["file_meta"],
            "main_ref": main_ref,
            "commit_msgs": payload["commit_msgs"],
            "commit_count": payload["commit_count"],
            "java_files": java_files,
            "frontend_files": frontend_files,
            "sql_files": sql_files,
            "topics": extract_topic_candidates(java_files + frontend_files + sql_files),
        }

    return day_results, main_ref


def file_exists(path, base_path):
    if base_path:
        return os.path.exists(os.path.join(base_path, path))
    return os.path.exists(path)


def reconstruct_text_from_diff(diff_text):
    if not diff_text:
        return ""

    lines = []
    for line in diff_text.splitlines():
        if line.startswith(("diff --git ", "index ", "@@ ", "--- ", "+++ ")):
            continue
        if line.startswith(("+", "-", " ")):
            lines.append(line[1:])
    return "\n".join(lines)


def classify_changed_files(file_diffs, repo_path=None):
    changed = sorted(file_diffs.keys())
    java_files = sorted([f for f in changed if f.endswith(".java") and not is_be_ignored_file(f)])
    frontend_files = sorted([
        f for f in changed
        if f.endswith((".ts", ".tsx", ".js", ".jsx", ".vue", ".css", ".scss"))
        and not is_fe_ignored_file(f)
        and "node_modules" not in f
        and not f.endswith((".min.js", ".min.css"))
    ])
    sql_files = sorted([f for f in changed if is_sql_migration_file(f)])
    return java_files, frontend_files, sql_files


def read_repo_text_file(file_path, repo_path=None, cache=None, fallback_ref=None, diff_text=None):
    cache = cache if cache is not None else {}
    cache_key = (file_path, fallback_ref or "")
    if cache_key in cache:
        return cache[cache_key]

    full_path = os.path.join(repo_path, file_path) if repo_path else file_path
    if os.path.exists(full_path):
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    elif fallback_ref:
        spec = shlex.quote(f"{fallback_ref}:{file_path}")
        content = run_cmd(f"git show {spec}", cwd=repo_path)
    else:
        content = ""

    if not content and diff_text:
        content = reconstruct_text_from_diff(diff_text)

    cache[cache_key] = content
    return content


def split_ascii_tokens(value):
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value or "")
    return [part.lower() for part in re.split(r"[^A-Za-z0-9\u4e00-\u9fff]+", text) if part]


def normalize_path(value):
    return (value or "").replace("\\", "/")


def canonical_theme_key(value):
    return "/".join(split_ascii_tokens(value))


def humanize_theme_name(value):
    if not value:
        return ""
    tail = value.split("/")[-1]
    if re.search(r"[\u4e00-\u9fff]", tail):
        return tail
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tail)
    text = text.replace("-", " ").replace("_", " ").strip()
    return re.sub(r"\s+", " ", text).title()


def contains_cjk(text):
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def has_meaningful_cjk_title(text):
    value = str(text or "").strip()
    if not value or not contains_cjk(value):
        return False
    stripped = value
    generic_terms = sorted(set(CJK_THEME_STOP_WORDS) | set(ANCHOR_HINT_WORDS) | {"接口"}, key=len, reverse=True)
    for term in generic_terms:
        stripped = stripped.replace(term, " ")
    return bool(re.search(r"[\u4e00-\u9fff]{2,}", stripped))


def extract_cjk_terms(text):
    terms = []
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,20}", text or ""):
        if chunk not in CJK_THEME_STOP_WORDS:
            terms.append(chunk)
        for size in (3,):
            if len(chunk) < size:
                continue
            for index in range(len(chunk) - size + 1):
                token = chunk[index:index + size]
                if token not in CJK_THEME_STOP_WORDS:
                    terms.append(token)
    return dedupe_keep_order(terms)


def extract_merge_terms(values):
    terms = []
    for value in values or []:
        text = normalize_path(str(value or ""))
        if not text or is_low_quality_title(text):
            continue
        for token in re.split(r"[^A-Za-z0-9\u4e00-\u9fff]+", text):
            stripped = token.strip()
            if not stripped:
                continue
            if contains_cjk(stripped):
                terms.extend(extract_cjk_terms(stripped))
                continue
            lowered = stripped.lower()
            if len(lowered) < 4 or lowered in ASCII_THEME_STOP_WORDS:
                continue
            terms.append(lowered)
    return dedupe_keep_order(terms)


def has_strong_product_term(values):
    joined = " ".join([str(value or "") for value in values or []])
    return any(term in joined for term in STRONG_PRODUCT_TERMS)


def is_low_quality_title(value):
    text = str(value or "").strip()
    if not text:
        return True
    if any(re.search(pattern, text) for pattern in LOW_QUALITY_TITLE_PATTERNS):
        return True
    if not contains_cjk(text):
        normalized = re.sub(r"\s+", " ", text.lower()).strip()
        parts = split_ascii_tokens(normalized)
        generic_words = ASCII_THEME_STOP_WORDS | GENERIC_ASCII_TITLE_WORDS
        if parts and len(parts) <= 2 and all(part in generic_words for part in parts):
            return True
    return False


def is_noise_title(value):
    text = str(value or "").strip()
    if not text:
        return True
    if text in GENERIC_FILTER_LABELS:
        return True
    if any(re.search(pattern, text) for pattern in NOISE_TITLE_PATTERNS):
        return True
    if re.search(r"[{}$]", text):
        return True
    if text.count("/") >= 2 and not text.startswith("/"):
        return True
    if not contains_cjk(text) and re.search(r"\b(class|plugin|icon|container|wrapper|layout|panel|subtitle|muted|glass)\b", text, re.IGNORECASE):
        return True
    return False


def cleaned_title_candidates(values):
    result = []
    for value in dedupe_keep_order(values or []):
        text = str(value or "").strip()
        if not text or is_noise_title(text) or is_low_quality_title(text):
            continue
        result.append(text)
    return result


def is_generic_ascii_title(value):
    text = str(value or "").strip()
    if not text or contains_cjk(text):
        return False
    parts = split_ascii_tokens(text)
    if not parts:
        return False
    generic_words = ASCII_THEME_STOP_WORDS | GENERIC_ASCII_TITLE_WORDS
    return all(part in generic_words for part in parts)


def is_generic_theme_title(value):
    text = str(value or "").strip()
    if not text:
        return True
    if contains_cjk(text):
        return False
    return is_generic_ascii_title(text)


def score_title_candidate(value):
    text = str(value or "").strip()
    if not text or is_noise_title(text):
        return -1000
    if is_low_quality_title(text):
        return -500
    meaningful = contains_cjk(text) and has_meaningful_cjk_title(text)
    if not meaningful:
        return -300
    if contains_cjk(text):
        if not has_meaningful_cjk_title(text):
            return -300
        score = 100
        if has_strong_product_term([text]):
            score += 20
        if any(hint in text for hint in ANCHOR_HINT_WORDS):
            score += 20
        if len(text) >= 4:
            score += min(len(text), 8)
        if len(text) <= 2:
            score -= 25
        if text in CJK_THEME_STOP_WORDS:
            score -= 25
        return score

    score = 10
    if is_generic_ascii_title(text):
        score -= 20
    else:
        score += 5
    if "/" in text:
        score -= 2
    if len(split_ascii_tokens(text)) >= 2:
        score += 1
    return score


def pick_theme_title(candidates, fallback=""):
    cleaned = cleaned_title_candidates(candidates)
    if cleaned:
        ranked = sorted(
            cleaned,
            key=lambda item: (score_title_candidate(item), len(str(item or "").strip())),
            reverse=True,
        )
        best = str(ranked[0] or "").strip()
        if best:
            return best
    return fallback


def build_anchor_candidates(values):
    anchors = []
    for value in dedupe_keep_order(values or []):
        text = str(value or "").strip()
        if not text or is_low_quality_title(text):
            continue
        if contains_cjk(text) and any(hint in text for hint in ANCHOR_HINT_WORDS):
            anchors.append(text)
        elif contains_cjk(text) and has_strong_product_term([text]):
            anchors.append(text)
        elif not contains_cjk(text) and "/" in text:
            anchors.append(text)
    return dedupe_keep_order(anchors)


def infer_primary_family(values, launch_signals=False, support_only=False):
    joined = " ".join([str(value or "") for value in values or []])
    if re.search(r"(筛选|过滤|范围|条件|filter|query)", joined, re.IGNORECASE):
        return "query_filter"
    if re.search(r"(轨迹|作业记录|时间线|日视图|周视图|签到|访问)", joined):
        return "trajectory"
    if re.search(r"(看板|统计|分析|指标|获客|效率|转化率|利用率)", joined):
        return "stats_analysis"
    if re.search(r"(详情|展示|图例|图标|布局|地图|字段|列表|视图)", joined):
        return "detail_display"
    if re.search(r"(异常|校验|错误响应|参数校验)", joined):
        return "validation_or_error"
    if launch_signals:
        return "page_launch"
    if support_only:
        return "support_only"
    return "feature_flow"


def route_strings(values):
    return [normalize_path(str(value or "")).lower() for value in values or [] if value]


def plain_strings(values):
    return [str(value or "").strip() for value in values or [] if str(value or "").strip()]


def detect_domain(routes=None, paths=None, labels=None, titles=None):
    route_values = route_strings(routes)
    path_values = route_strings(paths)
    title_values = plain_strings(titles)
    label_values = plain_strings(labels)

    fallback_title = pick_theme_title(title_values + label_values, fallback=(title_values[0] if title_values else ""))
    if fallback_title:
        return {
            "key": canonical_theme_key(fallback_title),
            "title": fallback_title,
        }

    if path_values:
        fallback_path = path_values[0]
        return {
            "key": canonical_theme_key(fallback_path),
            "title": pick_theme_title(title_values + label_values, fallback=humanize_theme_name(fallback_path)),
        }
    return None


def has_query_signals(values):
    joined = " ".join([str(value or "") for value in values or []])
    return bool(re.search(r"(筛选|过滤|范围|条件|filter|query|month|月份|维度|下钻|选择)", joined, re.IGNORECASE))


def has_visual_signals(values):
    joined = " ".join([str(value or "") for value in values or []])
    return bool(re.search(r"(地图|图例|图标|布局|联动|定位|视图|卡片|光晕|map|legend|layout)", joined, re.IGNORECASE))


def has_detail_signals(values):
    joined = " ".join([str(value or "") for value in values or []])
    return bool(re.search(r"(详情|明细|展示|时间线|列表|趋势|字段)", joined, re.IGNORECASE))


def has_bugfix_signals(values):
    joined = " ".join([str(value or "") for value in values or []])
    return bool(re.search(r"(修复|bug|异常|错误|失败|偏移|问题|缺陷|修复|fix)", joined, re.IGNORECASE))


def has_tech_signals(values):
    joined = " ".join([str(value or "") for value in values or []])
    return bool(re.search(r"(优化|重构|性能|效率|提升|改造|技术改进|refactor)", joined, re.IGNORECASE))


def has_menu_signals(values):
    joined = " ".join([str(value or "") for value in values or []])
    return bool(re.search(r"(菜单|导航|侧边栏|sidebar|menu|nav|路由|path)", joined, re.IGNORECASE))


def has_button_signals(values):
    joined = " ".join([str(value or "") for value in values or []])
    return bool(re.search(r"(按钮|button|点击|click|提交|保存|导出|删除|编辑|新增|添加|确认|取消|重置)", joined, re.IGNORECASE))


def infer_feature_slot_from_structure(routes=None, scene_anchor=None, file_path=None):
    routes = routes or []
    scene_anchor = scene_anchor or ""
    file_path = file_path or ""
    joined_routes = " ".join(str(r or "") for r in routes).lower()

    if re.search(r"(list|query|search|filter)", joined_routes):
        return "query_filter"
    if re.search(r"(detail|info|view)", joined_routes):
        return "detail_display"
    if re.search(r"(stat|chart|analysis|board)", joined_routes):
        return "visual_ux"
    if re.search(r"(create|add|new)", joined_routes):
        return "page_launch"
    if re.search(r"(menu|navigation|nav|sidebar)", joined_routes):
        return "menu_launch"
    if routes:
        return "launch_support"
    return "support_only"


def infer_frontend_feature_slots(role_counts=None, has_route=False, has_page_entry=False):
    role_counts = role_counts or {}
    subview_count = role_counts.get("页面子视图", 0)
    support_count = role_counts.get("页面支撑组件", 0)
    data_count = role_counts.get("页面数据层", 0)
    state_count = role_counts.get("页面状态层", 0)
    config_count = role_counts.get("页面配置层", 0)
    tool_count = role_counts.get("页面工具层", 0)
    total = sum(role_counts.values())

    if has_route:
        return ["page_launch"]
    if not has_page_entry:
        return ["support_only"]

    slots = []
    if subview_count > 0 or data_count > 0:
        slots.append("detail_display")
    if support_count > 0 or state_count > 0 or config_count > 0:
        slots.append("query_filter")
    if tool_count > 0 or (support_count > 0 and subview_count == 0):
        slots.append("visual_ux")
    if not slots:
        slots.append("feature_flow")
    return slots


def infer_feature_slot(values, has_route=False, launch_support=False, support_only=False):
    joined = " ".join([str(value or "") for value in values or []])
    if support_only:
        if has_query_signals(values):
            return "query_filter"
        if has_visual_signals(values):
            return "visual_ux"
        if has_detail_signals(values):
            return "detail_display"
        return "support_only"
    if has_bugfix_signals(values):
        return "bugfix"
    if has_tech_signals(values):
        return "tech_improvement"
    if has_button_signals(values):
        return "button_action"
    if has_query_signals(values):
        return "query_filter"
    if has_visual_signals(values):
        return "visual_ux"
    if has_detail_signals(values):
        return "detail_display"
    if has_menu_signals(values):
        return "menu_launch"
    if launch_support:
        return "launch_support"
    if has_route:
        return "page_launch"
    if re.search(r"(统计|分析|看板|绩效)", joined):
        return "feature_flow"
    return "feature_flow"


def candidate_similarity(left, right):
    score = 0
    left_domain = left.get("domain_key") or ""
    right_domain = right.get("domain_key") or ""
    if left_domain and right_domain:
        if left_domain != right_domain:
            return 0
        score += 8

    left_slot = left.get("feature_slot") or left.get("primary_family") or ""
    right_slot = right.get("feature_slot") or right.get("primary_family") or ""
    if left_slot and right_slot:
        if left_slot == right_slot:
            score += 4
        elif {left_slot, right_slot} == {"page_launch", "launch_support"}:
            score += 2

    if left.get("theme_key") and left.get("theme_key") == right.get("theme_key"):
        score += 4

    shared_terms = set(left.get("merge_terms") or []) & set(right.get("merge_terms") or [])
    score += len(shared_terms)

    for left_anchor in left.get("anchor_candidates") or []:
        for right_anchor in right.get("anchor_candidates") or []:
            if len(left_anchor) >= 2 and len(right_anchor) >= 2 and (left_anchor in right_anchor or right_anchor in left_anchor):
                score += 2
                break

    return score


def candidate_source_kind(candidate):
    if candidate.get("evidence_kind"):
        return str(candidate.get("evidence_kind"))
    kinds = candidate.get("evidence_kinds") or []
    if kinds:
        return str(kinds[0])
    return ""


def should_merge_candidate(theme, candidate):
    theme_domain = theme.get("domain_key") or ""
    candidate_domain = candidate.get("domain_key") or ""
    if theme_domain and candidate_domain and theme_domain != candidate_domain:
        return False

    theme_slot = theme.get("feature_slot") or theme.get("primary_family") or ""
    candidate_slot = candidate.get("feature_slot") or candidate.get("primary_family") or ""
    if theme_slot == candidate_slot:
        return True
    if {theme_slot, candidate_slot} == {"page_launch", "launch_support"}:
        return True

    return False


def parse_added_route_entries(diff_text):
    entries = []
    current = None
    for raw_line in (diff_text or "").splitlines():
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue

        line = raw_line[1:].strip()
        if not line:
            continue

        if current is None and line.startswith("{"):
            current = {}

        if current is None:
            continue

        path_match = re.search(r'\bpath\s*[:=]\s*["\']([^"\']+)["\']', line)
        title_match = re.search(r'\btitle\s*[:=]\s*["\']([^"\']+)["\']', line)
        component_match = re.search(r'\bcomponent\s*[:=]\s*["\']([^"\']+)["\']', line)
        name_match = re.search(r'\bname\s*[:=]\s*["\']([^"\']+)["\']', line)

        if path_match:
            current["path"] = path_match.group(1)
        if title_match:
            current["title"] = title_match.group(1)
        if component_match:
            current["component"] = component_match.group(1)
        if name_match:
            current["name"] = name_match.group(1)

        if line.startswith("},") or line == "}" or line.endswith("},"):
            if any(current.get(key) for key in ("path", "title", "component", "name")):
                entries.append(current)
            current = None

    if current and any(current.get(key) for key in ("path", "title", "component", "name")):
        entries.append(current)
    return entries


def route_component_theme_root(component_path):
    path = (component_path or "").replace("\\", "/").lstrip("./")
    parts = [part for part in path.split("/") if part]
    if not parts:
        return ""
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


def route_path_theme_root(route_path):
    parts = [part for part in (route_path or "").strip("/").split("/") if part]
    if not parts:
        return ""
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


def summarize_role_counts(role_counts):
    ordered_roles = [
        "路由入口",
        "页面入口",
        "页面子视图",
        "页面支撑组件",
        "页面数据层",
        "页面状态层",
        "页面工具层",
        "页面配置层",
        "页面类型定义",
    ]
    parts = []
    for role in ordered_roles:
        count = role_counts.get(role, 0)
        if count:
            parts.append(f"{role} {count} 个")
    return "，".join(parts)


def backend_theme_name(inspection):
    changes = inspection.get("changes") or {}
    routes = dedupe_keep_order((changes.get("added_routes") or []) + (changes.get("removed_routes") or []))
    if routes:
        route = routes[0].strip("/")
        parts = [part for part in route.split("/") if part and part not in {"api", "v1", "v2", "v3", "internal"}]
        if parts:
            return " / ".join(parts[:2])
    if inspection.get("scene_anchor"):
        return inspection["scene_anchor"].replace(" 接口", "").replace(" 模块", "")
    return inspection.get("class_name", "")


def build_frontend_theme_summaries(frontend_files, file_diffs):
    groups = {}

    def ensure_group(key):
        return groups.setdefault(
            key,
            {
                "page_root": "",
                "titles": [],
                "paths": [],
                "files": [],
                "role_counts": defaultdict(int),
            },
        )

    for file_path in frontend_files:
        theme_root = get_page_theme_root(file_path)
        if not theme_root:
            continue
        theme_key = canonical_theme_key(theme_root)
        if not theme_key:
            continue
        group = ensure_group(theme_key)
        group["page_root"] = group["page_root"] or theme_root
        group["files"].append(file_path)
        role = classify_frontend_file(file_path)
        group["role_counts"][role] += 1

    for file_path in frontend_files:
        if not is_router_file(file_path):
            continue
        for entry in parse_added_route_entries(file_diffs.get(file_path, "")):
            component_root = route_component_theme_root(entry.get("component", ""))
            route_root = route_path_theme_root(entry.get("path", ""))
            theme_key = canonical_theme_key(component_root or route_root)
            if not theme_key:
                continue
            group = ensure_group(theme_key)
            if component_root and not group["page_root"]:
                group["page_root"] = component_root
            if entry.get("title"):
                group["titles"].append(entry["title"])
            elif entry.get("name"):
                group["titles"].append(entry["name"])
            if entry.get("path"):
                group["paths"].append(entry["path"])
            group["role_counts"]["路由入口"] += 1

    summaries = []
    for theme_key, group in groups.items():
        if not group["role_counts"].get("路由入口") and not group["role_counts"].get("页面入口"):
            continue
        title = dedupe_keep_order(group["titles"])[0] if group["titles"] else humanize_theme_name(group["page_root"])
        summaries.append(
            {
                "title": title or group["page_root"] or theme_key,
                "paths": dedupe_keep_order(group["paths"]),
                "role_summary": summarize_role_counts(group["role_counts"]),
            }
        )

    summaries.sort(key=lambda item: item["title"])
    return summaries


def build_backend_theme_summaries(entry_files, file_diffs, repo_path, java_content_cache, file_meta):
    summaries = []
    for file_path, content in entry_files:
        meta = file_meta.get(file_path, {})
        inspection = inspect_backend_file(
            file_path,
            file_diffs.get(file_path, ""),
            repo_path=repo_path,
            content=content,
            compact=True,
            exists_in_worktree=meta.get("exists_in_worktree", True),
        )
        name = backend_theme_name(inspection)
        entry_info = inspection.get("entry_info") or {}
        routes = dedupe_keep_order(
            ((inspection.get("changes") or {}).get("added_routes") or [])
            + ((inspection.get("changes") or {}).get("removed_routes") or [])
        )
        if not routes:
            continue
        summaries.append(
            {
                "title": name,
                "entry_type": entry_info.get("entry_type", inspection.get("role", "")),
                "routes": routes,
            }
        )
    return summaries


def make_theme_candidate(
    *,
    domain,
    feature_slot,
    evidence_kind,
    user_visible,
    support_only,
    title_candidates,
    labels,
    routes=None,
    paths=None,
    source_refs=None,
    repo_name="",
    repo_path="",
    role_summary="",
    title_source_tier="unknown",
):
    routes = dedupe_keep_order(routes or [])
    paths = dedupe_keep_order(paths or [])
    labels = dedupe_keep_order(labels or [])
    domain_title = (domain or {}).get("title") or pick_theme_title(title_candidates) or "未命名主题"
    display_title = domain_title
    anchors = build_anchor_candidates([domain_title] + title_candidates + routes + paths + labels)
    merge_terms = extract_merge_terms([domain_title] + title_candidates + routes + paths + labels)
    return {
        "theme_key": f"{(domain or {}).get('key') or canonical_theme_key(domain_title)}:{feature_slot}",
        "theme_title": display_title,
        "domain_key": (domain or {}).get("key") or canonical_theme_key(domain_title),
        "domain_title": domain_title,
        "feature_slot": feature_slot,
        "anchor_candidates": anchors,
        "primary_family": feature_slot,
        "evidence_kind": evidence_kind,
        "user_visible": user_visible,
        "support_only": support_only,
        "labels": labels,
        "routes": routes,
        "paths": paths,
        "merge_terms": merge_terms,
        "source_refs": dedupe_keep_order(source_refs or []),
        "repo_name": repo_name,
        "repo_path": repo_path,
        "role_summary": role_summary,
        "title_source_tier": title_source_tier,
    }


def is_generic_backend_candidate(routes=None, file_path=None):
    routes = routes or []
    file_path = file_path or ""
    if not routes:
        return True
    if any(re.search(r"(list|query|search|filter|stat|detail|create|add|menu|nav)", str(r or "").lower()) for r in routes):
        return False
    if re.search(r"(controller|service|dto|vo|bo|dao|repository|config|util|common|exception)", file_path, re.IGNORECASE):
        return False
    return True


def build_backend_theme_candidates(result):
    repo_path = result["path"]
    file_diffs = result.get("file_diffs", {})
    file_meta = result.get("file_meta", {})
    java_content_cache = {}
    candidates = []

    for file_path in result.get("java_files", []):
        meta = file_meta.get(file_path, {})
        content = read_repo_text_file(
            file_path,
            repo_path,
            java_content_cache,
            fallback_ref=meta.get("fallback_ref"),
            diff_text=file_diffs.get(file_path, ""),
        )
        inspection = inspect_backend_file(
            file_path,
            file_diffs.get(file_path, ""),
            repo_path=repo_path,
            content=content,
            compact=True,
            exists_in_worktree=meta.get("exists_in_worktree", True),
        )
        changes = inspection.get("changes") or {}
        routes = dedupe_keep_order((changes.get("added_routes") or []) + (changes.get("removed_routes") or []))
        if not routes:
            continue

        labels = dedupe_keep_order((inspection.get("product_signals") or {}).get("labels") or [])
        raw_title = pick_theme_title(labels, fallback=backend_theme_name(inspection) or inspection.get("scene_anchor") or file_path)
        domain = detect_domain(
            routes=routes,
            paths=[file_path, inspection.get("scene_anchor")],
            labels=labels,
            titles=[raw_title, inspection.get("scene_anchor")],
        )
        support_only = is_generic_backend_candidate(routes=routes, file_path=file_path)
        feature_slot = "support_only" if support_only else "launch_support"
        candidates.append(
            make_theme_candidate(
                domain=domain,
                feature_slot=feature_slot,
                evidence_kind="backend_support" if support_only else "backend_http",
                user_visible=False,
                support_only=True,
                title_candidates=[raw_title, inspection.get("scene_anchor")] + labels,
                labels=labels,
                routes=routes,
                source_refs=[file_path],
                repo_name=result["name"],
                repo_path=repo_path,
                title_source_tier="structural",
            )
        )

    return candidates


def build_frontend_theme_candidates(result):
    file_diffs = result.get("file_diffs", {})
    file_meta = result.get("file_meta", {})
    groups = {}

    def ensure_group(key, page_root=""):
        return groups.setdefault(
            key,
            {
                "page_root": page_root,
                "route_files": [],
                "titles": [],
                "paths": [],
                "labels": [],
                "filters": [],
                "columns": [],
                "files": [],
                "role_counts": defaultdict(int),
                "has_route": False,
                "has_page_entry": False,
            },
        )

    for file_path in result.get("frontend_files", []):
        theme_root = get_page_theme_root(file_path)
        if not theme_root:
            continue
        group_key = canonical_theme_key(theme_root)
        if not group_key:
            continue
        group = ensure_group(group_key, page_root=theme_root)
        inspection = inspect_frontend_file(
            file_path,
            file_diffs.get(file_path, ""),
            compact=True,
            exists_in_worktree=file_meta.get(file_path, {}).get("exists_in_worktree", True),
        )
        group["files"].append(file_path)
        role = inspection["role"]
        group["role_counts"][role] += 1
        if role == "页面入口":
            group["has_page_entry"] = True
        signals = inspection.get("product_signals") or {}
        group["labels"].extend(signals.get("added_labels") or [])
        group["labels"].extend(signals.get("removed_labels") or [])
        group["filters"].extend(signals.get("added_filters") or [])
        group["filters"].extend(signals.get("removed_filters") or [])
        group["columns"].extend(signals.get("added_columns") or [])
        group["columns"].extend(signals.get("removed_columns") or [])

    for file_path in result.get("frontend_files", []):
        if not is_router_file(file_path):
            continue
        for entry in parse_added_route_entries(file_diffs.get(file_path, "")):
            component_root = route_component_theme_root(entry.get("component", ""))
            route_root = route_path_theme_root(entry.get("path", ""))
            group_key = canonical_theme_key(component_root or route_root or entry.get("title") or entry.get("name"))
            if not group_key:
                continue
            group = ensure_group(group_key, page_root=component_root or route_root)
            group["has_route"] = True
            if file_path not in group["route_files"]:
                group["route_files"].append(file_path)
            if entry.get("title"):
                group["titles"].append(entry["title"])
            if entry.get("name"):
                group["titles"].append(entry["name"])
            if entry.get("path"):
                group["paths"].append(entry["path"])
            group["role_counts"]["路由入口"] += 1

    candidates = []
    for _, group in groups.items():
        labels = [l for l in cleaned_title_candidates(group["labels"]) if score_title_candidate(l) >= 100]
        filters = cleaned_title_candidates(group["filters"])
        columns = cleaned_title_candidates(group["columns"])
        page_path_title = humanize_theme_name(group["page_root"])
        titles = cleaned_title_candidates(group["titles"]) + [page_path_title]
        domain = detect_domain(
            routes=group["paths"],
            paths=[group["page_root"]] + group["files"],
            labels=labels + filters + columns,
            titles=titles,
        )
        if not domain:
            continue

        source_refs = dedupe_keep_order((group.get("route_files") or []) + group["files"])
        role_summary = summarize_role_counts(group["role_counts"])

        title_tier = "structural" if (group["has_route"] or group["has_page_entry"]) else "text"
        if group["has_route"]:
            candidates.append(
                make_theme_candidate(
                    domain=domain,
                    feature_slot="page_launch",
                    evidence_kind="frontend_route",
                    user_visible=True,
                    support_only=False,
                    title_candidates=titles + labels,
                    labels=labels,
                    routes=group["paths"],
                    paths=[group["page_root"]],
                    source_refs=source_refs,
                    repo_name=result["name"],
                    repo_path=result["path"],
                    role_summary=role_summary,
                    title_source_tier=title_tier,
                )
            )

        visible_slots = infer_frontend_feature_slots(
            role_counts=group["role_counts"],
            has_route=group["has_route"],
            has_page_entry=group["has_page_entry"],
        )

        if visible_slots:
            for slot in dedupe_keep_order(visible_slots):
                candidates.append(
                    make_theme_candidate(
                        domain=domain,
                        feature_slot=slot,
                        evidence_kind="frontend_route" if group["has_route"] else ("frontend_page" if group["has_page_entry"] else "frontend_support"),
                        user_visible=True,
                        support_only=False,
                        title_candidates=titles + labels + filters + columns,
                        labels=labels + filters + columns,
                        routes=group["paths"],
                        paths=[group["page_root"]],
                        source_refs=source_refs,
                        repo_name=result["name"],
                        repo_path=result["path"],
                        role_summary=role_summary,
                        title_source_tier=title_tier,
                    )
                )
        else:
            candidates.append(
                make_theme_candidate(
                    domain=domain,
                    feature_slot="support_only",
                    evidence_kind="frontend_support",
                    user_visible=False,
                    support_only=True,
                    title_candidates=titles + labels + filters + columns,
                    labels=labels + filters + columns,
                    routes=group["paths"],
                    paths=[group["page_root"]],
                    source_refs=source_refs,
                    repo_name=result["name"],
                    repo_path=result["path"],
                    role_summary=role_summary,
                    title_source_tier="text",
                )
            )

    return candidates


def build_repo_theme_candidates(result):
    return build_backend_theme_candidates(result) + build_frontend_theme_candidates(result)


def build_json_report(all_results, notices, since, until):
    payload = {
        "status": "ok",
        "since": since,
        "until": until,
        "notices": notices,
        "repos": [],
    }

    for result in all_results:
        payload["repos"].append(
            {
                "name": result["name"],
                "path": result["path"],
                "main_ref": result["main_ref"],
                "commit_count": result["commit_count"],
                "topics": result["topics"],
                "theme_candidates": build_repo_theme_candidates(result),
            }
        )

    if not payload["repos"]:
        payload["status"] = "no_changes"
    elif not any(repo["theme_candidates"] for repo in payload["repos"]):
        payload["status"] = "no_changes"

    return payload


def build_sql_evidence_matcher():
    sql_keywords = [
        "create table",
        "alter table",
        "drop table",
        "add column",
        "drop column",
        "modify column",
        "change column",
        "rename column",
        "create index",
        "drop index",
        "constraint",
        "comment",
        "insert into",
        "update ",
        "delete from",
    ]

    def matcher(line):
        if not is_changed_diff_line(line):
            return False
        lowered = line[1:].strip().lower()
        return any(keyword in lowered for keyword in sql_keywords)

    return matcher


def analyze_repo(repo_name, repo_path, since, until):
    """分析单个仓库，返回变更信息"""
    file_diffs, mainline_commits, file_meta, main_ref, commit_msgs = collect_repo_file_diffs(since, until, repo_path)
    java_files, frontend_files, sql_files = classify_changed_files(file_diffs, repo_path)

    return {
        "name": repo_name,
        "path": repo_path,
        "main_ref": main_ref,
        "java_files": java_files,
        "frontend_files": frontend_files,
        "sql_files": sql_files,
        "file_diffs": file_diffs,
        "file_meta": file_meta,
        "commit_msgs": commit_msgs,
        "commit_count": len(mainline_commits),
        "topics": extract_topic_candidates(java_files + frontend_files + sql_files),
    }


def analyze_repo_window(repo_name, repo_path, since, until):
    day_results, main_ref = collect_repo_file_diffs_by_day(since, until, repo_path)
    results = {}
    for day, payload in day_results.items():
        results[day] = {
            "name": repo_name,
            "path": repo_path,
            "main_ref": main_ref,
            "java_files": payload["java_files"],
            "frontend_files": payload["frontend_files"],
            "sql_files": payload["sql_files"],
            "file_diffs": payload["file_diffs"],
            "file_commits": payload.get("file_commits", {}),
            "file_meta": payload["file_meta"],
            "commit_msgs": payload["commit_msgs"],
            "commit_count": payload["commit_count"],
            "topics": payload["topics"],
        }
    return results


def main():
    parser = argparse.ArgumentParser(description="按天提取多仓库代码变更证据，供 agent 生成中文产品更新日志。")
    parser.add_argument("--since", default=datetime.today().strftime("%Y-%m-%d"), help="开始日期；支持 YYYY-MM-DD、earliest、auto")
    parser.add_argument("--until", default=None, help="结束日期；默认与 --since 相同")
    parser.add_argument("--repo-path", default=None, help="单个仓库路径，或包含多个子仓库的项目目录")
    parser.add_argument("--repos", default=None, help="多仓库，格式: name:path,name:path")
    parser.add_argument("--repo-discovery-depth", type=int, default=None, help="自动发现子仓库的最大目录深度；默认不限制")
    parser.add_argument("--compact", action="store_true", help="输出关键证据片段，减少大段 raw diff")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON，包含主题候选和归并线索")
    args = parser.parse_args()

    repos, notices, errors = resolve_repo_inputs(args)

    if errors:
        if args.json:
            print(json.dumps({"status": "repo_discovery_error", "errors": errors}, ensure_ascii=False, indent=2))
            sys.exit(2)
        print("REPO_DISCOVERY_ERROR")
        for message in errors:
            print(f"# {message}")
        sys.exit(2)

    since, date_notices = resolve_since_value(args.since, repos)
    notices.extend(date_notices)
    until = args.until or since

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
        if args.json:
            print(json.dumps(build_json_report([], notices, since, until), ensure_ascii=False, indent=2))
            return
        for notice in notices:
            print(notice)
        print("NO_CHANGES")
        return

    if args.json:
        print(json.dumps(build_json_report(all_results, notices, since, until), ensure_ascii=False, indent=2))
        return

    for notice in notices:
        print(notice)

    for result in all_results:
        repo_path = result["path"]
        file_diffs = result.get("file_diffs", {})
        file_meta = result.get("file_meta", {})
        
        if result["java_files"]:
            entry_files = []
            other_files = []
            java_content_cache = {}
            for f in result["java_files"]:
                meta = file_meta.get(f, {})
                content = read_repo_text_file(
                    f,
                    repo_path,
                    java_content_cache,
                    fallback_ref=meta.get("fallback_ref"),
                    diff_text=file_diffs.get(f, ""),
                )
                if is_entry_file(f, repo_path, content=content):
                    entry_files.append((f, content))
                else:
                    other_files.append((f, content))

            print(f"\n\n{'#'*60}")
            print(f"# 后端变更 [{result['name']}]（主线 {result['main_ref']}，入口层 {len(entry_files)} 个，中间层 {len(other_files)} 个）")
            print(f"{'#'*60}")
            if result["topics"]:
                print(f"# 产品主题候选：{', '.join(result['topics'][:10])}")
            print(BACKEND_THEME_HINT)
            print(HIGH_RISK_TERMS_HINT)

            backend_theme_summaries = build_backend_theme_summaries(entry_files, file_diffs, repo_path, java_content_cache, file_meta)
            if backend_theme_summaries:
                print("# 后端主题归并提示：")
                for summary in backend_theme_summaries:
                    parts = [f"主题「{summary['title']}」"]
                    if summary["routes"]:
                        parts.append(f"入口：{', '.join(summary['routes'][:2])}")
                    if summary["entry_type"]:
                        parts.append(f"入口类型：{summary['entry_type']}")
                    parts.append("同主题下的 Service/DTO/Repository/Mapper 默认并入该主题，不要单列")
                    print(f"# - {'；'.join(parts)}")
            elif other_files:
                print("# 后端主题归并提示：本次仅有中间层或数据层支撑改动，如无独立入口或流程证据，默认不要单列正式产品日志")

            for f, content in entry_files + other_files:
                diff = file_diffs.get(f, "")
                meta = file_meta.get(f, {})
                print(format_java_file(f, diff, repo_path, content=content, compact=args.compact, exists_in_worktree=meta.get("exists_in_worktree", True)))

        if result["sql_files"]:
            print(f"\n\n{'#'*60}")
            print(f"# 数据库变更 [{result['name']}]（主线 {result['main_ref']}，SQL 迁移文件 {len(result['sql_files'])} 个）")
            print(f"{'#'*60}")
            print(HIGH_RISK_TERMS_HINT)

            for f in result["sql_files"]:
                diff = file_diffs.get(f, "")
                lines = []
                lines.append(f"\n{'='*60}")
                lines.append(f"[数据库] {f}")
                lines.append(f"{'='*60}")
                lines.append(f"\n▶ SQL 迁移文件（数据库表结构/数据变更）")
                evidence = diff
                if args.compact and diff:
                    evidence = build_compact_evidence(diff, build_sql_evidence_matcher())
                lines.append(f"\n[{'关键证据' if args.compact else 'Diff'}]")
                lines.append(evidence if evidence else "（无法获取 diff）")
                print("\n".join(lines))

        if result["frontend_files"]:
            router_files = [f for f in result["frontend_files"] if is_router_file(f)]
            page_files = [f for f in result["frontend_files"] if f not in router_files and is_page_file(f)]
            other_fe = [f for f in result["frontend_files"] if f not in router_files and f not in page_files]

            print(f"\n\n{'#'*60}")
            print(f"# 前端变更 [{result['name']}]（主线 {result['main_ref']}，路由 {len(router_files)} 个，页面 {len(page_files)} 个，其他 {len(other_fe)} 个）")
            print(f"{'#'*60}")
            if result["topics"]:
                print(f"# 产品主题候选：{', '.join(result['topics'][:10])}")
            print(FRONTEND_THEME_HINT)
            print(HIGH_RISK_TERMS_HINT)

            theme_summaries = build_frontend_theme_summaries(result["frontend_files"], file_diffs)
            if theme_summaries:
                print("# 前端主题归并提示：")
                for summary in theme_summaries:
                    parts = [f"主题「{summary['title']}」"]
                    if summary["paths"]:
                        parts.append(f"入口：{', '.join(summary['paths'][:2])}")
                    if summary["role_summary"]:
                        parts.append(f"关联文件：{summary['role_summary']}")
                    parts.append("如需保留该主题下的小功能，也应并入同一条主功能中描述，不要按子组件或支撑文件拆条")
                    print(f"# - {'；'.join(parts)}")

            for f in router_files + page_files + other_fe:
                diff = file_diffs.get(f, "")
                meta = file_meta.get(f, {})
                print(format_frontend_file(f, diff, compact=args.compact, exists_in_worktree=meta.get("exists_in_worktree", True)))

if __name__ == "__main__":
    main()
