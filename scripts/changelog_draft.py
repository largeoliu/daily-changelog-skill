#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import shlex
import sys
from datetime import datetime, timedelta, timezone

from context_fetcher import (
    ANCHOR_HINT_WORDS,
    analyze_repo_window,
    build_repo_theme_candidates,
    candidate_similarity,
    contains_cjk,
    extract_merge_terms,
    has_meaningful_cjk_title,
    has_strong_product_term,
    is_generic_theme_title,
    is_low_quality_title,
    resolve_repo_inputs,
    resolve_since_value,
    should_merge_candidate,
    run_cmd,
)

DEFAULT_WINDOW_DAYS = 31


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def daterange(start, end):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def iter_date_windows(since, until, window_days):
    start = parse_date(since)
    end = parse_date(until)
    if not window_days or window_days <= 0:
        yield since, until
        return

    current = start
    step = timedelta(days=window_days - 1)
    while current <= end:
        window_end = min(current + step, end)
        yield current.isoformat(), window_end.isoformat()
        current = window_end + timedelta(days=1)


def resolve_window_plan(since, until, window_days=None):
    total_days = (parse_date(until) - parse_date(since)).days + 1
    effective_window_days = window_days
    if effective_window_days is None and total_days > DEFAULT_WINDOW_DAYS:
        effective_window_days = DEFAULT_WINDOW_DAYS
    windows = list(iter_date_windows(since, until, effective_window_days))
    return effective_window_days, windows


def collect_repo_active_days(repo_path, since, until):
    quoted_path = shlex.quote(os.path.abspath(repo_path))
    raw = run_cmd(
        f'git -C {quoted_path} log --since="{since} 00:00:00" --until="{until} 23:59:59" --date=short --pretty=%ad'
    )
    if not raw:
        return []
    return sorted({line.strip() for line in raw.splitlines() if line.strip()})


def collect_active_days(repos, since, until):
    active_days = set()
    for _repo_name, repo_path in repos:
        active_days.update(collect_repo_active_days(repo_path, since, until))
    return sorted(active_days)


def iter_window_active_days(active_days, windows):
    indexed = [parse_date(day) for day in active_days]
    for window_since, window_until in windows:
        start = parse_date(window_since)
        end = parse_date(window_until)
        window_days = [
            active_days[index]
            for index, current in enumerate(indexed)
            if start <= current <= end
        ]
        yield window_since, window_until, window_days


def choose_theme_title(current_title, candidate_title):
    if not current_title:
        return candidate_title
    if not candidate_title:
        return current_title
    if is_generic_theme_title(current_title) and not is_generic_theme_title(candidate_title):
        return candidate_title
    if contains_cjk(candidate_title) and not contains_cjk(current_title):
        return candidate_title
    if has_strong_product_term([candidate_title]) and not has_strong_product_term([current_title]):
        return candidate_title
    if len(candidate_title) > len(current_title) and has_strong_product_term([candidate_title]):
        return candidate_title
    return current_title


def flatten_candidates(day_reports):
    flattened = []
    for report in day_reports:
        day = report["date"]
        for repo in report["repos"]:
            for candidate in repo["theme_candidates"]:
                item = dict(candidate)
                item["date"] = day
                item["repo_name"] = repo["name"]
                item["repo_path"] = repo["path"]
                flattened.append(item)
    flattened.sort(key=lambda item: (item["date"], item["theme_title"], item["primary_family"]))
    return flattened


def choose_best_title_tier(tiers):
    if "structural" in tiers:
        return "structural"
    if "unknown" in tiers:
        return "unknown"
    return "text"


def create_aggregate(candidate, index):
    feature_slot = candidate.get("feature_slot") or candidate.get("primary_family") or "feature_flow"
    should_publish = bool(candidate.get("user_visible")) and feature_slot not in {"support_only", "launch_support"}
    return {
        "theme_id": f"theme-{index}",
        "theme_key": candidate.get("theme_key") or "",
        "theme_title": candidate.get("theme_title") or "",
        "domain_key": candidate.get("domain_key") or "",
        "domain_title": candidate.get("domain_title") or candidate.get("theme_title") or "",
        "feature_slot": feature_slot,
        "primary_family": feature_slot,
        "delivery_date": candidate["date"],
        "first_seen_date": candidate["date"],
        "last_seen_date": candidate["date"],
        "dates_seen": [candidate["date"]],
        "anchor_candidates": list(candidate.get("anchor_candidates") or []),
        "merge_terms": list(candidate.get("merge_terms") or []),
        "source_refs": list(candidate.get("source_refs") or []),
        "evidence_kinds": [candidate.get("evidence_kind") or ""],
        "user_visible": bool(candidate.get("user_visible")),
        "support_only": bool(candidate.get("support_only")),
        "should_publish": should_publish,
        "title_source_tier": candidate.get("title_source_tier", "unknown"),
        "merged_from": [
            {
                "date": candidate["date"],
                "repo_name": candidate.get("repo_name"),
                "theme_title": candidate.get("theme_title"),
                "feature_slot": feature_slot,
                "evidence_kind": candidate.get("evidence_kind"),
                "support_only": bool(candidate.get("support_only")),
                "source_refs": list(candidate.get("source_refs") or []),
            }
        ],
    }


def merge_candidate(theme, candidate):
    candidate_slot = candidate.get("feature_slot") or candidate.get("primary_family") or "feature_flow"
    if {theme.get("feature_slot"), candidate_slot} == {"page_launch", "launch_support"}:
        theme["feature_slot"] = "page_launch"
        theme["primary_family"] = "page_launch"
    elif theme.get("feature_slot") == "launch_support" and candidate_slot != "launch_support":
        theme["feature_slot"] = candidate_slot
        theme["primary_family"] = candidate_slot
    elif theme.get("feature_slot") == "support_only" and candidate_slot != "support_only":
        theme["feature_slot"] = candidate_slot
        theme["primary_family"] = candidate_slot

    theme["theme_title"] = choose_theme_title(theme.get("theme_title"), candidate.get("theme_title"))
    theme["domain_title"] = theme.get("domain_title") or candidate.get("domain_title") or theme.get("theme_title")
    if candidate.get("user_visible") or not theme.get("user_visible"):
        theme["delivery_date"] = max(theme["delivery_date"], candidate["date"])
    theme["last_seen_date"] = max(theme["last_seen_date"], candidate["date"])
    theme["first_seen_date"] = min(theme["first_seen_date"], candidate["date"])
    if candidate["date"] not in theme["dates_seen"]:
        theme["dates_seen"].append(candidate["date"])
        theme["dates_seen"].sort()
    for field in ("anchor_candidates", "merge_terms", "source_refs", "evidence_kinds"):
        existing = theme[field]
        for item in candidate.get(field, []) if field != "evidence_kinds" else [candidate.get("evidence_kind")]:
            if item and item not in existing:
                existing.append(item)
    theme["user_visible"] = theme["user_visible"] or bool(candidate.get("user_visible"))
    theme["support_only"] = theme["support_only"] and bool(candidate.get("support_only"))
    theme["should_publish"] = theme["user_visible"] and not theme["support_only"]
    theme["title_source_tier"] = choose_best_title_tier([
        theme.get("title_source_tier", "unknown"),
        candidate.get("title_source_tier", "unknown"),
    ])
    theme["merged_from"].append(
        {
            "date": candidate["date"],
            "repo_name": candidate.get("repo_name"),
            "theme_title": candidate.get("theme_title"),
            "feature_slot": candidate_slot,
            "evidence_kind": candidate.get("evidence_kind"),
            "support_only": bool(candidate.get("support_only")),
            "source_refs": list(candidate.get("source_refs") or []),
        }
    )
    theme["should_publish"] = theme["user_visible"] and theme.get("feature_slot") not in {"support_only", "launch_support"}


def merge_theme_candidates(day_reports):
    themes = []
    for candidate in flatten_candidates(day_reports):
        best_index = None
        best_score = -1
        for index, theme in enumerate(themes):
            if not should_merge_candidate(theme, candidate):
                continue
            score = candidate_similarity(theme, candidate)
            if score > best_score:
                best_index = index
                best_score = score
        if best_index is None:
            themes.append(create_aggregate(candidate, len(themes) + 1))
        else:
            merge_candidate(themes[best_index], candidate)

    for theme in themes:
        theme["dates_seen"].sort()
        theme["source_refs"].sort()
        theme["evidence_kinds"] = [kind for kind in theme["evidence_kinds"] if kind]

    return themes


FEATURE_SLOT_PRIORITY = {
    "page_launch": 0,
    "menu_launch": 0,
    "button_action": 0,
    "feature_flow": 1,
    "query_filter": 2,
    "detail_display": 3,
    "visual_ux": 4,
    "bugfix": 5,
    "tech_improvement": 6,
    "launch_support": 7,
    "support_only": 8,
}


RECORD_KIND_PRIORITY = {
    "launch": 0,
    "enhancement": 1,
    "tech": 2,
    "bugfix": 3,
}


TECH_SLOTS = {"launch_support", "support_only", "tech_improvement"}


def slot_record_kind(slot):
    if slot in {"page_launch", "menu_launch", "button_action"}:
        return "launch"
    if slot == "tech_improvement":
        return "tech"
    if slot == "bugfix":
        return "bugfix"
    if slot in TECH_SLOTS:
        return "tech"
    return "enhancement"


def choose_record_kind(slots):
    kind_set = {slot_record_kind(slot) for slot in (slots or []) if slot}
    if "launch" in kind_set:
        return "launch"
    if kind_set == {"bugfix"}:
        return "bugfix"
    if kind_set == {"tech"}:
        return "tech"
    return "enhancement"


def choose_primary_slot(items):
    return sorted(
        items,
        key=lambda item: (
            FEATURE_SLOT_PRIORITY.get(item.get("feature_slot"), 99),
            item.get("delivery_date"),
        ),
    )[0]["feature_slot"]


def assign_effective_delivery_dates(items):
    launch_dates = sorted(
        {
            item["delivery_date"]
            for item in items
            if item.get("feature_slot") == "page_launch"
        }
    )
    assigned = []
    for item in items:
        assigned_item = dict(item)
        effective_date = item["delivery_date"]
        if item.get("feature_slot") == "launch_support":
            future_launch = next((date for date in launch_dates if date >= item["delivery_date"]), None)
            if future_launch:
                effective_date = future_launch
        assigned_item["_effective_delivery_date"] = effective_date
        assigned.append(assigned_item)
    return assigned


def create_day_record(domain_key, delivery_date, items, record_kind, suppressed_slots=None):
    primary = sorted(
        items,
        key=lambda item: (
            FEATURE_SLOT_PRIORITY.get(item.get("feature_slot"), 99),
            item.get("delivery_date"),
        ),
    )[0]
    merged_slots = sorted({item.get("feature_slot") for item in items if item.get("feature_slot")})
    return {
        "record_id": f"{domain_key}:{delivery_date}:{record_kind}",
        "theme_id": f"{domain_key}:{delivery_date}:{record_kind}",
        "domain_key": domain_key,
        "domain_title": primary.get("domain_title") or primary.get("theme_title"),
        "theme_title": primary.get("domain_title") or primary.get("theme_title"),
        "delivery_date": delivery_date,
        "record_kind": record_kind,
        "primary_slot": choose_primary_slot(items),
        "feature_slot": choose_primary_slot(items),
        "primary_family": choose_primary_slot(items),
        "dates_seen": sorted({date for item in items for date in item.get("dates_seen", [])}),
        "merged_slots": merged_slots,
        "anchor_candidates": sorted({anchor for item in items for anchor in item.get("anchor_candidates", []) if anchor}),
        "merge_terms": sorted({term for item in items for term in item.get("merge_terms", []) if term}),
        "source_theme_ids": sorted({item["theme_id"] for item in items}),
        "source_refs": sorted({ref for item in items for ref in item.get("source_refs", [])}),
        "evidence_kinds": sorted({kind for item in items for kind in item.get("evidence_kinds", []) if kind}),
        "user_visible": any(item.get("user_visible") for item in items),
        "support_only": all(item.get("support_only") for item in items),
        "should_publish": any(item.get("should_publish") for item in items),
        "suppressed_slots": sorted(set(suppressed_slots or [])),
        "title_source_tier": choose_best_title_tier([item.get("title_source_tier", "unknown") for item in items]),
    }


def has_publishable_product_identity(record):
    title = str(record.get("domain_title") or record.get("theme_title") or "").strip()
    if not title or is_generic_theme_title(title) or is_low_quality_title(title):
        return False
    if contains_cjk(title) and has_meaningful_cjk_title(title) and (has_strong_product_term([title]) or any(hint in title for hint in ANCHOR_HINT_WORDS)):
        return True

    for anchor in record.get("anchor_candidates") or []:
        text = str(anchor or "").strip()
        if not text or not contains_cjk(text):
            continue
        if has_meaningful_cjk_title(text) and (has_strong_product_term([text]) or any(hint in text for hint in ANCHOR_HINT_WORDS)):
            return True
    return False


def has_frontend_support_evidence(record):
    evidence_kinds = record.get("evidence_kinds") or []
    return any(str(kind).startswith("frontend") for kind in evidence_kinds)


def has_route_like_anchor(record):
    anchors = record.get("anchor_candidates") or []
    return any("/" in str(anchor or "") for anchor in anchors)


def is_low_quality_record_title(record):
    title = str(record.get("domain_title") or record.get("theme_title") or "").strip()
    if not title:
        return True
    if is_low_quality_title(title):
        return True
    if title.endswith(("详情", "列表")):
        return True
    if title.endswith(("姓名", "工号", "UID", "Id", "ID", "位置", "时间")):
        return True
    if contains_cjk(title) and len(title) <= 4:
        has_cjk_anchor = any(
            contains_cjk(str(anchor or "")) and any(hint in str(anchor or "") for hint in ANCHOR_HINT_WORDS)
            for anchor in (record.get("anchor_candidates") or [])
        )
        if not has_strong_product_term([title]) and not any(hint in title for hint in ANCHOR_HINT_WORDS) and not has_cjk_anchor:
            return True
    return False


def record_similarity_terms(record):
    values = [record.get("domain_title"), record.get("theme_title")]
    values.extend(record.get("anchor_candidates") or [])
    values.extend(record.get("merge_terms") or [])
    return set(extract_merge_terms(values))


def root_domain_key(record):
    key = str(record.get("domain_key") or "").strip("/")
    if not key:
        return ""
    return key.split("/")[0]


def should_merge_day_records(left, right):
    if left["delivery_date"] != right["delivery_date"]:
        return False
    if left.get("record_kind") != right.get("record_kind"):
        return False
    if left.get("domain_key") == right.get("domain_key"):
        return True
    if (left.get("domain_title") or "").strip() and (left.get("domain_title") or "").strip() == (right.get("domain_title") or "").strip():
        return True

    shared_terms = record_similarity_terms(left) & record_similarity_terms(right)
    shared_cjk = any(contains_cjk(term) for term in shared_terms)
    same_root = root_domain_key(left) and root_domain_key(left) == root_domain_key(right)
    one_low_quality = is_low_quality_record_title(left) or is_low_quality_record_title(right)

    if same_root and (one_low_quality or shared_terms):
        return True
    if shared_cjk and len(shared_terms) >= 2:
        return True
    if one_low_quality and shared_cjk:
        return True
    return False


def merge_record_payload(base, incoming):
    better_title = choose_theme_title(base.get("domain_title"), incoming.get("domain_title"))
    if better_title != base.get("domain_title"):
        base["domain_title"] = better_title
        base["theme_title"] = better_title
        base["domain_key"] = incoming.get("domain_key") or base.get("domain_key")
    for field in ("dates_seen", "merged_slots", "anchor_candidates", "merge_terms", "source_theme_ids", "source_refs", "evidence_kinds", "suppressed_slots"):
        base[field] = sorted(set(base.get(field) or []) | set(incoming.get(field) or []))
    base["user_visible"] = base.get("user_visible") or incoming.get("user_visible")
    base["support_only"] = base.get("support_only") and incoming.get("support_only")
    base["should_publish"] = base.get("should_publish") or incoming.get("should_publish")
    return base


def dedupe_same_day_records(records):
    deduped = []
    for record in sorted(records, key=lambda item: (item["delivery_date"], item.get("record_kind"), item.get("domain_key") or "")):
        merged = False
        for existing in deduped:
            if should_merge_day_records(existing, record):
                merge_record_payload(existing, record)
                merged = True
                break
        if not merged:
            deduped.append(record)
    return deduped


def dedupe_exact_title_records(records):
    deduped = {}
    for record in records:
        key = (
            record["delivery_date"],
            record.get("record_kind"),
            str(record.get("domain_title") or "").strip(),
        )
        if key in deduped:
            merge_record_payload(deduped[key], record)
        else:
            deduped[key] = record
    return list(deduped.values())


def absorb_low_quality_records_into_launch(records):
    grouped = {}
    for record in records:
        grouped.setdefault(record["delivery_date"], []).append(record)

    normalized = []
    for day_records in grouped.values():
        launches = [record for record in day_records if record.get("record_kind") == "launch"]
        retained = []
        for record in day_records:
            if record.get("record_kind") == "launch":
                retained.append(record)
                continue

            target = None
            if launches:
                for launch in launches:
                    if root_domain_key(launch) == root_domain_key(record) or (record_similarity_terms(launch) & record_similarity_terms(record)):
                        target = launch
                        break
            if target is not None:
                target["suppressed_slots"] = sorted(set(target.get("suppressed_slots") or []) | {record.get("primary_slot")})
                merge_record_payload(target, record)
                continue
            retained.append(record)
        normalized.extend(retained)
    return normalized


def suppress_low_quality_records(records):
    for record in records:
        if is_low_quality_record_title(record):
            record["should_publish"] = False
    return records


def apply_publishability(records):
    by_domain = {}
    for record in records:
        by_domain.setdefault(record["domain_key"], []).append(record)

    normalized = []
    for domain_records in by_domain.values():
        domain_records.sort(
            key=lambda item: (
                parse_date(item["delivery_date"]).toordinal(),
                RECORD_KIND_PRIORITY.get(item.get("record_kind"), 99),
            ),
        )
        for record in domain_records:
            if record.get("should_publish") or record.get("record_kind") != "tech":
                continue
            if not has_publishable_product_identity(record):
                continue
            record["should_publish"] = True
        normalized.extend(domain_records)
    return normalized


def normalize_domain_day_records(records):
    by_domain = {}
    for record in records:
        by_domain.setdefault(record["domain_key"], []).append(record)

    normalized = []
    for domain_records in by_domain.values():
        domain_records.sort(key=lambda item: item["delivery_date"])
        launches = [item for item in domain_records if item["record_kind"] == "launch"]
        normalized_records = []
        for record in domain_records:
            if record["record_kind"] != "enhancement":
                normalized_records.append(record)
                continue
            if record.get("primary_slot") == "feature_flow":
                prior_launches = [launch for launch in launches if launch["delivery_date"] <= record["delivery_date"]]
                if prior_launches:
                    target = prior_launches[-1]
                    target["dates_seen"] = sorted(set(target["dates_seen"]) | set(record.get("dates_seen") or []))
                    target["source_theme_ids"] = sorted(set(target["source_theme_ids"]) | set(record.get("source_theme_ids") or []))
                    target["source_refs"] = sorted(set(target["source_refs"]) | set(record.get("source_refs") or []))
                    target["merged_slots"] = sorted(set(target["merged_slots"]) | set(record.get("merged_slots") or []))
                    continue
            normalized_records.append(record)
        normalized.extend(normalized_records)

    normalized.sort(key=lambda item: item["delivery_date"], reverse=False)
    return normalized


def build_domain_day_records(themes, order):
    grouped = {}
    for theme in themes:
        domain_key = theme.get("domain_key") or ""
        if not domain_key and not (theme.get("domain_title") or theme.get("theme_title")):
            continue
        grouped.setdefault(domain_key, []).append(theme)

    records = []
    for domain_key, items in grouped.items():
        assigned_items = assign_effective_delivery_dates(items)
        day_groups = {}
        for item in assigned_items:
            day_groups.setdefault(item["_effective_delivery_date"], []).append(item)

        if not day_groups:
            continue

        for delivery_date, day_items in day_groups.items():
            launch_items = [item for item in day_items if slot_record_kind(item.get("feature_slot")) == "launch"]
            if launch_items:
                all_items_same_domain = []
                other_items = []
                for item in day_items:
                    is_same_domain = False
                    for launch in launch_items:
                        if root_domain_key(launch) == root_domain_key(item) or (record_similarity_terms(launch) & record_similarity_terms(item)):
                            is_same_domain = True
                            break
                    if is_same_domain:
                        all_items_same_domain.append(item)
                    else:
                        other_items.append(item)

                suppressed_slots = [
                    item.get("feature_slot")
                    for item in all_items_same_domain
                    if item.get("feature_slot") != "page_launch"
                ]
                if all_items_same_domain:
                    records.append(create_day_record(domain_key, delivery_date, all_items_same_domain, "launch", suppressed_slots))

                grouped_other = {"enhancement": [], "bugfix": [], "tech": []}
                for item in other_items:
                    grouped_other.setdefault(slot_record_kind(item.get("feature_slot")), []).append(item)

                for record_kind in ("enhancement", "bugfix", "tech"):
                    kind_items = grouped_other.get(record_kind) or []
                    if not kind_items:
                        continue
                    records.append(create_day_record(domain_key, delivery_date, kind_items, record_kind))
                continue

            grouped_items = {"enhancement": [], "bugfix": [], "tech": []}
            for item in day_items:
                grouped_items.setdefault(slot_record_kind(item.get("feature_slot")), []).append(item)

            for record_kind in ("enhancement", "bugfix", "tech"):
                kind_items = grouped_items.get(record_kind) or []
                if not kind_items:
                    continue
                records.append(create_day_record(domain_key, delivery_date, kind_items, record_kind))

    records = normalize_domain_day_records(records)
    records = dedupe_same_day_records(records)
    records = apply_publishability(records)
    records = suppress_low_quality_records(records)
    records = absorb_low_quality_records_into_launch(records)
    records = dedupe_exact_title_records(records)
    records.sort(key=lambda item: item["delivery_date"], reverse=(order == "desc"))
    return records


def build_day_reports(repos, since, until, window_days=None, active_days=None, include_context=False):
    reports = []
    _effective_window_days, windows = resolve_window_plan(since, until, window_days)
    resolved_active_days = list(active_days) if active_days is not None else collect_active_days(repos, since, until)
    for window_since, window_until, window_active_days in iter_window_active_days(resolved_active_days, windows):
        repo_results_by_day = {}
        for repo_name, repo_path in repos:
            repo_results_by_day[(repo_name, repo_path)] = analyze_repo_window(repo_name, repo_path, window_since, window_until)
        for day_str in window_active_days:
            day_report = {"date": day_str, "repos": []}
            for repo_name, repo_path in repos:
                result = repo_results_by_day[(repo_name, repo_path)].get(
                    day_str,
                    {
                        "name": repo_name,
                        "path": repo_path,
                        "main_ref": "",
                        "java_files": [],
                        "frontend_files": [],
                        "sql_files": [],
                        "file_diffs": {},
                        "file_meta": {},
                        "commit_msgs": [],
                        "commit_count": 0,
                        "topics": [],
                    },
                )
                candidates = build_repo_theme_candidates(result)
                day_report["repos"].append(
                    {
                        "name": result["name"],
                        "path": result["path"],
                        "main_ref": result["main_ref"],
                        "theme_candidates": candidates,
                        **(
                            {
                                "file_diffs": result.get("file_diffs", {}),
                                "file_commits": result.get("file_commits", {}),
                                "file_meta": result.get("file_meta", {}),
                                "commit_msgs": result.get("commit_msgs", []),
                                "commit_count": result.get("commit_count", 0),
                            }
                            if include_context
                            else {}
                        ),
                    }
                )
            reports.append(day_report)
    return reports


def build_repo_fingerprint(repos):
    digest = hashlib.sha256()
    for repo_name, repo_path in sorted((name, os.path.abspath(path)) for name, path in repos):
        digest.update(f"{repo_name}:{repo_path}\n".encode("utf-8"))
    return digest.hexdigest()


def build_ledger_from_day_reports(repos, notices, since, until, order, effective_window_days, windows, active_days, day_reports):
    calendar_day_count = (parse_date(until) - parse_date(since)).days + 1
    themes = merge_theme_candidates(day_reports)
    domain_day_records = build_domain_day_records(themes, order)
    return {
        "status": "ok",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "since": since,
        "until": until,
        "order": order,
        "calendar_day_count": calendar_day_count,
        "active_day_count": len(active_days),
        "window_days": effective_window_days,
        "window_count": len(windows),
        "repo_fingerprint": build_repo_fingerprint(repos),
        "repos": [
            {"name": repo_name, "path": os.path.abspath(repo_path)}
            for repo_name, repo_path in sorted((name, path) for name, path in repos)
        ],
        "notices": notices,
        "days": day_reports,
        "themes": themes,
        "domain_day_records": domain_day_records,
        "published_themes": domain_day_records,
    }


def build_ledger_payload(repos, notices, since, until, order, window_days=None):
    effective_window_days, windows = resolve_window_plan(since, until, window_days)
    active_days = collect_active_days(repos, since, until)
    day_reports = build_day_reports(repos, since, until, window_days=effective_window_days, active_days=active_days)
    return build_ledger_from_day_reports(
        repos,
        notices,
        since,
        until,
        order,
        effective_window_days,
        windows,
        active_days,
        day_reports,
    )


def build_context_payload(repos, notices, since, until, order, window_days=None):
    effective_window_days, windows = resolve_window_plan(since, until, window_days)
    active_days = collect_active_days(repos, since, until)
    day_reports = build_day_reports(
        repos,
        since,
        until,
        window_days=effective_window_days,
        active_days=active_days,
        include_context=True,
    )
    return build_ledger_from_day_reports(
        repos,
        notices,
        since,
        until,
        order,
        effective_window_days,
        windows,
        active_days,
        day_reports,
    )


def render_markdown(ledger):
    lines = ["# 主题账本草稿"]
    grouped = {}
    records = ledger.get("domain_day_records") or ledger.get("published_themes") or []
    for record in records:
        grouped.setdefault(record["delivery_date"], []).append(record)

    date_keys = sorted(grouped.keys(), reverse=(ledger["order"] == "desc"))
    for day in date_keys:
        lines.append("")
        lines.append(f"## {day}")
        lines.append("")
        for theme in grouped[day]:
            lines.append(
                f"- {theme['domain_title']} / {theme['primary_slot']}（类型：{theme['record_kind']}；归并日期：{', '.join(theme['dates_seen'])}；合并槽位：{', '.join(theme.get('merged_slots') or ['无'])}）"
            )
    return "\n".join(lines).strip() + "\n"


def build_args_namespace(args):
    return argparse.Namespace(
        repos=args.repos,
        repo_path=args.repo_path,
        repo_discovery_depth=args.repo_discovery_depth,
    )


def main():
    parser = argparse.ArgumentParser(description="按天归并主题候选，生成 changelog 主题账本。")
    parser.add_argument("--since", default=datetime.today().strftime("%Y-%m-%d"), help="开始日期；支持 YYYY-MM-DD、earliest、auto")
    parser.add_argument("--until", default=None, help="结束日期；默认与 --since 相同")
    parser.add_argument("--repo-path", default=None, help="单个仓库路径，或包含多个子仓库的项目目录")
    parser.add_argument("--repos", default=None, help="多仓库，格式: name:path,name:path")
    parser.add_argument("--repo-discovery-depth", type=int, default=None, help="自动发现子仓库的最大目录深度；默认不限制")
    parser.add_argument("--order", choices=["asc", "desc"], default="desc", help="主题账本输出顺序")
    parser.add_argument("--json-output", help="主题账本 JSON 输出路径")
    parser.add_argument("--markdown-output", help="主题账本 Markdown 输出路径")
    args = parser.parse_args()

    repos, notices, errors = resolve_repo_inputs(build_args_namespace(args))
    if errors:
        print("CHANGELOG_DRAFT_ERROR")
        for error in errors:
            print(f"- {error}")
        sys.exit(2)

    since, date_notices = resolve_since_value(args.since, repos)
    notices.extend(date_notices)
    until = args.until or since

    ledger = build_ledger_payload(repos, notices, since, until, args.order)
    json_text = json.dumps(ledger, ensure_ascii=False, indent=2)
    markdown_text = render_markdown(ledger)

    if args.json_output:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_output)) or ".", exist_ok=True)
        with open(args.json_output, "w", encoding="utf-8") as f:
            f.write(json_text + "\n")
    if args.markdown_output:
        os.makedirs(os.path.dirname(os.path.abspath(args.markdown_output)) or ".", exist_ok=True)
        with open(args.markdown_output, "w", encoding="utf-8") as f:
            f.write(markdown_text)

    if args.json_output or args.markdown_output:
        print("CHANGELOG_DRAFT_OK")
        if args.json_output:
            print(os.path.abspath(args.json_output))
        if args.markdown_output:
            print(os.path.abspath(args.markdown_output))
        return

    print(json_text)


if __name__ == "__main__":
    main()
