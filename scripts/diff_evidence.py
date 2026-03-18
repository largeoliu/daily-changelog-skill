#!/usr/bin/env python3

from typing import Callable, List


def is_changed_diff_line(line: str) -> bool:
    return line.startswith(("+", "-")) and not line.startswith(("+++", "---"))


def should_compact_diff(diff: str, max_total_lines: int = 120, max_changed_lines: int = 32) -> bool:
    if not diff:
        return False
    lines = diff.splitlines()
    changed_lines = [line for line in lines if is_changed_diff_line(line)]
    return len(lines) > max_total_lines or len(changed_lines) > max_changed_lines


def extract_diff_hunks(diff: str) -> List[List[str]]:
    hunks: List[List[str]] = []
    current: List[str] = []

    for line in diff.splitlines():
        if line.startswith("@@ "):
            if current:
                hunks.append(current)
            current = [line]
            continue

        if line.startswith("diff --git "):
            if current:
                hunks.append(current)
                current = []
            continue

        if current:
            current.append(line)

    if current:
        hunks.append(current)

    return hunks


def compact_hunk(
    hunk_lines: List[str],
    matcher: Callable[[str], bool],
    context_lines: int = 2,
    max_body_lines: int = 18,
) -> str:
    if not hunk_lines:
        return ""

    has_header = hunk_lines[0].startswith("@@ ")
    header = hunk_lines[0] if has_header else "@@ compact evidence @@"
    body = hunk_lines[1:] if has_header else hunk_lines

    matched_indexes = [index for index, line in enumerate(body) if matcher(line)]
    if not matched_indexes:
        return ""

    keep_indexes = set()
    for index in matched_indexes[:8]:
        for offset in range(max(0, index - context_lines), min(len(body), index + context_lines + 1)):
            keep_indexes.add(offset)

    selected_lines: List[str] = []
    kept_count = 0
    last_index = None
    for index in sorted(keep_indexes):
        if kept_count >= max_body_lines:
            break
        if last_index is not None and index > last_index + 1:
            selected_lines.append("...")
        selected_lines.append(body[index])
        kept_count += 1
        last_index = index

    return "\n".join([header] + selected_lines)


def fallback_changed_lines(diff: str, max_lines: int = 20) -> str:
    changed_lines = [line for line in diff.splitlines() if is_changed_diff_line(line)]
    if not changed_lines:
        return diff
    if len(changed_lines) > max_lines:
        changed_lines = changed_lines[:max_lines] + ["..."]
    return "\n".join(["@@ compact evidence @@"] + changed_lines)


def build_compact_evidence(
    diff: str,
    matcher: Callable[[str], bool],
    max_hunks: int = 3,
    context_lines: int = 2,
    max_body_lines: int = 18,
) -> str:
    if not diff:
        return ""

    if not should_compact_diff(diff):
        return diff

    snippets: List[str] = []
    for hunk in extract_diff_hunks(diff):
        snippet = compact_hunk(
            hunk,
            matcher,
            context_lines=context_lines,
            max_body_lines=max_body_lines,
        )
        if snippet:
            snippets.append(snippet)
        if len(snippets) >= max_hunks:
            break

    if not snippets:
        return fallback_changed_lines(diff)

    return "\n\n...\n\n".join(snippets)
