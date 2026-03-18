#!/usr/bin/env python3
"""
前端代码变更分析器

提供前端文件的变更分析功能：
- 路由文件识别
- 页面组件识别
- API 调用提取
"""

import re
import os
from typing import Any, Dict, List

from diff_evidence import build_compact_evidence, is_changed_diff_line

IGNORE_PATTERNS = [
    r"/e2e/",
    r"\.spec\.ts$",
    r"\.spec\.tsx$",
    r"\.test\.ts$",
    r"\.test\.tsx$",
    r"\.test\.js$",
    r"\.test\.jsx$",
    r"__tests__/",
    r"/mock/",
    r"/mocks/",
    r"\.mock\.",
    r"package\.json$",
    r"tsconfig\.json$",
    r"\.eslintrc\.",
    r"\.stylelintrc\.",
    r"webpack\.",
    r"vite\.config\.",
    r"\.config\.ts$",
    r"\.config\.js$",
    r"/locales/",
    r"typings\.d\.ts$",
]

PAGE_DIR_KEYWORDS = ["pages", "views", "screens"]
ROUTER_FILE_KEYWORDS = ["router", "routes", "App.tsx", "App.jsx"]
ROUTER_PATH_KEYWORDS = ["/router/", "/routers/", "/routes/", "config/routes/"]
PAGE_SUPPORT_DIR_KEYWORDS = {
    "components", "component", "common", "shared", "hooks", "utils", "config",
    "context", "store", "redux", "zustand", "recoil", "types", "models", "api", "services", "service", "map"
}


def normalize_path(file_path):
    return (file_path or "").replace("\\", "/")


def extract_page_tail_parts(file_path):
    path = normalize_path(file_path)
    match = re.search(r'/(pages|views|screens)/(.+)', path)
    if not match:
        return []
    return [p for p in match.group(2).split('/') if p]


def get_page_theme_root(file_path):
    parts = extract_page_tail_parts(file_path)
    if not parts:
        return ""

    collected = []
    for part in parts:
        if part.endswith((".tsx", ".ts", ".jsx", ".js", ".vue", ".css", ".scss", ".less", ".sass")):
            break
        if part.lower() in PAGE_SUPPORT_DIR_KEYWORDS:
            break
        collected.append(part)

    if not collected:
        return ""
    if len(collected) >= 2:
        return "/".join(collected[:2])
    return collected[0]


def is_page_entry_file(file_path):
    basename = os.path.basename(file_path).lower()
    if basename not in {"index.tsx", "index.ts", "index.jsx", "index.js", "index.vue"}:
        return False
    theme_root = get_page_theme_root(file_path)
    if not theme_root:
        return False
    parts = extract_page_tail_parts(file_path)
    return parts[:-1] == [p for p in theme_root.split('/') if p]


def is_ignored_file(file_path):
    """检查文件是否应该被忽略"""
    path_lower = file_path.lower()
    for pattern in IGNORE_PATTERNS:
        if re.search(pattern, path_lower, re.IGNORECASE):
            return True
    return False


def dedupe_keep_order(items):
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def clean_signal_values(items):
    cleaned = []
    for item in items or []:
        value = (item or "").strip()
        if not value:
            continue
        if re.fullmatch(r"[0-9.:%_-]+", value):
            continue
        if value.startswith(("./", "../", "http://", "https://")):
            continue
        if re.fullmatch(r"[a-z0-9_./-]+", value) and not re.search(r"[A-Z]", value):
            continue
        cleaned.append(value)
    return dedupe_keep_order(cleaned)


def get_page_area(file_path):
    path = normalize_path(file_path)
    match = re.search(r'/(pages|views|screens)/(.+)', path)
    if not match:
        return ""
    tail = match.group(2)
    parts = [p for p in tail.split('/') if p and not p.endswith(('.tsx', '.ts', '.jsx', '.js', '.vue'))]
    parts = [p for p in parts if p.lower() not in {"components", "component", "common", "hooks"}]
    formatted = [p.replace('-', ' ').replace('_', ' ').title() for p in parts[:3]]
    return " / ".join(formatted)


def classify_frontend_file(file_path):
    """判断前端文件的角色"""
    path_lower = file_path.lower()
    basename = os.path.basename(file_path).lower()

    page_root = get_page_theme_root(file_path)
    page_parts = extract_page_tail_parts(file_path)
    page_lower_parts = [part.lower() for part in page_parts]

    if not page_root and (
        any(k.lower() in basename for k in ROUTER_FILE_KEYWORDS)
        or any(keyword in path_lower for keyword in ROUTER_PATH_KEYWORDS)
    ):
        return "路由配置"

    if page_root:
        if is_page_entry_file(file_path):
            return "页面入口"
        if any(part in {"service", "services", "api"} for part in page_lower_parts) or basename.startswith(("service.", "api.")):
            return "页面数据层"
        if any(part in {"context", "store", "redux", "zustand", "recoil", "hooks"} for part in page_lower_parts):
            return "页面状态层"
        if any(part in {"utils", "helpers", "filters"} for part in page_lower_parts):
            return "页面工具层"
        if any(part in {"config", "constants", "models"} for part in page_lower_parts):
            return "页面配置层"
        if any(part in {"types"} for part in page_lower_parts) or basename.endswith(".d.ts"):
            return "页面类型定义"
        if any(part in {"components", "component", "common", "shared", "map", "detail"} for part in page_lower_parts):
            return "页面支撑组件"
        return "页面子视图"

    if file_path.endswith((".css", ".scss", ".less", ".sass")):
        return "样式文件"

    if any(k in path_lower for k in ["/api/", "/services/", "/requests/", "/http/"]):
        return "API 请求层"

    if any(k in path_lower for k in ["/store/", "/redux/", "/context/", "/zustand/", "/recoil/"]):
        return "状态管理"

    if basename.startswith("use") and basename.endswith((".ts", ".tsx", ".js", ".jsx")):
        return "自定义 Hook"

    if any(k in path_lower for k in ["/components/", "/ui/", "/common/", "/shared/"]):
        return "通用组件"

    return "前端文件"


def extract_frontend_entry_info(file_path, diff):
    """提取前端入口信息"""
    role = classify_frontend_file(file_path)
    info: Dict[str, Any] = {"role": role}

    if role == "路由配置":
        added = re.findall(r'^\+.*path[:\s]+["\']([^"\']+)["\']', diff or "", re.MULTILINE)
        removed = re.findall(r'^-.*path[:\s]+["\']([^"\']+)["\']', diff or "", re.MULTILINE)
        info["added_routes"] = [r for r in added if not r.startswith("//")]
        info["removed_routes"] = [r for r in removed if not r.startswith("//")]

    elif role == "API 请求层":
        added = re.findall(
            r'^\+.*(?:get|post|put|delete|patch)\s*\(["\']([^"\']+)["\']',
            diff or "", re.MULTILINE | re.IGNORECASE
        )
        removed = re.findall(
            r'^-.*(?:get|post|put|delete|patch)\s*\(["\']([^"\']+)["\']',
            diff or "", re.MULTILINE | re.IGNORECASE
        )
        info["added_apis"] = added
        info["removed_apis"] = removed

    return info


def extract_frontend_product_signals(file_path, diff):
    role = classify_frontend_file(file_path)
    text = diff or ""

    added_labels = clean_signal_values(re.findall(r'^\+.*?["\']([\u4e00-\u9fffA-Za-z0-9][^"\'\n]{1,30})["\']', text, re.MULTILINE))
    removed_labels = clean_signal_values(re.findall(r'^-.*?["\']([\u4e00-\u9fffA-Za-z0-9][^"\'\n]{1,30})["\']', text, re.MULTILINE))
    added_text_nodes = clean_signal_values(re.findall(r'^\+.*?>\s*([\u4e00-\u9fffA-Za-z0-9][^<\n]{1,30})\s*<', text, re.MULTILINE))
    removed_text_nodes = clean_signal_values(re.findall(r'^-.*?>\s*([\u4e00-\u9fffA-Za-z0-9][^<\n]{1,30})\s*<', text, re.MULTILINE))
    added_labels = clean_signal_values(added_labels + added_text_nodes)
    removed_labels = clean_signal_values(removed_labels + removed_text_nodes)
    added_columns = clean_signal_values(re.findall(r'^\+.*?(?:title|label|name)\s*[:=]\s*["\']([^"\'\n]{1,30})["\']', text, re.MULTILINE))
    removed_columns = clean_signal_values(re.findall(r'^-.*?(?:title|label|name)\s*[:=]\s*["\']([^"\'\n]{1,30})["\']', text, re.MULTILINE))
    added_filters = clean_signal_values(re.findall(r'^\+.*?(?:placeholder|filter|RangeSlider|Select|Checkbox|Radio).*?["\']([^"\'\n]{1,30})["\']', text, re.MULTILINE))
    removed_filters = clean_signal_values(re.findall(r'^-.*?(?:placeholder|filter|RangeSlider|Select|Checkbox|Radio).*?["\']([^"\'\n]{1,30})["\']', text, re.MULTILINE))
    added_paths = dedupe_keep_order(re.findall(r'^\+.*?path\s*[:=]\s*["\']([^"\'\n]+)["\']', text, re.MULTILINE))
    removed_paths = dedupe_keep_order(re.findall(r'^-.*?path\s*[:=]\s*["\']([^"\'\n]+)["\']', text, re.MULTILINE))
    added_api_names = dedupe_keep_order(re.findall(r'^\+.*?(fetch\w+|get\w+|query\w+|load\w+)\s*\(', text, re.MULTILINE))
    removed_api_names = dedupe_keep_order(re.findall(r'^-.*?(fetch\w+|get\w+|query\w+|load\w+)\s*\(', text, re.MULTILINE))

    role_hints: List[str] = []
    path_lower = file_path.lower()
    page_area = get_page_area(file_path)
    if role == "页面入口":
        if page_area:
            role_hints.append(f"{page_area} 页面入口或主体结构可能变化")
        else:
            role_hints.append("页面入口或主体结构可能变化")
    elif role.startswith("页面"):
        theme_root = get_page_theme_root(file_path)
        if theme_root:
            role_hints.append(f"{theme_root} 主题下的支撑能力可能变化，默认应并入同一页面主题")
        else:
            role_hints.append("页面支撑能力可能变化，默认应并入同一页面主题")
    if any(k in path_lower for k in ["/context/", "/store/", "/redux/", "/zustand/", "/recoil/"]):
        role_hints.append("筛选条件、状态联动或权限展示可能变化")
    if any(k in path_lower for k in ["/service", "/api/"]):
        role_hints.append("前端可调用的数据接口可能变化")

    return {
        "page_area": page_area,
        "added_labels": added_labels[:8],
        "removed_labels": removed_labels[:8],
        "added_paths": added_paths[:6],
        "removed_paths": removed_paths[:6],
        "added_columns": added_columns[:6],
        "removed_columns": removed_columns[:6],
        "added_filters": added_filters[:6],
        "removed_filters": removed_filters[:6],
        "added_api_names": added_api_names[:6],
        "removed_api_names": removed_api_names[:6],
        "role_hints": role_hints,
    }


def build_frontend_evidence_matcher(entry_info, product_signals):
    keywords = set()

    for items in [
        entry_info.get("added_routes"),
        entry_info.get("removed_routes"),
        entry_info.get("added_apis"),
        entry_info.get("removed_apis"),
        product_signals.get("added_labels"),
        product_signals.get("removed_labels"),
        product_signals.get("added_paths"),
        product_signals.get("removed_paths"),
        product_signals.get("added_columns"),
        product_signals.get("removed_columns"),
        product_signals.get("added_filters"),
        product_signals.get("removed_filters"),
        product_signals.get("added_api_names"),
        product_signals.get("removed_api_names"),
    ]:
        for item in items or []:
            cleaned = item.strip()
            if 1 < len(cleaned) <= 80:
                keywords.add(cleaned)

    def matcher(line):
        if not is_changed_diff_line(line):
            return False

        stripped = line[1:].strip()
        if any(keyword in stripped for keyword in keywords):
            return True
        if re.search(r'\b(path|title|label|name|placeholder|columns?|filters?|options?)\b', stripped):
            return True
        if re.search(r'\b(get|post|put|delete|patch)\s*\(', stripped, re.IGNORECASE):
            return True
        if re.search(r'<(Button|Table|Form|Select|Modal|Tabs|DatePicker|Checkbox|Radio)', stripped):
            return True
        return False

    return matcher


def inspect_frontend_file(file_path, diff, compact=False, exists_in_worktree=True):
    role = classify_frontend_file(file_path)
    entry_info = extract_frontend_entry_info(file_path, diff)
    product_signals = extract_frontend_product_signals(file_path, diff)

    evidence = diff
    if compact and diff:
        evidence = build_compact_evidence(
            diff,
            build_frontend_evidence_matcher(entry_info, product_signals),
        )

    return {
        "file_path": file_path,
        "role": role,
        "entry_info": entry_info,
        "product_signals": product_signals,
        "evidence": evidence,
        "exists_in_worktree": exists_in_worktree,
    }


def format_frontend_file(file_path, diff, compact=False, exists_in_worktree=True):
    """格式化单个前端文件的分析结果"""
    inspection = inspect_frontend_file(
        file_path,
        diff,
        compact=compact,
        exists_in_worktree=exists_in_worktree,
    )
    role = inspection["role"]
    entry_info = inspection["entry_info"]
    product_signals = inspection["product_signals"]
    evidence = inspection["evidence"]

    lines = []
    lines.append(f"\n{'='*60}")
    lines.append(f"[前端] {file_path}  [{role}]")
    lines.append(f"{'='*60}")
    if not inspection["exists_in_worktree"]:
        lines.append("\n▶ 文件状态：本次分析范围内已删除或迁出当前工作区")

    added_routes = entry_info.get("added_routes") or []
    removed_routes = entry_info.get("removed_routes") or []
    added_apis = entry_info.get("added_apis") or []
    removed_apis = entry_info.get("removed_apis") or []

    if role == "路由配置":
        if added_routes:
            lines.append(f"\n▶ 新增路由：{', '.join(added_routes)}")
        if removed_routes:
            lines.append(f"\n▶ 删除路由：{', '.join(removed_routes)}")

    elif role == "API 请求层":
        if added_apis:
            lines.append(f"\n▶ 新增 API 调用：{', '.join(added_apis[:5])}")
        if removed_apis:
            lines.append(f"\n▶ 删除 API 调用：{', '.join(removed_apis[:5])}")
        lines.append("▶ 默认作为页面或流程的支撑证据，不要单独写成主功能")

    elif role == "页面入口":
        lines.append(f"\n▶ 页面入口文件（优先作为独立主题主证据）")

    elif role.startswith("页面"):
        lines.append(f"\n▶ 页面支撑文件（默认应并入同一页面主题，不要单独写成主功能）")

    elif role in {"通用组件", "状态管理", "自定义 Hook", "前端文件", "样式文件"}:
        lines.append(f"\n▶ 通用或支撑文件（只有能明确映射到独立主题时才用于补充，不要单独写成主功能）")

    signal_lines = []
    if product_signals.get("added_paths"):
        signal_lines.append(f"- 页面/路由线索：{', '.join(product_signals['added_paths'])}")
    if product_signals.get("removed_paths"):
        signal_lines.append(f"- 调整/下线路由线索：{', '.join(product_signals['removed_paths'])}")
    if product_signals.get("added_labels"):
        signal_lines.append(f"- 新增文案/标题线索：{', '.join(product_signals['added_labels'])}")
    if product_signals.get("removed_labels"):
        signal_lines.append(f"- 调整/下线文案线索：{', '.join(product_signals['removed_labels'])}")
    if product_signals.get("added_columns"):
        signal_lines.append(f"- 字段/列展示线索：{', '.join(product_signals['added_columns'])}")
    if product_signals.get("removed_columns"):
        signal_lines.append(f"- 调整/下线字段线索：{', '.join(product_signals['removed_columns'])}")
    if product_signals.get("added_filters"):
        signal_lines.append(f"- 筛选条件线索：{', '.join(product_signals['added_filters'])}")
    if product_signals.get("removed_filters"):
        signal_lines.append(f"- 调整/下线筛选线索：{', '.join(product_signals['removed_filters'])}")
    if product_signals.get("added_api_names"):
        signal_lines.append(f"- 数据请求线索：{', '.join(product_signals['added_api_names'])}")
    if product_signals.get("removed_api_names"):
        signal_lines.append(f"- 调整/下线请求线索：{', '.join(product_signals['removed_api_names'])}")
    if product_signals.get("role_hints"):
        signal_lines.extend([f"- {hint}" for hint in product_signals["role_hints"]])

    if signal_lines:
        lines.append(f"\n▶ 产品信号：")
        lines.extend(signal_lines)

    lines.append(f"\n[{'关键证据' if compact else 'Diff'}]")
    lines.append(evidence if evidence else "（无法获取 diff）")

    return "\n".join(lines)


def is_router_file(file_path):
    """检查是否是路由文件"""
    path_lower = normalize_path(file_path).lower()
    basename = os.path.basename(path_lower)
    if get_page_theme_root(file_path):
        return False
    return any(k.lower() in basename for k in ROUTER_FILE_KEYWORDS) or any(keyword in path_lower for keyword in ROUTER_PATH_KEYWORDS)


def is_page_file(file_path):
    """检查是否是页面级组件"""
    path_lower = file_path.lower()
    return any(f"/{k}/" in path_lower for k in PAGE_DIR_KEYWORDS)
