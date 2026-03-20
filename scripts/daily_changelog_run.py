#!/usr/bin/env python3

import argparse
import os
import sys

from changelog_pipeline import ChangelogPipelineError, run_pipeline
from context_fetcher import resolve_repo_inputs, resolve_since_value


def build_args_namespace(args):
    return argparse.Namespace(
        repos=args.repos,
        repo_path=args.repo_path,
        repo_discovery_depth=args.repo_discovery_depth,
    )


def main():
    parser = argparse.ArgumentParser(description="生成中文产品更新日志。")
    parser.add_argument("--since", default=None, help="开始日期；支持 YYYY-MM-DD、earliest、auto")
    parser.add_argument("--until", default=None, help="结束日期；默认与 --since 相同")
    parser.add_argument("--repo-path", default=None, help="单个仓库路径，或包含多个子仓库的项目目录")
    parser.add_argument("--repos", default=None, help="多仓库，格式: name:path,name:path")
    parser.add_argument("--repo-discovery-depth", type=int, default=None, help="自动发现子仓库的最大目录深度；默认不限制")
    parser.add_argument("--order", choices=["asc", "desc"], default="desc", help="日期顺序")
    parser.add_argument("--output", required=True, help="最终 changelog 输出文件路径")
    args = parser.parse_args()

    repos, notices, errors = resolve_repo_inputs(build_args_namespace(args))
    if errors:
        print("DAILY_CHANGELOG_RUN_ERROR")
        for error in errors:
            print(f"- {error}")
        sys.exit(2)

    since, date_notices = resolve_since_value(args.since, repos)
    notices.extend(date_notices)
    until = args.until or since

    try:
        final_path, _pipeline_workdir = run_pipeline(
            repos,
            notices,
            since,
            until,
            args.order,
            args.output,
        )
    except ChangelogPipelineError as exc:
        print("DAILY_CHANGELOG_RUN_ERROR")
        for line in str(exc).splitlines():
            print(f"- {line}")
        sys.exit(1)

    print("DAILY_CHANGELOG_RUN_OK")
    print(os.path.abspath(final_path))


if __name__ == "__main__":
    main()
