#!/usr/bin/env python3

import argparse
import json
from collections import defaultdict

from changelog_guard import TITLE
from context_fetcher import has_strong_product_term, is_low_quality_title, pick_theme_title


NEW_FEATURE = "### ✨ 新功能"
CHANGE = "### 🔄 功能变更"
TECH = "### 🔧 技术改造"
BUG = "### 🐛 Bug 修复"


def combined_terms(record):
    values = []
    values.extend(record.get("anchor_candidates") or [])
    values.extend(record.get("merge_terms") or [])
    return [str(value or "") for value in values if str(value or "").strip()]


def has_any(record, *terms):
    joined = " ".join(combined_terms(record))
    return any(term in joined for term in terms)


def quoted_title(record):
    title = record_display_title(record)
    if not title:
        return ""
    return f"“{title}”"


def record_display_title(record):
    candidates = []
    candidates.append(record.get("domain_title"))
    candidates.append(record.get("theme_title"))
    candidates.extend(record.get("anchor_candidates") or [])
    title = pick_theme_title(candidates, fallback="")
    if not title or is_low_quality_title(title):
        return ""
    if record.get("record_kind") == "launch" and len(title) >= 3 and any(ord(ch) > 127 for ch in title):
        return title
    if not has_strong_product_term([title]) and not any(word in title for word in ("页面", "模块", "看板", "列表", "详情", "配置", "管理", "中心", "视图", "入口")):
        return ""
    return title


def compose_sentence(prefix, details):
    details = [detail.strip("，。 ").lstrip("并").strip() for detail in details if detail and detail.strip("，。 ")]
    if not details:
        return prefix + "。"
    if len(details) == 1:
        return f"{prefix}，{details[0]}。"
    return f"{prefix}，{details[0]}，" + "，".join(f"并{detail}" for detail in details[1:]) + "。"


def bugfix_phrase(record):
    quoted = quoted_title(record)
    if not quoted:
        return ""
    prefix = f"修复{quoted}相关问题"
    details = []
    if has_any(record, "筛选", "查询", "条件"):
        details.append("修复筛选与查询条件生效异常")
    if has_any(record, "地图", "联动", "定位", "时间轴"):
        details.append("修复页面联动和定位展示问题")
    if has_any(record, "空状态", "暂无", "未选择", "加载", "请重新登录"):
        details.append("修复空状态与异常提示不准确的问题")
    if has_any(record, "详情", "明细"):
        details.append("修复明细展示和下钻查看异常")
    if not details:
        details.append("提升页面展示、交互和数据呈现的稳定性")
    return compose_sentence(prefix, details)


def tech_phrase(record):
    quoted = quoted_title(record)
    if not quoted:
        return ""
    prefix = f"{quoted}完成底层能力改造"
    details = []
    if has_any(record, "异常", "校验", "错误", "提示"):
        details.append("异常处理与校验反馈更统一")
    if has_any(record, "解析", "转换", "聚合", "统计"):
        details.append("数据处理与统计链路更稳定")
    if has_any(record, "详情", "筛选", "查询", "字段"):
        details.append("为后续页面扩展和能力补充预留更稳的支撑")
    if not details:
        details.append("提升数据处理、异常兜底和后续扩展的稳定性")
    return compose_sentence(prefix, details)


def read_ledger(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def slot_phrase(record):
    title = record_display_title(record)
    if not title:
        return ""
    slot = record.get("primary_slot") or record.get("feature_slot")
    kind = record.get("record_kind")
    merged = set(record.get("merged_slots") or [])

    if kind == "launch":
        if "query_filter" in merged and "visual_ux" in merged:
            return f"新增“{title}”页面，支持核心数据查看、条件筛选及页面联动操作。"
        if "query_filter" in merged:
            return f"新增“{title}”页面，支持基础数据查看和条件筛选。"
        if "detail_display" in merged:
            return f"新增“{title}”页面，支持查看核心业务数据和明细内容。"
        return f"新增“{title}”页面，支持查看核心业务数据。"

    if kind == "bugfix":
        return bugfix_phrase(record)

    if kind == "tech":
        return tech_phrase(record)

    if slot == "query_filter":
        return f"{title}新增条件筛选能力，支持按关键业务维度快速定位目标数据。"
    if slot == "visual_ux":
        return f"{title}优化页面展示与交互联动，信息浏览和定位操作更顺畅。"
    if slot == "detail_display":
        return f"{title}补充明细展示能力，支持更完整地查看业务数据与关联信息。"
    if slot == "feature_flow":
        return f"{title}新增业务能力补充，支持更细粒度地完成日常分析操作。"
    return f"{title}能力增强，支持更完整地完成相关业务操作。"


def record_to_entry(record):
    kind = record.get("record_kind")
    slot = record.get("primary_slot") or record.get("feature_slot")
    if kind == "launch":
        category = NEW_FEATURE
    elif kind == "enhancement":
        category = CHANGE
    elif kind == "tech":
        category = TECH
    elif kind == "bugfix":
        category = BUG
    else:
        category = CHANGE if slot in {"query_filter", "visual_ux", "detail_display", "feature_flow"} else TECH
    phrase = slot_phrase(record).rstrip("。")
    if not phrase:
        return None
    return category, f"- {phrase}"


def render_changelog(ledger):
    grouped = defaultdict(lambda: defaultdict(list))
    records = ledger.get("domain_day_records") or ledger.get("published_themes") or []
    for record in records:
        if not record.get("should_publish", True):
            continue
        rendered = record_to_entry(record)
        if not rendered:
            continue
        category, entry = rendered
        entries = grouped[record["delivery_date"]][category]
        if entry not in entries:
            entries.append(entry)

    order = ledger.get("order", "desc")
    dates = sorted(grouped.keys(), reverse=(order == "desc"))

    lines = [TITLE]
    for index, day in enumerate(dates):
        lines.append("")
        lines.append(f"## {day}")
        lines.append("")
        for category in (NEW_FEATURE, CHANGE, TECH, BUG):
            entries = grouped[day].get(category) or []
            if not entries:
                continue
            lines.append(category)
            lines.append("")
            lines.extend(entries)
            lines.append("")
        if lines[-1] == "":
            lines.pop()
        if index != len(dates) - 1:
            lines.append("")
            lines.append("---")

    return "\n".join(lines).strip() + "\n"


def main():
    parser = argparse.ArgumentParser(description="Generate a draft changelog directly from domain-day records.")
    parser.add_argument("--ledger", required=True, help="theme-ledger.json path")
    parser.add_argument("--output", required=True, help="draft markdown output path")
    args = parser.parse_args()

    ledger = read_ledger(args.ledger)
    draft = render_changelog(ledger)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(draft)
    print("CHANGELOG_GENERATE_OK")
    print(args.output)


if __name__ == "__main__":
    main()
