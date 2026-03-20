#!/usr/bin/env python3

import argparse
import json
import os
import shutil
import sys
import tempfile

from changelog_draft import build_context_payload, build_ledger_payload, render_markdown
from changelog_generate import build_generation_packets, render_changelog_from_entries, render_context
from changelog_guard import validate_file
from changelog_semantic_guard import validate_generated_entries_file
from context_fetcher import resolve_repo_inputs, resolve_since_value


MANIFEST_FILENAME = "pipeline-manifest.json"
LEDGER_FILENAME = "theme-ledger.json"
THEMES_FILENAME = "themes.md"
PACKETS_FILENAME = "entry-packets.json"
ENTRIES_FILENAME = "generated-entries.json"
DRAFT_FILENAME = "draft.md"


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

    ledger = build_context_payload(repos, notices, since, until, order)
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


def generate_packets(workdir, output_path=None):
    _manifest_path, manifest, ledger_path, ledger = load_manifest(workdir)
    validate_ledger_consistency(_manifest_path, manifest, ledger_path, ledger)
    output_path = os.path.abspath(output_path or os.path.join(os.path.abspath(workdir), PACKETS_FILENAME))
    packets = build_generation_packets(ledger)
    write_json(output_path, packets)
    return output_path


def finalize_with_entries(workdir, entries_file, output, keep_artifacts=False):
    _manifest_path, manifest, ledger_path, ledger = load_manifest(workdir)
    validate_ledger_consistency(_manifest_path, manifest, ledger_path, ledger)

    entries_path = os.path.abspath(entries_file)
    if not os.path.exists(entries_path):
        raise ChangelogPipelineError(f"生成条目文件不存在：{entries_path}")
    if os.path.getmtime(entries_path) < os.path.getmtime(ledger_path):
        raise ChangelogPipelineError("生成条目文件早于当前主题账本，请先基于最新账本重新生成条目")

    semantic_errors = validate_generated_entries_file(entries_path, ledger_path)
    if semantic_errors:
        raise ChangelogPipelineError("\n".join(semantic_errors))

    entries_payload = read_json(entries_path)
    draft_path = os.path.join(os.path.abspath(workdir), DRAFT_FILENAME)
    draft = render_changelog_from_entries(ledger, entries_payload)
    write_text(draft_path, draft)

    structure_errors = validate_file(draft_path, manifest["order"], check_tech=True)
    if structure_errors:
        raise ChangelogPipelineError("\n".join(structure_errors))

    atomic_copy(draft_path, output)
    if not keep_artifacts and os.path.exists(draft_path):
        os.remove(draft_path)
    return os.path.abspath(output)


def build_context_output(repos, notices, since, until, order):
    ledger = build_context_payload(repos, notices, since, until, order)
    return render_context(ledger)


def run_pipeline(repos, notices, since, until, order, output, workdir=None, keep_artifacts=False):
    raise ChangelogPipelineError(
        "`run` 黑盒模式已移除；当前流程需要 skill 内部先 prepare 生成写作包，再由宿主模型生成条目，最后 finalize。"
    )


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
    output_path = finalize_with_entries(args.workdir, args.entries_file, args.output, keep_artifacts=args.keep_artifacts)
    print("CHANGELOG_PIPELINE_FINALIZE_OK")
    print(output_path)


def generate_main(args):
    packets_path = generate_packets(args.workdir, args.output)
    print("CHANGELOG_PIPELINE_GENERATE_OK")
    print(packets_path)


def run_main(args):
    raise ChangelogPipelineError(
        "`run` 黑盒模式已移除；请在 skill 内部使用 prepare -> generate -> finalize 流程。"
    )


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

    run_parser = subparsers.add_parser("run", help="已废弃；当前 skill 需要宿主模型参与逐条写作")
    run_parser.add_argument("--since", default=None, help="开始日期；支持 YYYY-MM-DD、earliest、auto")
    run_parser.add_argument("--until", default=None, help="结束日期；默认与 --since 相同")
    run_parser.add_argument("--repo-path", default=None, help="单个仓库路径，或包含多个子仓库的项目目录")
    run_parser.add_argument("--repos", default=None, help="多仓库，格式: name:path,name:path")
    run_parser.add_argument("--repo-discovery-depth", type=int, default=None, help="自动发现子仓库的最大目录深度；默认不限制")
    run_parser.add_argument("--order", choices=["asc", "desc"], default="desc", help="日期顺序")
    run_parser.add_argument("--output", required=True, help="最终 changelog 输出文件")
    run_parser.add_argument("--workdir", help="可选调试工作目录；默认自动创建并清理")
    run_parser.add_argument("--keep-artifacts", action="store_true", help="保留中间产物；默认在自动工作目录下清理")

    generate_parser = subparsers.add_parser("generate", help="基于最新主题账本生成逐条写作包")
    generate_parser.add_argument("--workdir", required=True, help="prepare 阶段生成的 pipeline 工作目录")
    generate_parser.add_argument("--output", help=f"写作包输出路径；默认写到 workdir/{PACKETS_FILENAME}")

    finalize_parser = subparsers.add_parser("finalize", help="基于主题账本和宿主模型生成的条目校验并落盘最终 changelog")
    finalize_parser.add_argument("--workdir", required=True, help="prepare 阶段生成的 pipeline 工作目录")
    finalize_parser.add_argument("--entries-file", required=True, help="宿主模型生成的 JSON 条目文件")
    finalize_parser.add_argument("--output", required=True, help="最终 changelog 输出文件")
    finalize_parser.add_argument("--keep-artifacts", action="store_true", help=f"保留 {DRAFT_FILENAME} 中间文件")

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
