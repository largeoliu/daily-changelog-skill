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

IGNORE_PATTERNS = [
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
    r"webpack\.",
    r"vite\.config\.",
    r"\.config\.ts$",
    r"\.config\.js$",
]

PAGE_DIR_KEYWORDS = ["pages", "views", "screens"]
ROUTER_FILE_KEYWORDS = ["router", "routes", "App.tsx", "App.jsx", "index.tsx", "index.jsx"]


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


def get_page_area(file_path):
    path = file_path.replace('\\', '/')
    match = re.search(r'/(pages|views|screens)/(.+)', path)
    if not match:
        return ""
    tail = match.group(2)
    parts = [p for p in tail.split('/') if p and not p.endswith(('.tsx', '.ts', '.jsx', '.js', '.vue'))]
    parts = [p for p in parts if p.lower() not in {"components", "component", "common", "hooks"}]
    formatted = [p.replace('-', ' ').replace('_', ' ').title() for p in parts[:3]]
    return " / ".join(formatted)


def find_frontend_root():
    """查找前端项目根目录"""
    import subprocess
    
    def run_cmd(cmd):
        try:
            return subprocess.check_output(
                cmd, shell=True, stderr=subprocess.DEVNULL
            ).decode("utf-8").strip()
        except subprocess.CalledProcessError:
            return ""
    
    result = run_cmd(
        'find . -name "package.json" -maxdepth 3 '
        '| grep -v "node_modules" | head -5'
    ).split("\n")
    for r in result:
        r = r.strip()
        if r:
            return os.path.dirname(r)
    return "."


def classify_frontend_file(file_path):
    """判断前端文件的角色"""
    path_lower = file_path.lower()
    basename = os.path.basename(file_path).lower()

    if any(k.lower() in basename for k in ROUTER_FILE_KEYWORDS):
        return "路由配置"

    if any(f"/{k}/" in path_lower for k in PAGE_DIR_KEYWORDS):
        return "页面组件"

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

    titles = dedupe_keep_order(re.findall(r'^\+.*?["\']([\u4e00-\u9fffA-Za-z0-9][^"\'\n]{1,30})["\']', text, re.MULTILINE))
    button_labels = [s for s in titles if any(k in s for k in ["查询", "提交", "导出", "保存", "定位", "更多", "收起", "筛选", "确认"])]
    metric_labels = [s for s in titles if any(k in s for k in ["率", "数", "统计", "分析", "趋势", "概览", "效率", "转化", "利用"])]

    added_columns = dedupe_keep_order(re.findall(r'^\+.*?(?:title|label|name)\s*[:=]\s*["\']([^"\'\n]{1,30})["\']', text, re.MULTILINE))
    added_filters = dedupe_keep_order(re.findall(r'^\+.*?(?:placeholder|filter|RangeSlider|Select|Checkbox|Radio).*?["\']([^"\'\n]{1,30})["\']', text, re.MULTILINE))
    added_paths = dedupe_keep_order(re.findall(r'^\+.*?path\s*[:=]\s*["\']([^"\'\n]+)["\']', text, re.MULTILINE))
    added_api_names = dedupe_keep_order(re.findall(r'^\+.*?(fetch\w+|get\w+|query\w+|load\w+)\s*\(', text, re.MULTILINE))

    role_hints: List[str] = []
    path_lower = file_path.lower()
    page_area = get_page_area(file_path)
    if role == "页面组件":
        if page_area:
            role_hints.append(f"{page_area} 页面结构或操作入口可能变化")
        else:
            role_hints.append("页面结构或操作入口可能变化")
    if any(k in path_lower for k in ["/context/", "/store/", "/redux/"]):
        role_hints.append("筛选条件、状态联动或权限展示可能变化")
    if any(k in path_lower for k in ["/service", "/api/"]):
        role_hints.append("前端可调用的数据接口可能变化")

    business_hints: List[str] = []
    joined_diff = text.lower()
    if "查询" in text and any(k in text for k in ["筛选", "range", "select", "filter"]):
        business_hints.append("页面新增或优化了筛选查询能力")
    if any(k in text for k in ["转化率", "利用率", "效率", "趋势", "概览", "统计", "分析", "指标"]):
        business_hints.append("页面新增了指标展示或统计分析内容")

    return {
        "page_area": page_area,
        "added_paths": added_paths[:6],
        "button_labels": button_labels[:6],
        "metric_labels": metric_labels[:6],
        "added_columns": added_columns[:6],
        "added_filters": added_filters[:6],
        "added_api_names": added_api_names[:6],
        "role_hints": role_hints,
        "business_hints": business_hints,
    }


def format_frontend_file(file_path, diff):
    """格式化单个前端文件的分析结果"""
    role = classify_frontend_file(file_path)
    entry_info = extract_frontend_entry_info(file_path, diff)
    product_signals = extract_frontend_product_signals(file_path, diff)

    lines = []
    lines.append(f"\n{'='*60}")
    lines.append(f"[前端] {file_path}  [{role}]")
    lines.append(f"{'='*60}")

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

    elif role == "页面组件":
        lines.append(f"\n▶ 页面级组件（直接影响用户界面）")

    signal_lines = []
    if product_signals.get("added_paths"):
        signal_lines.append(f"- 页面/路由线索：{', '.join(product_signals['added_paths'])}")
    if product_signals.get("button_labels"):
        signal_lines.append(f"- 操作按钮线索：{', '.join(product_signals['button_labels'])}")
    if product_signals.get("metric_labels"):
        signal_lines.append(f"- 指标展示线索：{', '.join(product_signals['metric_labels'])}")
    if product_signals.get("added_columns"):
        signal_lines.append(f"- 字段/列展示线索：{', '.join(product_signals['added_columns'])}")
    if product_signals.get("added_filters"):
        signal_lines.append(f"- 筛选条件线索：{', '.join(product_signals['added_filters'])}")
    if product_signals.get("added_api_names"):
        signal_lines.append(f"- 数据请求线索：{', '.join(product_signals['added_api_names'])}")
    if product_signals.get("role_hints"):
        signal_lines.extend([f"- {hint}" for hint in product_signals["role_hints"]])
    if product_signals.get("business_hints"):
        signal_lines.extend([f"- 业务影响：{hint}" for hint in product_signals["business_hints"]])

    if signal_lines:
        lines.append(f"\n▶ 产品信号：")
        lines.extend(signal_lines)

    lines.append(f"\n[Diff]")
    lines.append(diff if diff else "（无法获取 diff）")

    return "\n".join(lines)


def is_router_file(file_path):
    """检查是否是路由文件"""
    basename = os.path.basename(file_path).lower()
    return any(k.lower() in basename for k in ROUTER_FILE_KEYWORDS)


def is_page_file(file_path):
    """检查是否是页面级组件"""
    path_lower = file_path.lower()
    return any(f"/{k}/" in path_lower for k in PAGE_DIR_KEYWORDS)
