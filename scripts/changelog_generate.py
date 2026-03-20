#!/usr/bin/env python3

import argparse
import json
from collections import defaultdict

from backend_analyzer import inspect_backend_file
from changelog_guard import TITLE
from context_fetcher import contains_cjk, has_meaningful_cjk_title, has_strong_product_term, is_low_quality_title, pick_theme_title
from diff_evidence import fallback_changed_lines
from frontend_analyzer import inspect_frontend_file


NEW_FEATURE = "### ✨ 新功能"
CHANGE = "### 🔄 功能变更"
TECH = "### 🔧 技术改造"
BUG = "### 🐛 Bug 修复"
CATEGORY_ORDER = (NEW_FEATURE, CHANGE, TECH, BUG)


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
    if contains_cjk(title) and not has_meaningful_cjk_title(title):
        return ""
    if record.get("record_kind") == "launch" and len(title) >= 3 and any(ord(ch) > 127 for ch in title):
        return title
    title_tier = record.get("title_source_tier", "unknown")
    if title_tier == "text":
        if not has_strong_product_term([title]):
            return ""
    else:
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
    if has_any(record, "数据", "统计", "计算", "金额"):
        details.append("修复数据计算和统计口径异常")
    if has_any(record, "权限", "登录", "认证", "token"):
        details.append("修复权限校验和登录认证问题")
    if has_any(record, "导出", "下载", "上传"):
        details.append("修复文件导出和上传功能异常")
    if has_any(record, "保存", "提交", "新增"):
        details.append("修复数据保存和提交异常")
    if not details:
        details.append("提升页面展示、交互和数据呈现的稳定性")
    return compose_sentence(prefix, details)


def tech_phrase(record):
    quoted = quoted_title(record)
    if not quoted:
        return ""
    details = []
    if has_any(record, "异常", "校验", "错误", "提示"):
        details.append("异常处理与校验反馈更统一")
    if has_any(record, "解析", "转换", "聚合", "统计"):
        details.append("数据处理与统计链路更稳定")
    if has_any(record, "详情", "筛选", "查询", "字段"):
        details.append("为后续页面扩展和能力补充预留更稳的支撑")
    if has_any(record, "性能", "效率", "优化", "加载", "响应"):
        details.append("性能与响应效率显著提升")
    if has_any(record, "sql", "查询", "数据库", "索引"):
        details.append("数据库查询效率优化")
    if has_any(record, "代码", "重构", "结构"):
        details.append("代码结构更清晰，便于后续维护")
    if has_any(record, "列表", "查看", "数据"):
        details.append("列表加载与数据展示更稳定")
    if has_any(record, "地图", "定位", "轨迹"):
        details.append("地图定位与轨迹展示更精准")
    if not details:
        details.append("提升数据处理、异常兜底和后续扩展的稳定性")
    return compose_sentence(f"{quoted}完成底层能力改造", details)


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
        if slot == "menu_launch":
            return f"“{title}”菜单上线。"
        if slot == "button_action":
            return f"“{title}”按钮上线。"
        if "query_filter" in merged and "visual_ux" in merged:
            return f"新增“{title}”页面，支持核心数据查看、条件筛选及页面联动操作。"
        if "query_filter" in merged:
            return f"新增“{title}”页面，支持基础数据查看和条件筛选。"
        if "detail_display" in merged:
            return f"新增“{title}”页面，支持查看核心业务数据和明细内容。"
        return f"新增“{title}”页面，支持查看核心业务数据。"

    if kind == "bugfix":
        return bugfix_phrase(record)

    if kind == "tech" or slot == "tech_improvement":
        return tech_phrase(record)

    if slot == "query_filter":
        return f"{title}新增条件筛选能力，支持按关键业务维度快速定位目标数据。"
    if slot == "visual_ux":
        return f"{title}优化页面展示与交互联动，信息浏览和定位操作更顺畅。"
    if slot == "detail_display":
        return f"{title}补充明细展示能力，支持更完整地查看业务数据与关联信息。"
    if slot == "feature_flow":
        merged = set(record.get("merged_slots") or [])
        if "query_filter" in merged:
            return f"{title}进行功能优化，增强业务数据处理和筛选能力。"
        if "visual_ux" in merged:
            return f"{title}进行界面和交互优化，提升用户操作体验。"
        if "detail_display" in merged:
            return f"{title}补充数据展示能力，支持更全面的业务数据查看。"
        return None
    if not title:
        return None
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
    phrase = slot_phrase(record)
    if not phrase:
        return None
    phrase = phrase.rstrip("。")
    return category, f"- {phrase}"


def record_category(record):
    kind = record.get("record_kind")
    slot = record.get("primary_slot") or record.get("feature_slot")
    if kind == "launch":
        return NEW_FEATURE
    if kind == "enhancement":
        return CHANGE
    if kind == "tech":
        return TECH
    if kind == "bugfix":
        return BUG
    return CHANGE if slot in {"query_filter", "visual_ux", "detail_display", "feature_flow"} else TECH


def context_display_title(record):
    return record_display_title(record) or str(record.get("domain_title") or record.get("theme_title") or record.get("domain_key") or "未命名主题").strip()


def build_theme_lookup(ledger):
    return {
        str(theme.get("theme_id")): theme
        for theme in ledger.get("themes") or []
        if theme.get("theme_id")
    }


def build_day_repo_lookup(ledger):
    lookup = {}
    for day in ledger.get("days") or []:
        for repo in day.get("repos") or []:
            lookup[(day.get("date"), repo.get("name"))] = repo
    return lookup


def collect_record_sources(record, theme_lookup):
    sources = []
    seen = set()
    for theme_id in record.get("source_theme_ids") or []:
        theme = theme_lookup.get(str(theme_id)) or {}
        for item in theme.get("merged_from") or []:
            key = (
                item.get("date"),
                item.get("repo_name"),
                tuple(item.get("source_refs") or []),
                item.get("evidence_kind"),
            )
            if key in seen:
                continue
            seen.add(key)
            sources.append(item)
    return sorted(sources, key=lambda item: (item.get("date") or "", item.get("repo_name") or "", ",".join(item.get("source_refs") or [])))


def code_language(file_path):
    suffix = str(file_path or "").rsplit(".", 1)
    ext = suffix[-1].lower() if len(suffix) == 2 else ""
    return {
        "java": "java",
        "ts": "ts",
        "tsx": "tsx",
        "js": "js",
        "jsx": "jsx",
        "vue": "vue",
        "sql": "sql",
        "css": "css",
        "scss": "scss",
        "less": "less",
        "md": "md",
    }.get(ext, "text")


def compact_file_evidence(file_path, diff, file_meta=None):
    if not diff:
        return ""
    exists_in_worktree = bool((file_meta or {}).get("exists_in_worktree", True))
    path = str(file_path or "")
    if path.endswith(".java"):
        return (inspect_backend_file(path, diff, compact=True, exists_in_worktree=exists_in_worktree).get("evidence") or "").strip()
    if path.endswith((".ts", ".tsx", ".js", ".jsx", ".vue", ".css", ".scss", ".less", ".sass")):
        return (inspect_frontend_file(path, diff, compact=True, exists_in_worktree=exists_in_worktree).get("evidence") or "").strip()
    return fallback_changed_lines(diff).strip()


def collect_record_context(record, theme_lookup, day_repo_lookup, max_files=4, max_commits=6):
    commits = []
    files = []
    seen_commits = set()
    seen_files = set()
    source_dates = []

    for source in collect_record_sources(record, theme_lookup):
        source_date = source.get("date") or ""
        repo_name = source.get("repo_name") or ""
        if source_date:
            source_dates.append(source_date)
        repo = day_repo_lookup.get((source_date, repo_name)) or {}
        repo_commit_msgs = repo.get("commit_msgs") or []
        file_commits = repo.get("file_commits") or {}
        file_diffs = repo.get("file_diffs") or {}
        file_meta = repo.get("file_meta") or {}
        source_refs = source.get("source_refs") or []

        related_commit_msgs = []
        for ref in source_refs:
            related_commit_msgs.extend(file_commits.get(ref) or [])
        if not related_commit_msgs:
            related_commit_msgs = repo_commit_msgs

        for message in related_commit_msgs:
            if message in seen_commits:
                continue
            seen_commits.add(message)
            commits.append(message)
            if len(commits) >= max_commits:
                break

        for ref in source_refs:
            if (repo_name, source_date, ref) in seen_files:
                continue
            diff = file_diffs.get(ref)
            if not diff:
                continue
            seen_files.add((repo_name, source_date, ref))
            files.append(
                {
                    "repo_name": repo_name,
                    "date": source_date,
                    "file_path": ref,
                    "diff": diff,
                    "file_meta": file_meta.get(ref) or {},
                    "related_commits": dedupe_keep_order(file_commits.get(ref) or [])[:max_commits],
                }
            )
            if len(files) >= max_files:
                break
        if len(files) >= max_files and len(commits) >= max_commits:
            break

    return {
        "commits": commits,
        "files": files,
        "source_dates": sorted({date for date in source_dates if date}),
    }


def dedupe_keep_order(items):
    seen = set()
    result = []
    for item in items or []:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def category_name(category_heading):
    return category_heading.replace("### ", "", 1).strip()


def record_theme_id(record):
    return str(record.get("theme_id") or record.get("record_id") or "").strip()


def build_generation_packets(ledger, max_files=4, max_commits=6):
    records = ledger.get("domain_day_records") or ledger.get("published_themes") or []
    theme_lookup = build_theme_lookup(ledger)
    day_repo_lookup = build_day_repo_lookup(ledger)
    packets = []

    for record in records:
        if not record.get("should_publish", True):
            continue

        theme_id = record_theme_id(record)
        if not theme_id:
            continue

        context = collect_record_context(
            record,
            theme_lookup,
            day_repo_lookup,
            max_files=max_files,
            max_commits=max_commits,
        )
        category_heading = record_category(record)
        packets.append(
            {
                "theme_id": theme_id,
                "record_id": str(record.get("record_id") or "").strip(),
                "delivery_date": record.get("delivery_date"),
                "category_heading": category_heading,
                "category_name": category_name(category_heading),
                "record_kind": record.get("record_kind"),
                "theme_title": context_display_title(record),
                "domain_title": str(record.get("domain_title") or "").strip(),
                "domain_key": str(record.get("domain_key") or "").strip(),
                "primary_slot": str(record.get("primary_slot") or record.get("feature_slot") or "").strip(),
                "merged_slots": [slot for slot in (record.get("merged_slots") or []) if slot],
                "anchor_candidates": [anchor for anchor in (record.get("anchor_candidates") or []) if anchor][:6],
                "keywords": [term for term in (record.get("merge_terms") or []) if term][:12],
                "source_dates": context.get("source_dates") or [],
                "commits": dedupe_keep_order(context.get("commits") or [])[:max_commits],
                "evidence_files": [
                    {
                        "repo_name": item.get("repo_name") or "",
                        "date": item.get("date") or "",
                        "file_path": item.get("file_path") or "",
                        "diff": compact_file_evidence(item.get("file_path"), item.get("diff"), item.get("file_meta")),
                        "related_commits": dedupe_keep_order(item.get("related_commits") or [])[:max_commits],
                    }
                    for item in context.get("files") or []
                ],
            }
        )

    return {
        "order": ledger.get("order", "desc"),
        "records": packets,
    }


def render_generation_packets(ledger):
    return json.dumps(build_generation_packets(ledger), ensure_ascii=False, indent=2) + "\n"


def normalize_generated_entries(generated_entries):
    if isinstance(generated_entries, dict):
        if isinstance(generated_entries.get("entries"), list):
            entries = generated_entries["entries"]
        else:
            entries = [
                {"theme_id": key, "text": value}
                for key, value in generated_entries.items()
            ]
    else:
        entries = generated_entries or []

    normalized = {}
    for item in entries:
        theme_id = str((item or {}).get("theme_id") or "").strip()
        text = str((item or {}).get("text") or (item or {}).get("entry") or "").strip()
        if not theme_id:
            continue
        normalized[theme_id] = text
    return normalized


def render_changelog_from_entries(ledger, generated_entries):
    grouped = defaultdict(lambda: defaultdict(list))
    entry_map = normalize_generated_entries(generated_entries)
    records = ledger.get("domain_day_records") or ledger.get("published_themes") or []

    for record in records:
        if not record.get("should_publish", True):
            continue
        theme_id = record_theme_id(record)
        if not theme_id:
            continue
        text = str(entry_map.get(theme_id) or "").strip().lstrip("- ").strip()
        if not text:
            continue
        category = record_category(record)
        entry = f"- {text.rstrip('。')}"
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
        for category in CATEGORY_ORDER:
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


def render_context(ledger):
    grouped = defaultdict(lambda: defaultdict(list))
    records = ledger.get("domain_day_records") or ledger.get("published_themes") or []
    theme_lookup = build_theme_lookup(ledger)
    day_repo_lookup = build_day_repo_lookup(ledger)

    for record in records:
        if not record.get("should_publish", True):
            continue
        grouped[record["delivery_date"]][record_category(record)].append(record)

    order = ledger.get("order", "desc")
    dates = sorted(grouped.keys(), reverse=(order == "desc"))

    lines = ["# 代码变更上下文", "", "> 仅供主控大模型理解代码变更证据使用；最终产品更新日志应遵循写作规则和模板，不要原样照抄文件路径、类名、接口路径或 commit message。"]

    for date_index, day in enumerate(dates):
        lines.append("")
        lines.append(f"## {day}")
        for category in CATEGORY_ORDER:
            day_records = grouped[day].get(category) or []
            if not day_records:
                continue
            lines.append("")
            lines.append(category)
            for record in day_records:
                title = context_display_title(record)
                context = collect_record_context(record, theme_lookup, day_repo_lookup)
                lines.append("")
                lines.append(f"#### 主题：{title}")
                anchors = [anchor for anchor in (record.get("anchor_candidates") or []) if anchor][:6]
                merged_slots = [slot for slot in (record.get("merged_slots") or []) if slot]
                if anchors:
                    lines.append(f"- 场景锚点：{', '.join(anchors)}")
                if merged_slots:
                    lines.append(f"- 变更信号：{', '.join(merged_slots)}")
                if context["source_dates"]:
                    lines.append(f"- 证据日期：{', '.join(context['source_dates'])}")
                if context["commits"]:
                    lines.append("- 相关提交：")
                    for message in context["commits"]:
                        lines.append(f"  - {message}")
                if context["files"]:
                    lines.append("- 关键文件：")
                    for item in context["files"]:
                        repo_prefix = f"{item['repo_name']}:" if item.get("repo_name") else ""
                        lines.append(f"  - `{repo_prefix}{item['file_path']}`")
                for item in context["files"]:
                    evidence = compact_file_evidence(item["file_path"], item["diff"], item.get("file_meta"))
                    if not evidence:
                        continue
                    lines.append("")
                    lines.append(f"```{code_language(item['file_path'])}")
                    if item.get("repo_name"):
                        lines.append(f"// {item['repo_name']}:{item['file_path']}")
                    else:
                        lines.append(f"// {item['file_path']}")
                    lines.append(evidence)
                    lines.append("```")
        if date_index != len(dates) - 1:
            lines.append("")
            lines.append("---")

    return "\n".join(lines).strip() + "\n"


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
        for category in CATEGORY_ORDER:
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
    parser = argparse.ArgumentParser(description="Generate entry packets or assemble a final changelog from model-written entries.")
    parser.add_argument("--ledger", required=True, help="theme-ledger.json path")
    parser.add_argument("--output", required=True, help="draft markdown output path")
    parser.add_argument("--mode", choices=["packets", "assemble"], default="packets", help="packets=生成逐条写作包；assemble=基于模型条目组装最终 markdown")
    parser.add_argument("--entries", help="assemble 模式下的模型条目 JSON 文件")
    args = parser.parse_args()

    ledger = read_ledger(args.ledger)
    if args.mode == "packets":
        draft = render_generation_packets(ledger)
    else:
        if not args.entries:
            raise SystemExit("assemble 模式必须提供 --entries")
        with open(args.entries, "r", encoding="utf-8") as f:
            generated_entries = json.load(f)
        draft = render_changelog_from_entries(ledger, generated_entries)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(draft)
    print("CHANGELOG_GENERATE_OK")
    print(args.output)


if __name__ == "__main__":
    main()
