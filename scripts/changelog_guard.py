#!/usr/bin/env python3

import argparse
import re
import sys
from datetime import datetime


TITLE = "# 产品更新日志"
DATE_HEADING_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})$")
RANGE_HEADING_RE = re.compile(r"^## .*?(?:\s+[~～]\s+|\s+to\s+|\s+至\s+|\s+-\s+).+")
ALLOWED_CATEGORIES = {
    "### ✨ 新功能",
    "### 🔄 功能变更",
    "### 🔧 技术改造",
    "### 🐛 Bug 修复",
}
FILE_NAME_RE = re.compile(r"\b[A-Za-z0-9_.-]+\.(?:java|ts|tsx|js|jsx|vue|sql|py)\b")
CLASS_NAME_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9]*(?:Controller|Service(?:Impl)?|DTO|Dto|VO|Vo|Req|Request|Resp|Response|Query|Action|Mapper|Listener|Interceptor|Config|Configuration|Enum|Helper|Util|Client|Model)\b"
)
ROUTE_PATH_RE = re.compile(r"/(?:[A-Za-z0-9_-]+|\{[A-Za-z0-9_-]+\})(?:/(?:[A-Za-z0-9_-]+|\{[A-Za-z0-9_-]+\}))*")
ROUTE_FRAGMENT_RE = re.compile(r"\b(?:[a-z0-9_-]+|\{[A-Za-z0-9_-]+\})(?:\s*/\s*(?:[a-z0-9_-]+|\{[A-Za-z0-9_-]+\}))+\b")
TECH_TERM_RE = re.compile(r"\b(?:API|RPC|Dubbo|DTO|VO|Req|Resp|Query|Mapper|Interceptor|Apollo|Doris|SQL|Schema|MVC|Controller|Service)\b")
NON_PATH_FRAGMENT_SEGMENTS = {"app", "web", "pc", "h5", "ios", "android", "b2b", "b2c", "saas"}


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def is_likely_route_fragment(token):
    segments = [segment.strip().strip("{}") for segment in token.split("/") if segment.strip()]
    if len(segments) < 2:
        return False
    if all(segment in NON_PATH_FRAGMENT_SEGMENTS for segment in segments):
        return False
    return True


def find_technical_leaks(line):
    issues = []
    stripped = line.strip()
    if not stripped.startswith("- "):
        return issues

    text = stripped[2:].strip()

    patterns = [
        (FILE_NAME_RE, "文件名", "请改写成页面、模块或功能描述，不要出现文件名"),
        (CLASS_NAME_RE, "类名/对象名", "请改写成产品能力或业务动作，不要出现类名、DTO、Controller、Service 等技术名"),
        (ROUTE_PATH_RE, "接口路径", "请改写成页面、模块或操作场景，不要直接写接口路径"),
        (ROUTE_FRAGMENT_RE, "路径片段", "请改写成业务流程或页面能力，不要直接写英文路径片段"),
        (TECH_TERM_RE, "技术术语", "请翻译成用户能理解的产品语言"),
    ]

    seen = set()
    for regex, kind, hint in patterns:
        for match in regex.finditer(text):
            token = match.group(0)
            if regex is ROUTE_FRAGMENT_RE and not is_likely_route_fragment(token):
                continue
            if token in seen:
                continue
            seen.add(token)
            issues.append((kind, token, hint))

    return issues


def validate_file(file_path, order, check_tech):
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    errors = []
    first_non_empty = next((line.strip() for line in lines if line.strip()), "")
    if first_non_empty != TITLE:
        errors.append(f"缺少或错误的标题：第一条非空内容必须是 `{TITLE}`")

    seen_dates = set()
    parsed_dates = []
    current_date = None

    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue

        if line == "NO_CHANGES":
            errors.append(f"第 {line_no} 行：最终文件不能包含 `NO_CHANGES`")
            continue

        if line.startswith("## "):
            if RANGE_HEADING_RE.match(line):
                errors.append(f"第 {line_no} 行：日期标题必须是单日，不能使用区间标题 `{line}`")
                current_date = None
                continue

            match = DATE_HEADING_RE.match(line)
            if not match:
                errors.append(f"第 {line_no} 行：非法日期标题 `{line}`，必须严格为 `## YYYY-MM-DD`")
                current_date = None
                continue

            current_date = match.group(1)
            try:
                parsed_date = parse_date(current_date)
            except ValueError:
                errors.append(f"第 {line_no} 行：非法日期标题 `{line}`，日期不存在")
                current_date = None
                continue

            if current_date in seen_dates:
                errors.append(f"第 {line_no} 行：重复日期标题 `{current_date}`")
            else:
                seen_dates.add(current_date)
                parsed_dates.append((line_no, parsed_date, current_date))
            continue

        if line.startswith("### "):
            if current_date is None:
                errors.append(f"第 {line_no} 行：分类标题必须位于某个单日日期块下方")
            elif line not in ALLOWED_CATEGORIES:
                errors.append(f"第 {line_no} 行：非法分类标题 `{line}`")
            continue

        if check_tech and line.startswith("- "):
            for kind, token, hint in find_technical_leaks(line):
                errors.append(f"第 {line_no} 行：检测到{kind} `{token}`；{hint}")

    if not parsed_dates:
        errors.append("未找到任何有效的单日日期块")

    if order in {"asc", "desc"} and parsed_dates:
        date_values = [item[1] for item in parsed_dates]
        expected = sorted(date_values, reverse=(order == "desc"))
        if date_values != expected:
            errors.append(f"日期顺序不符合要求：当前必须按 `{order}` 排列")

    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate final changelog structure.")
    parser.add_argument("--file", required=True, help="待校验的 changelog 文件路径")
    parser.add_argument("--order", choices=["asc", "desc", "any"], default="any", help="日期顺序要求")
    parser.add_argument("--check-tech", action="store_true", help="检查最终输出中的技术名、路径和实现术语")
    args = parser.parse_args()

    errors = validate_file(args.file, args.order, args.check_tech)
    if errors:
        print("CHANGELOG_GUARD_ERROR")
        for error in errors:
            print(f"- {error}")
        sys.exit(1)

    print("CHANGELOG_GUARD_OK")


if __name__ == "__main__":
    main()
