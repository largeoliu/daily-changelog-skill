#!/usr/bin/env python3

import argparse
import json
import os
import shutil
import sys
import tempfile

from changelog_assemble import ChangelogAssembleError, assemble_blocks
from changelog_draft import build_ledger_payload, build_repo_fingerprint, render_markdown
from changelog_generate import render_changelog
from changelog_guard import validate_file
from changelog_semantic_guard import validate_semantics
from context_fetcher import resolve_repo_inputs, resolve_since_value


MANIFEST_FILENAME = "pipeline-manifest.json"
LEDGER_FILENAME = "theme-ledger.json"
THEMES_FILENAME = "themes.md"
DRAFT_FILENAME = "draft.md"
ASSEMBLED_FILENAME = "assembled-final.md"


class ChangelogPipelineError(Exception):
    pass


def write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def write_json(path, payload):
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def atomic_copy(src, dest):
    output_dir = os.path.dirname(os.path.abspath(dest)) or "."
    os.makedirs(output_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="changelog-pipeline-", suffix=".md", dir=output_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out_f:
            with open(src, "r", encoding="utf-8") as in_f:
                out_f.write(in_f.read())
        os.replace(tmp_path, os.path.abspath(dest))
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def max_markdown_mtime(target_dir):
    latest = None
    for name in os.listdir(target_dir):
        if not name.endswith(".md"):
            continue
        current = os.path.getmtime(os.path.join(target_dir, name))
        latest = current if latest is None else max(latest, current)
    return latest


def safe_remove_path(target_path):
    if not target_path or not os.path.exists(target_path):
        return
    if os.path.isdir(target_path):
        shutil.rmtree(target_path)
    else:
        os.remove(target_path)


def prepare_pipeline(repos, notices, since, until, order, workdir):
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)

    ledger = build_ledger_payload(repos, notices, since, until, order)
    ledger_path = os.path.join(workdir, LEDGER_FILENAME)
    themes_path = os.path.join(workdir, THEMES_FILENAME)
    manifest_path = os.path.join(workdir, MANIFEST_FILENAME)

    write_json(ledger_path, ledger)
    write_text(themes_path, render_markdown(ledger))

    manifest = {
        "generated_at": ledger["generated_at"],
        "since": ledger["since"],
        "until": ledger["until"],
        "order": ledger["order"],
        "calendar_day_count": ledger.get("calendar_day_count"),
        "active_day_count": ledger.get("active_day_count"),
        "window_days": ledger.get("window_days"),
        "window_count": ledger.get("window_count"),
        "repo_fingerprint": ledger["repo_fingerprint"],
        "repos": ledger["repos"],
        "ledger_path": ledger_path,
        "themes_path": themes_path,
    }
    write_json(manifest_path, manifest)
    return manifest_path, ledger_path, themes_path


def load_manifest(workdir):
    manifest_path = os.path.join(os.path.abspath(workdir), MANIFEST_FILENAME)
    if not os.path.exists(manifest_path):
        raise ChangelogPipelineError(f"缺少 pipeline manifest：{manifest_path}")
    manifest = read_json(manifest_path)
    ledger_path = manifest.get("ledger_path") or os.path.join(os.path.abspath(workdir), LEDGER_FILENAME)
    if not os.path.exists(ledger_path):
        raise ChangelogPipelineError(f"缺少主题账本：{ledger_path}")
    ledger = read_json(ledger_path)
    return manifest_path, manifest, ledger_path, ledger


def validate_ledger_consistency(manifest_path, manifest, ledger_path, ledger):
    for key in (
        "generated_at",
        "since",
        "until",
        "order",
        "calendar_day_count",
        "active_day_count",
        "window_days",
        "window_count",
        "repo_fingerprint",
    ):
        if manifest.get(key) != ledger.get(key):
            raise ChangelogPipelineError(f"manifest 与账本元数据不一致：{key}")
    if manifest.get("ledger_path") and os.path.abspath(manifest["ledger_path"]) != os.path.abspath(ledger_path):
        raise ChangelogPipelineError("manifest 中的 ledger_path 与实际账本路径不一致")


def build_prepare_args_namespace(args):
    return argparse.Namespace(
        repos=args.repos,
        repo_path=args.repo_path,
        repo_discovery_depth=args.repo_discovery_depth,
    )


def finalize_with_draft(workdir, draft_file, output, keep_artifacts=False):
    _manifest_path, manifest, ledger_path, ledger = load_manifest(workdir)
    validate_ledger_consistency(_manifest_path, manifest, ledger_path, ledger)

    draft_path = os.path.abspath(draft_file)
    if not os.path.exists(draft_path):
        raise ChangelogPipelineError(f"草稿文件不存在：{draft_path}")
    if os.path.getmtime(draft_path) < os.path.getmtime(ledger_path):
        raise ChangelogPipelineError("草稿文件早于当前主题账本，请先基于最新账本重生成草稿")

    structure_errors = validate_file(draft_path, manifest["order"], check_tech=True)
    if structure_errors:
        raise ChangelogPipelineError("\n".join(structure_errors))

    semantic_errors = validate_semantics(draft_path, ledger_path, manifest["order"])
    if semantic_errors:
        raise ChangelogPipelineError("\n".join(semantic_errors))

    atomic_copy(draft_path, output)
    if not keep_artifacts:
        assembled_path = os.path.join(os.path.abspath(workdir), ASSEMBLED_FILENAME)
        if os.path.exists(assembled_path):
            os.remove(assembled_path)
    return os.path.abspath(output)


def finalize_with_blocks(workdir, blocks_dir, output, keep_artifacts=False):
    manifest_path, manifest, ledger_path, ledger = load_manifest(workdir)
    validate_ledger_consistency(manifest_path, manifest, ledger_path, ledger)

    blocks_dir = os.path.abspath(blocks_dir)
    if not os.path.isdir(blocks_dir):
        raise ChangelogPipelineError(f"单日块目录不存在：{blocks_dir}")
    latest_block_mtime = max_markdown_mtime(blocks_dir)
    if latest_block_mtime is None:
        raise ChangelogPipelineError(f"单日块目录为空：{blocks_dir}")
    if latest_block_mtime < os.path.getmtime(ledger_path):
        raise ChangelogPipelineError("单日块早于当前主题账本，请先基于最新账本重生成日期块")

    assembled_path = os.path.join(os.path.abspath(workdir), ASSEMBLED_FILENAME)
    try:
        assemble_blocks(blocks_dir, assembled_path, order=manifest["order"], cleanup_dir=None, keep_temp=True)
    except ChangelogAssembleError as exc:
        raise ChangelogPipelineError(str(exc)) from exc

    semantic_errors = validate_semantics(assembled_path, ledger_path, manifest["order"])
    if semantic_errors:
        raise ChangelogPipelineError("\n".join(semantic_errors))

    atomic_copy(assembled_path, output)
    if not keep_artifacts and os.path.exists(assembled_path):
        os.remove(assembled_path)
    return os.path.abspath(output)


def generate_draft(workdir, output_path=None):
    _manifest_path, manifest, ledger_path, ledger = load_manifest(workdir)
    validate_ledger_consistency(_manifest_path, manifest, ledger_path, ledger)
    output_path = os.path.abspath(output_path or os.path.join(os.path.abspath(workdir), DRAFT_FILENAME))
    draft = render_changelog(ledger)
    write_text(output_path, draft)
    return output_path


def run_pipeline(repos, notices, since, until, order, output, workdir=None, keep_artifacts=False):
    temp_workdir = None
    if workdir:
        pipeline_workdir = os.path.abspath(workdir)
        os.makedirs(pipeline_workdir, exist_ok=True)
    else:
        temp_workdir = tempfile.mkdtemp(prefix="daily-changelog-pipeline-")
        pipeline_workdir = temp_workdir

    try:
        prepare_pipeline(repos, notices, since, until, order, pipeline_workdir)
        draft_path = generate_draft(pipeline_workdir)
        final_path = finalize_with_draft(
            pipeline_workdir,
            draft_path,
            output,
            keep_artifacts=keep_artifacts or bool(workdir),
        )
        if temp_workdir and not keep_artifacts:
            safe_remove_path(temp_workdir)
            temp_workdir = None
        return final_path, pipeline_workdir
    except Exception:
        if temp_workdir and not keep_artifacts:
            safe_remove_path(temp_workdir)
        raise


def prepare_main(args):
    repos, notices, errors = resolve_repo_inputs(build_prepare_args_namespace(args))
    if errors:
        raise ChangelogPipelineError("\n".join(errors))

    since, date_notices = resolve_since_value(args.since, repos)
    notices.extend(date_notices)
    until = args.until or since
    manifest_path, ledger_path, themes_path = prepare_pipeline(
        repos,
        notices,
        since,
        until,
        args.order,
        args.workdir,
    )
    print("CHANGELOG_PIPELINE_PREPARE_OK")
    print(os.path.abspath(manifest_path))
    print(os.path.abspath(ledger_path))
    print(os.path.abspath(themes_path))


def finalize_main(args):
    if bool(args.draft_file) == bool(args.blocks_dir):
        raise ChangelogPipelineError("必须且只能提供 `--draft-file` 或 `--blocks-dir` 其中一个")
    if args.draft_file:
        output_path = finalize_with_draft(args.workdir, args.draft_file, args.output, keep_artifacts=args.keep_artifacts)
    else:
        output_path = finalize_with_blocks(args.workdir, args.blocks_dir, args.output, keep_artifacts=args.keep_artifacts)
    print("CHANGELOG_PIPELINE_FINALIZE_OK")
    print(output_path)


def generate_main(args):
    draft_path = generate_draft(args.workdir, args.output)
    print("CHANGELOG_PIPELINE_GENERATE_OK")
    print(draft_path)


def run_main(args):
    repos, notices, errors = resolve_repo_inputs(build_prepare_args_namespace(args))
    if errors:
        raise ChangelogPipelineError("\n".join(errors))

    since, date_notices = resolve_since_value(args.since, repos)
    notices.extend(date_notices)
    until = args.until or since
    final_path, pipeline_workdir = run_pipeline(
        repos,
        notices,
        since,
        until,
        args.order,
        args.output,
        workdir=args.workdir,
        keep_artifacts=args.keep_artifacts,
    )
    print("CHANGELOG_PIPELINE_RUN_OK")
    print(final_path)
    if args.keep_artifacts or args.workdir:
        print(pipeline_workdir)


def main():
    parser = argparse.ArgumentParser(description="Prepare or finalize a gated changelog generation pipeline.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="生成最新主题账本和 pipeline manifest")
    prepare_parser.add_argument("--since", default=None, help="开始日期；支持 YYYY-MM-DD、earliest、auto")
    prepare_parser.add_argument("--until", default=None, help="结束日期；默认与 --since 相同")
    prepare_parser.add_argument("--repo-path", default=None, help="单个仓库路径，或包含多个子仓库的项目目录")
    prepare_parser.add_argument("--repos", default=None, help="多仓库，格式: name:path,name:path")
    prepare_parser.add_argument("--repo-discovery-depth", type=int, default=None, help="自动发现子仓库的最大目录深度；默认不限制")
    prepare_parser.add_argument("--order", choices=["asc", "desc"], default="desc", help="日期顺序")
    prepare_parser.add_argument("--workdir", required=True, help="pipeline 工作目录")

    run_parser = subparsers.add_parser("run", help="黑盒生成最终 changelog；内部自动执行 prepare/generate/finalize")
    run_parser.add_argument("--since", default=None, help="开始日期；支持 YYYY-MM-DD、earliest、auto")
    run_parser.add_argument("--until", default=None, help="结束日期；默认与 --since 相同")
    run_parser.add_argument("--repo-path", default=None, help="单个仓库路径，或包含多个子仓库的项目目录")
    run_parser.add_argument("--repos", default=None, help="多仓库，格式: name:path,name:path")
    run_parser.add_argument("--repo-discovery-depth", type=int, default=None, help="自动发现子仓库的最大目录深度；默认不限制")
    run_parser.add_argument("--order", choices=["asc", "desc"], default="desc", help="日期顺序")
    run_parser.add_argument("--output", required=True, help="最终 changelog 输出文件")
    run_parser.add_argument("--workdir", help="可选调试工作目录；默认自动创建并清理")
    run_parser.add_argument("--keep-artifacts", action="store_true", help="保留中间产物；默认在自动工作目录下清理")

    generate_parser = subparsers.add_parser("generate", help="基于最新主题账本自动生成草稿")
    generate_parser.add_argument("--workdir", required=True, help="prepare 阶段生成的 pipeline 工作目录")
    generate_parser.add_argument("--output", help="草稿输出路径；默认写到 workdir/draft.md")

    finalize_parser = subparsers.add_parser("finalize", help="基于最新 manifest 和主题账本校验并落盘最终 changelog")
    finalize_parser.add_argument("--workdir", required=True, help="prepare 阶段生成的 pipeline 工作目录")
    finalize_parser.add_argument("--draft-file", help="基于最新主题账本生成的最终草稿文件")
    finalize_parser.add_argument("--blocks-dir", help="基于最新主题账本生成的单日块目录")
    finalize_parser.add_argument("--output", required=True, help="最终 changelog 输出文件")
    finalize_parser.add_argument("--keep-artifacts", action="store_true", help="保留 assembled 中间文件")

    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare_main(args)
        elif args.command == "run":
            run_main(args)
        elif args.command == "generate":
            generate_main(args)
        else:
            finalize_main(args)
    except ChangelogPipelineError as exc:
        print("CHANGELOG_PIPELINE_ERROR")
        for line in str(exc).splitlines():
            print(f"- {line}")
        sys.exit(1)


if __name__ == "__main__":
    main()
