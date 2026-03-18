#!/usr/bin/env python3

import argparse
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime

from changelog_guard import ALLOWED_CATEGORIES, TITLE, RANGE_HEADING_RE, validate_file


DATE_HEADING_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})$")


class ChangelogAssembleError(Exception):
    pass


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def read_block(block_path):
    with open(block_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    errors = []
    if any(line.strip() == TITLE for line in lines):
        errors.append("单日块不应包含总标题 `# 产品更新日志`")

    dates = []
    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("## "):
            if RANGE_HEADING_RE.match(line):
                errors.append(f"第 {line_no} 行：不能使用日期区间标题 `{line}`")
                continue

            match = DATE_HEADING_RE.match(line)
            if not match:
                errors.append(f"第 {line_no} 行：非法日期标题 `{line}`")
                continue

            try:
                parse_date(match.group(1))
            except ValueError:
                errors.append(f"第 {line_no} 行：日期不存在 `{line}`")
                continue

            dates.append(match.group(1))
            continue

        if line.startswith("### ") and line not in ALLOWED_CATEGORIES:
            errors.append(f"第 {line_no} 行：非法分类标题 `{line}`")

    if not dates:
        errors.append("未找到单日日期标题")
        return None, errors

    if len(dates) > 1:
        errors.append(f"单日块只能包含 1 个日期标题，当前找到 {len(dates)} 个")

    if errors:
        return None, errors

    content = "\n".join(lines).strip()
    return {"date": dates[0], "content": content, "path": block_path}, []


def safe_remove_path(target_path):
    if not target_path or not os.path.exists(target_path):
        return
    if os.path.isdir(target_path):
        shutil.rmtree(target_path)
    else:
        os.remove(target_path)


def is_path_within(child_path, parent_path):
    try:
        return os.path.commonpath([os.path.abspath(child_path), os.path.abspath(parent_path)]) == os.path.abspath(parent_path)
    except ValueError:
        return False


def assemble_blocks(blocks_dir, output, order="desc", cleanup_dir=None, keep_temp=False):
    blocks_dir = os.path.abspath(blocks_dir)
    output_path = os.path.abspath(output)
    cleanup_dir = os.path.abspath(cleanup_dir) if cleanup_dir else None

    cleanup_targets = [cleanup_dir] if cleanup_dir else [blocks_dir]

    for cleanup_target in cleanup_targets:
        if is_path_within(output_path, cleanup_target):
            raise ChangelogAssembleError(f"输出文件不能位于待清理目录内：{cleanup_target}")

    if not os.path.isdir(blocks_dir):
        raise ChangelogAssembleError(f"单日块目录不存在：{blocks_dir}")

    block_paths = sorted(
        os.path.join(blocks_dir, name)
        for name in os.listdir(blocks_dir)
        if name.endswith(".md")
    )
    if not block_paths:
        raise ChangelogAssembleError(f"单日块目录为空：{blocks_dir}")

    blocks = []
    errors = []
    for block_path in block_paths:
        block, block_errors = read_block(block_path)
        if block_errors:
            errors.extend([f"{block_path}: {error}" for error in block_errors])
            continue
        blocks.append(block)
    if errors:
        if not keep_temp:
            for cleanup_target in cleanup_targets:
                safe_remove_path(cleanup_target)
        raise ChangelogAssembleError("\n".join(errors))

    blocks.sort(key=lambda item: item["date"], reverse=(order == "desc"))

    final_parts = [TITLE]
    for index, block in enumerate(blocks):
        final_parts.append("")
        final_parts.append(block["content"])
        if index != len(blocks) - 1:
            final_parts.append("")
            final_parts.append("---")

    final_content = "\n".join(final_parts).strip() + "\n"
    output_dir = os.path.dirname(output_path) or "."
    os.makedirs(output_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix="changelog-", suffix=".md", dir=output_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(final_content)

        guard_errors = validate_file(tmp_path, order, check_tech=True)
        if guard_errors:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            if not keep_temp:
                for cleanup_target in cleanup_targets:
                    safe_remove_path(cleanup_target)
            raise ChangelogAssembleError("\n".join(guard_errors))

        os.replace(tmp_path, output_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    if not keep_temp:
        for cleanup_target in cleanup_targets:
            safe_remove_path(cleanup_target)

    return output_path


def main():
    parser = argparse.ArgumentParser(description="Assemble daily changelog blocks into a final validated changelog.")
    parser.add_argument("--blocks-dir", required=True, help="单日 changelog 块所在目录")
    parser.add_argument("--output", required=True, help="最终 changelog 输出文件")
    parser.add_argument("--order", choices=["asc", "desc"], default="desc", help="日期顺序")
    parser.add_argument("--cleanup-dir", help="汇总完成后要清理的临时工作目录；通常可传整个临时工作区")
    parser.add_argument("--keep-temp", action="store_true", help="保留单日块和临时工作目录，便于排查问题")
    args = parser.parse_args()

    try:
        output_path = assemble_blocks(
            args.blocks_dir,
            args.output,
            order=args.order,
            cleanup_dir=args.cleanup_dir,
            keep_temp=args.keep_temp,
        )
    except ChangelogAssembleError as exc:
        print("CHANGELOG_ASSEMBLE_ERROR")
        for line in str(exc).splitlines():
            print(f"- {line}")
        sys.exit(1)

    print("CHANGELOG_ASSEMBLE_OK")
    print(output_path)


if __name__ == "__main__":
    main()
