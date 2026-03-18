#!/usr/bin/env python3

import argparse
import json
import re
import sys

from changelog_guard import validate_file
from context_fetcher import ABSTRACT_ENTRY_PATTERNS, ANCHOR_HINT_WORDS, build_anchor_candidates, candidate_similarity, detect_domain, extract_merge_terms, infer_feature_slot


DATE_HEADING_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})$")
CATEGORY_RE = re.compile(r"^### ")


CATEGORY_TO_RECORD_KIND = {
    "### ✨ 新功能": "launch",
    "### 🔄 功能变更": "enhancement",
    "### 🔧 技术改造": "tech",
    "### 🐛 Bug 修复": "bugfix",
}


def parse_entries(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    entries = []
    current_date = None
    current_category = None
    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        date_match = DATE_HEADING_RE.match(line)
        if date_match:
            current_date = date_match.group(1)
            current_category = None
            continue
        if CATEGORY_RE.match(line):
            current_category = line
            continue
        if line.startswith("- "):
            entries.append(
                {
                    "date": current_date,
                    "category": current_category,
                    "text": line[2:].strip(),
                    "line_no": line_no,
                }
            )
    return entries


def has_anchor(text):
    return any(word in text for word in ANCHOR_HINT_WORDS)


def entry_candidate(entry_text):
    domain = detect_domain(labels=[entry_text], titles=[entry_text])
    feature_slot = infer_feature_slot([entry_text], has_route=False, launch_support=False, support_only=False)
    return {
        "theme_key": f"{(domain or {}).get('key') or ''}:{feature_slot}",
        "theme_title": entry_text,
        "domain_key": (domain or {}).get("key") or "",
        "domain_title": (domain or {}).get("title") or "",
        "feature_slot": feature_slot,
        "anchor_candidates": build_anchor_candidates([entry_text]),
        "merge_terms": extract_merge_terms([entry_text]),
        "primary_family": feature_slot,
    }


def expected_record_kind(entry):
    return CATEGORY_TO_RECORD_KIND.get(entry.get("category"))


def match_theme(entry, themes):
    probe = entry_candidate(entry["text"])
    candidates = list(themes)
    if entry.get("date"):
        same_day = [theme for theme in candidates if theme.get("delivery_date") == entry["date"]]
        if same_day:
            candidates = same_day

    exact_anchor_matches = [
        theme for theme in candidates
        if any(anchor and anchor in entry["text"] for anchor in (theme.get("anchor_candidates") or []))
    ]
    if exact_anchor_matches:
        candidates = exact_anchor_matches

    exact_title_matches = [
        theme for theme in candidates
        if (theme.get("theme_title") and theme["theme_title"] in entry["text"])
        or (theme.get("domain_title") and theme["domain_title"] in entry["text"])
    ]
    if exact_title_matches:
        candidates = exact_title_matches

    if probe.get("domain_key"):
        same_domain = [theme for theme in candidates if theme.get("domain_key") == probe["domain_key"]]
        if same_domain:
            candidates = same_domain

    expected_kind = expected_record_kind(entry)
    if expected_kind:
        same_kind = [theme for theme in candidates if theme.get("record_kind") == expected_kind]
        if same_kind:
            candidates = same_kind

    best_theme = None
    best_score = -1
    for theme in candidates:
        score = candidate_similarity(theme, probe)
        if any(anchor and anchor in entry["text"] for anchor in (theme.get("anchor_candidates") or [])):
            score += 5
        if theme.get("theme_title") and theme["theme_title"] in entry["text"]:
            score += 6
        if theme.get("domain_title") and theme["domain_title"] in entry["text"]:
            score += 6
        if theme.get("delivery_date") == entry.get("date"):
            score += 3
        if probe.get("domain_key") and theme.get("domain_key") == probe["domain_key"]:
            score += 3
        if theme.get("feature_slot") == probe.get("feature_slot"):
            score += 2
        if expected_kind and theme.get("record_kind") == expected_kind:
            score += 3
        if score > best_score:
            best_theme = theme
            best_score = score
    return best_theme, best_score


def validate_semantics(file_path, ledger_path, order):
    errors = []
    structure_errors = validate_file(file_path, order, check_tech=True)
    if structure_errors:
        return structure_errors

    with open(ledger_path, "r", encoding="utf-8") as f:
        ledger = json.load(f)

    themes = ledger.get("domain_day_records") or ledger.get("published_themes") or []
    entries = parse_entries(file_path)
    seen_themes = {}
    seen_domain_date_kind = {}
    matched_by_domain_date = {}

    for entry in entries:
        text = entry["text"]
        if any(re.search(pattern, text) for pattern in ABSTRACT_ENTRY_PATTERNS) and not has_anchor(text):
            errors.append(f"第 {entry['line_no']} 行：条目表述过于抽象且缺少场景锚点 `{text}`")

        theme, score = match_theme(entry, themes)
        if theme is None or score < 2:
            errors.append(f"第 {entry['line_no']} 行：未匹配到可靠的主题账本候选 `{text}`")
            continue

        if not theme.get("should_publish"):
            errors.append(f"第 {entry['line_no']} 行：条目命中了仅作支撑证据的主题 `{theme['theme_title']}`，不应单列")

        if entry["date"] != theme.get("delivery_date"):
            errors.append(
                f"第 {entry['line_no']} 行：条目日期 `{entry['date']}` 与主题账本交付日 `{theme.get('delivery_date')}` 不一致：`{theme['theme_title']}`"
            )

        expected_kind = expected_record_kind(entry)
        if expected_kind and theme.get("record_kind") != expected_kind:
            errors.append(
                f"第 {entry['line_no']} 行：条目分类 `{entry['category']}` 与主题账本类型 `{theme.get('record_kind')}` 不一致：`{theme['theme_title']}`"
            )

        if not has_anchor(text):
            matched_anchor = any(anchor and anchor in text for anchor in theme.get("anchor_candidates") or [])
            if not matched_anchor:
                errors.append(f"第 {entry['line_no']} 行：条目缺少明确场景锚点 `{text}`")

        previous = seen_themes.get(theme["theme_id"])
        if previous:
            errors.append(
                f"第 {entry['line_no']} 行：主题 `{theme['theme_title']}` 在 {previous['date']} 已有同类条目，当前条目疑似重复拆条"
            )
        else:
            seen_themes[theme["theme_id"]] = {"date": entry["date"], "line_no": entry["line_no"]}

        domain_day_kind = (theme.get("domain_key"), entry["date"], theme.get("record_kind"))
        previous_kind = seen_domain_date_kind.get(domain_day_kind)
        if previous_kind:
            errors.append(
                f"第 {entry['line_no']} 行：功能域 `{theme['theme_title']}` 在 {entry['date']} 的 `{theme.get('record_kind')}` 已出现，不应重复写多条"
            )
        else:
            seen_domain_date_kind[domain_day_kind] = entry["line_no"]

        matched_by_domain_date.setdefault((theme.get("domain_key"), entry["date"]), []).append((entry, theme))

    for (domain_key, delivery_date), matched_items in matched_by_domain_date.items():
        day_themes = [
            theme
            for theme in themes
            if theme.get("domain_key") == domain_key and theme.get("delivery_date") == delivery_date
        ]
        if not day_themes:
            continue
        if any(theme.get("record_kind") == "launch" for theme in day_themes) and len(matched_items) > 1:
            domain_title = matched_items[0][1].get("theme_title") or matched_items[0][1].get("domain_title") or domain_key
            errors.append(
                f"日期 `{delivery_date}` 的功能域 `{domain_title}` 存在新功能上线时，不应再同时输出功能变更、技术改造或 Bug 修复"
            )

    return errors


def main():
    parser = argparse.ArgumentParser(description="Validate final changelog semantics against a merged theme ledger.")
    parser.add_argument("--file", required=True, help="待校验的 changelog 文件路径")
    parser.add_argument("--ledger", required=True, help="`changelog_draft.py` 生成的主题账本 JSON")
    parser.add_argument("--order", choices=["asc", "desc", "any"], default="any", help="日期顺序要求")
    args = parser.parse_args()

    errors = validate_semantics(args.file, args.ledger, args.order)
    if errors:
        print("CHANGELOG_SEMANTIC_GUARD_ERROR")
        for error in errors:
            print(f"- {error}")
        sys.exit(1)

    print("CHANGELOG_SEMANTIC_GUARD_OK")


if __name__ == "__main__":
    main()
