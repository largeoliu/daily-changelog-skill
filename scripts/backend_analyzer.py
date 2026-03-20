#!/usr/bin/env python3
"""
后端代码变更分析器

提供后端文件的变更分析功能：
- 入口层识别（Controller / DubboService / Scheduled / MQ）
- 路由提取
- Breaking Change 检测
- SQL 迁移文件检测
"""

import re
import os

from diff_evidence import build_compact_evidence, is_changed_diff_line

GENERIC_FIELD_NAMES = {"serialversionuid"}
GENERIC_KEYWORDS = {"service", "controller", "repository", "mapper", "impl", "dto", "vo", "query", "request", "response", "model", "entity", "domain", "common", "config"}
NOISY_LABEL_PATTERNS = [r"json_extract", r"select\s", r"insert\s", r"update\s", r"delete\s", r"\$\.%,", r"^\[.*事件\]", r"log\."]
SCENE_STOP_WORDS = {"application", "admin", "api", "domain", "service", "impl", "controller", "repository", "query", "event", "listener", "common", "core", "java", "src", "main", "cn", "com"}

IGNORE_PATTERNS = [
    r"/test/",
    r"Test\.java$",
    r"\.test\.java$",
    r"Tests\.java$",
    r"Spec\.java$",
    r"application\.yml$",
    r"application\.yaml$",
    r"application\.properties$",
    r"bootstrap\.yml$",
    r"bootstrap\.properties$",
    r"application-.*\.yml$",
    r"application-.*\.properties$",
    r"pom\.xml$",
    r"build\.gradle$",
    r"settings\.gradle$",
    r"/resources/static/",
    r"/resources/public/",
]

SQL_MIGRATION_DIRS = ["db", "database", "migration", "sql", "schema", "dml", "ddl", "alter", "changes", "updates"]


def is_ignored_file(file_path):
    """检查文件是否应该被忽略"""
    path_lower = file_path.lower()
    for pattern in IGNORE_PATTERNS:
        if re.search(pattern, path_lower, re.IGNORECASE):
            return True
    return False


def is_sql_migration_file(file_path):
    """检查是否是 SQL 迁移文件"""
    if not file_path.endswith((".sql", ".ddl", ".mysql", ".postgres", ".psql", ".dml", ".ddl")):
        return False
    path_lower = file_path.lower()
    return any(d in path_lower for d in SQL_MIGRATION_DIRS)

# 后端入口注解及其产品含义
BACKEND_ENTRY_ANNOTATIONS = {
    "@RestController": "HTTP 接口层",
    "@Controller":     "HTTP 接口层",
    "@GetMapping":     "GET 接口",
    "@PostMapping":    "POST 接口",
    "@PutMapping":     "PUT 接口",
    "@DeleteMapping":  "DELETE 接口",
    "@PatchMapping":   "PATCH 接口",
    "@RequestMapping": "HTTP 接口",
    "@DubboService":   "Dubbo RPC 服务",
    "@com.alibaba.dubbo.config.annotation.Service": "Dubbo RPC 服务",
    "@org.apache.dubbo.config.annotation.Service":  "Dubbo RPC 服务",
    "@DubboReference": "Dubbo RPC 调用方",
    "@Scheduled":      "定时任务",
    "@KafkaListener":  "Kafka 消费者",
    "@RabbitListener": "RabbitMQ 消费者",
    "@SqsListener":    "SQS 消费者",
    "@JmsListener":    "JMS 消费者",
    "@EventListener":  "Spring 事件监听",
}

DUBBO_INTERFACE_PATTERNS = [
    r"/api/",
    r"/facade/",
    r"/rpc/",
    r"Service\.java$",
]

VALIDATION_ANNOTATIONS = {
    "@Valid": "级联校验",
    "@NotNull": "非空校验",
    "@NotBlank": "非空字符串校验",
    "@NotEmpty": "非空集合校验",
    "@Size": "大小范围校验",
    "@Min": "最小值校验",
    "@Max": "最大值校验",
    "@Pattern": "正则表达式校验",
    "@Email": "邮箱格式校验",
    "@DecimalMin": "最小值校验（Decimal）",
    "@DecimalMax": "最大值校验（Decimal）",
    "@Positive": "正数校验",
    "@PositiveOrZero": "非负数校验",
    "@Negative": "负数校验",
    "@NegativeOrZero": "非正数校验",
    "@Past": "过去时间校验",
    "@Future": "未来时间校验",
    "@PastOrPresent": "过去或现在时间校验",
    "@FutureOrPresent": "未来或现在时间校验",
}

def dedupe_keep_order(items):
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def parse_added_identifiers(diff, pattern):
    matches = re.findall(pattern, diff, re.MULTILINE)
    return dedupe_keep_order([m.strip() for m in matches if m and m.strip()])


def to_cn_scene_name(token):
    return token


def extract_scene_anchor(file_path, class_name, routes=None):
    routes = routes or []
    if routes:
        first_route = routes[0].strip("/")
        if first_route:
            parts = [to_cn_scene_name(p) for p in first_route.split("/") if p and p not in {"api", "v1", "v2", "fix", "mq"}]
            parts = [p for p in parts if p]
            if parts and any(re.search(r"[\u4e00-\u9fff]", part) for part in parts):
                return " / ".join(parts[:3]) + " 接口"

    candidates = []
    for part in re.findall(r"[A-Z][a-z]+|[a-z]+", class_name):
        token = part.strip()
        if len(token) < 4:
            continue
        lower = token.lower()
        if lower in SCENE_STOP_WORDS:
            continue
        if lower.endswith(("service", "controller", "listener", "handler", "impl", "query", "dto", "vo", "event")):
            continue
        candidates.append(to_cn_scene_name(token))
    candidates = [c for c in dedupe_keep_order(candidates) if c]
    if candidates and any(re.search(r"[\u4e00-\u9fff]", candidate) for candidate in candidates):
        return " / ".join(candidates[:3]) + " 模块"
    return ""


def extract_product_signals(file_path, class_name, diff):
    """从后端 diff 中提取尽量客观的结构化信号。"""
    if not diff:
        return {}

    added_fields = parse_added_identifiers(
        diff,
        r'^\+\s*(?:private|protected|public)\s+(?!static\b)(?:final\s+)?[\w<>\[\], ?]+\s+(\w+)\s*(?:=|;)'
    )
    removed_fields = parse_added_identifiers(
        diff,
        r'^-\s*(?:private|protected|public)\s+(?!static\b)(?:final\s+)?[\w<>\[\], ?]+\s+(\w+)\s*(?:=|;)'
    )
    added_fields = [f for f in added_fields if f.lower() not in GENERIC_FIELD_NAMES]
    removed_fields = [f for f in removed_fields if f.lower() not in GENERIC_FIELD_NAMES]

    added_enum_values = parse_added_identifiers(
        diff,
        r'^\+\s*([A-Z][A-Z0-9_]+)\s*(?:\(|,|;)'
    )
    removed_enum_values = parse_added_identifiers(
        diff,
        r'^-\s*([A-Z][A-Z0-9_]+)\s*(?:\(|,|;)'
    )

    string_literals = parse_added_identifiers(
        diff,
        r'^\+.*?["\']([^"\'\n]{2,50})["\']'
    )
    labels = [s for s in string_literals if re.search(r'[\u4e00-\u9fffA-Za-z]', s)]
    labels = [s for s in labels if not s.startswith("/") and "{" not in s and "." not in s[:3]]
    labels = [s for s in labels if not any(re.search(pattern, s, re.IGNORECASE) for pattern in NOISY_LABEL_PATTERNS)]

    path_lower = file_path.lower()
    role_hints = []
    if any(k in path_lower for k in ["/dto/", "/vo/", "/response/", "/request/"]):
        role_hints.append("返回字段/请求参数可能变化")
    if any(k in path_lower for k in ["/query/", "/repository/"]):
        role_hints.append("查询条件或统计口径可能变化")
    if any(k in path_lower for k in ["/enum/", "/constant/"]):
        role_hints.append("业务类型或状态枚举可能变化")

    return {
        "added_fields": dedupe_keep_order(added_fields[:8]),
        "removed_fields": dedupe_keep_order(removed_fields[:8]),
        "added_enum_values": dedupe_keep_order(added_enum_values[:8]),
        "removed_enum_values": dedupe_keep_order(removed_enum_values[:8]),
        "labels": dedupe_keep_order(labels[:8]),
        "role_hints": dedupe_keep_order(role_hints),
    }


def extract_backend_entry_info(file_path, content):
    """从文件内容提取入口信息"""
    found_labels = set()
    for ann, label in BACKEND_ENTRY_ANNOTATIONS.items():
        if ann in content:
            found_labels.add(label)

    is_interface = bool(re.search(r'\binterface\s+\w+', content))
    is_dubbo_interface = is_interface and any(
        re.search(p, file_path) for p in DUBBO_INTERFACE_PATTERNS
    )
    if is_dubbo_interface:
        found_labels.add("Dubbo 接口定义（RPC 契约）")

    if not found_labels:
        return None

    http_routes = re.findall(
        r'@(?:Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']',
        content
    )

    dubbo_interfaces = re.findall(r'implements\s+([\w,\s]+?)(?:\{|extends)', content)
    dubbo_interfaces = [i.strip() for group in dubbo_interfaces for i in group.split(",") if i.strip()]

    return {
        "is_entry": True,
        "entry_type": ", ".join(sorted(found_labels)),
        "http_routes": http_routes,
        "dubbo_interfaces": dubbo_interfaces if "Dubbo" in str(found_labels) else [],
        "is_dubbo_interface": is_dubbo_interface,
    }


def analyze_diff_entry_changes(diff):
    """从 diff 识别入口层的增删改"""
    added_routes = re.findall(
        r'^\+.*@(?:Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']',
        diff, re.MULTILINE
    )
    removed_routes = re.findall(
        r'^-.*@(?:Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']',
        diff, re.MULTILINE
    )

    added_methods = re.findall(
        r'^\+\s+(?:public|protected)\s+\S+\s+(\w+)\s*\(', diff, re.MULTILINE
    )
    removed_methods = re.findall(
        r'^-\s+(?:public|protected)\s+\S+\s+(\w+)\s*\(', diff, re.MULTILINE
    )

    skip = {"get", "set", "toString", "hashCode", "equals", "build"}
    added_methods  = [m for m in added_methods  if m not in skip]
    removed_methods = [m for m in removed_methods if m not in skip]

    breaking_changes = []

    method_changes = re.findall(
        r'^-.*?(?:public|protected)\s+\S+\s+(\w+)\s*\([^)]*\).*\n'
        r'^\+.*?(?:public|protected)\s+\S+\s+\1\s*\([^)]*\)',
        diff, re.MULTILINE
    )
    if method_changes:
        breaking_changes.append("方法签名变更（请检查参数或返回类型变化）")

    validation_changes = []
    for ann in VALIDATION_ANNOTATIONS.keys():
        added_val = re.findall(r'^\+.*?' + re.escape(ann), diff, re.MULTILINE)
        removed_val = re.findall(r'^-.*?' + re.escape(ann), diff, re.MULTILINE)
        if added_val:
            validation_changes.append(f"+ {ann}")
        if removed_val:
            validation_changes.append(f"- {ann}")

    return {
        "added_routes":   added_routes,
        "removed_routes": removed_routes,
        "added_methods":  added_methods,
        "removed_methods": removed_methods,
        "breaking_changes": breaking_changes,
        "validation_changes": validation_changes,
    }


def build_backend_evidence_matcher(entry_info, changes, product_signals):
    keywords = set()

    def add_keywords(items):
        for item in items or []:
            cleaned = item.split(" ", 1)[-1] if item.startswith(("+ ", "- ")) else item
            cleaned = cleaned.strip()
            if 1 < len(cleaned) <= 80:
                keywords.add(cleaned)

    if entry_info:
        add_keywords(entry_info.get("http_routes"))
        add_keywords(entry_info.get("dubbo_interfaces"))

    add_keywords(changes.get("added_routes"))
    add_keywords(changes.get("removed_routes"))
    add_keywords(changes.get("added_methods"))
    add_keywords(changes.get("removed_methods"))
    add_keywords(changes.get("validation_changes"))
    add_keywords(product_signals.get("added_fields"))
    add_keywords(product_signals.get("removed_fields"))
    add_keywords(product_signals.get("added_enum_values"))
    add_keywords(product_signals.get("removed_enum_values"))
    add_keywords(product_signals.get("labels"))

    annotation_keywords = list(BACKEND_ENTRY_ANNOTATIONS.keys()) + list(VALIDATION_ANNOTATIONS.keys())

    def matcher(line):
        if not is_changed_diff_line(line):
            return False

        stripped = line[1:].strip()
        if any(keyword in stripped for keyword in keywords):
            return True
        if any(annotation in stripped for annotation in annotation_keywords):
            return True
        if re.search(r'\b(public|protected)\b.*\(', stripped):
            return True
        if any(token in stripped for token in ["@Operation", "@Parameter", "summary =", "description ="]):
            return True
        return False

    return matcher


def inspect_backend_file(file_path, diff, repo_path=None, content=None, compact=False, exists_in_worktree=True):
    if content is None:
        if repo_path:
            full_path = os.path.join(repo_path, file_path)
        else:
            full_path = file_path

        if os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        else:
            content = ""

    class_name = os.path.basename(file_path).replace(".java", "")
    is_interface = bool(re.search(r'\binterface\s+' + class_name, content))

    if is_interface:
        role = "接口定义"
    elif re.search(r'@(Rest)?Controller', content):
        role = "Controller（入口层）"
    elif "Service" in class_name and not "Impl" in class_name:
        role = "Service 接口"
    elif "ServiceImpl" in class_name or ("Service" in class_name and "Impl" in class_name):
        role = "Service 实现"
    elif any(k in class_name for k in ["Repository", "Mapper", "Dao"]):
        role = "数据访问层"
    elif any(k in class_name for k in ["DTO", "VO", "Request", "Response", "Command", "Query"]):
        role = "数据传输对象"
    elif any(k in class_name for k in ["Entity", "Domain", "Model"]):
        role = "业务实体"
    elif "Config" in class_name or "Configuration" in class_name:
        role = "配置类（含功能开关/灰度配置）"
    else:
        role = "类"

    entry_info = extract_backend_entry_info(file_path, content)
    product_signals = extract_product_signals(file_path, class_name, diff)
    changes = analyze_diff_entry_changes(diff) if diff else {
        "added_routes": [],
        "removed_routes": [],
        "added_methods": [],
        "removed_methods": [],
        "breaking_changes": [],
        "validation_changes": [],
    }
    routes = []
    if entry_info:
        routes.extend(entry_info.get("http_routes") or [])
    routes.extend(changes.get("added_routes") or [])
    routes.extend(changes.get("removed_routes") or [])
    scene_anchor = extract_scene_anchor(file_path, class_name, dedupe_keep_order(routes))

    evidence = diff
    if compact and diff:
        evidence = build_compact_evidence(
            diff,
            build_backend_evidence_matcher(entry_info, changes, product_signals),
        )

    return {
        "file_path": file_path,
        "class_name": class_name,
        "role": role,
        "entry_info": entry_info,
        "changes": changes,
        "product_signals": product_signals,
        "scene_anchor": scene_anchor,
        "evidence": evidence,
        "exists_in_worktree": exists_in_worktree,
    }


def format_java_file(file_path, diff, repo_path=None, content=None, compact=False, exists_in_worktree=True):
    """格式化单个 Java 文件的分析结果"""
    inspection = inspect_backend_file(
        file_path,
        diff,
        repo_path=repo_path,
        content=content,
        compact=compact,
        exists_in_worktree=exists_in_worktree,
    )
    role = inspection["role"]
    entry_info = inspection["entry_info"]
    product_signals = inspection["product_signals"]
    changes = inspection["changes"]
    scene_anchor = inspection["scene_anchor"]
    evidence = inspection["evidence"]

    lines = []
    lines.append(f"\n{'='*60}")
    lines.append(f"[后端] {file_path}  [{role}]")
    lines.append(f"{'='*60}")
    if not inspection["exists_in_worktree"]:
        lines.append("\n▶ 文件状态：本次分析范围内已删除或迁出当前工作区")

    if entry_info:
        lines.append(f"\n▶ 入口类型：{entry_info['entry_type']}")
        if scene_anchor:
            lines.append(f"▶ 场景锚点：{scene_anchor}")

        if entry_info['http_routes']:
            lines.append(f"▶ 路由路径：{', '.join(entry_info['http_routes'])}")

        if entry_info.get('is_dubbo_interface'):
            lines.append(f"▶ ⚠️  Dubbo 接口定义变更（RPC 契约变化，影响所有调用方）")
        elif entry_info['dubbo_interfaces']:
            lines.append(f"▶ 实现接口：{', '.join(entry_info['dubbo_interfaces'][:3])}")

        if diff:
            if changes['added_routes']:
                lines.append(f"▶ 新增路由：{', '.join(changes['added_routes'])}")
            if changes['removed_routes']:
                lines.append(f"▶ 删除路由：{', '.join(changes['removed_routes'])}")
            if changes['added_methods']:
                lines.append(f"▶ 新增方法：{', '.join(changes['added_methods'][:5])}")
            if changes['removed_methods']:
                lines.append(f"▶ 删除方法：{', '.join(changes['removed_methods'][:5])}")
            if changes.get('breaking_changes'):
                lines.append(f"⚠️  Breaking Change（方法签名变化，可能破坏兼容性）：")
                for bc in changes['breaking_changes']:
                    lines.append(f"   - {bc}")
            if changes.get('validation_changes'):
                lines.append(f"▶ 校验规则变更：")
                for vc in changes['validation_changes']:
                    lines.append(f"   - {vc}")
    else:
        lines.append(f"\n▶ 非入口层（中间层/数据层）")
        if scene_anchor:
            lines.append(f"▶ 场景锚点：{scene_anchor}")
        if role in {"Service 接口", "Service 实现", "数据访问层", "数据传输对象", "业务实体", "类"}:
            lines.append("▶ 默认作为支撑证据，通常应并入上层入口或流程，不要单独写成主功能")

    signal_lines = []
    if product_signals.get("added_fields"):
        signal_lines.append(f"- 新增字段：{', '.join(product_signals['added_fields'][:6])}")
    if product_signals.get("removed_fields"):
        signal_lines.append(f"- 删除字段：{', '.join(product_signals['removed_fields'][:6])}")
    if product_signals.get("added_enum_values"):
        signal_lines.append(f"- 新增枚举值：{', '.join(product_signals['added_enum_values'][:6])}")
    if product_signals.get("removed_enum_values"):
        signal_lines.append(f"- 删除枚举值：{', '.join(product_signals['removed_enum_values'][:6])}")
    if product_signals.get("labels"):
        signal_lines.append(f"- 文案/标签线索：{', '.join(product_signals['labels'][:6])}")
    if product_signals.get("role_hints"):
        signal_lines.extend([f"- {hint}" for hint in product_signals["role_hints"]])

    if signal_lines:
        lines.append("\n▶ 产品信号：")
        lines.extend(signal_lines)

    lines.append(f"\n[{'关键证据' if compact else 'Diff'}]")
    lines.append(evidence if evidence else "（无法获取 diff）")

    return "\n".join(lines)


def is_entry_file(file_path, repo_path=None, content=None):
    """检查文件是否是入口层文件（包括配置类）"""
    try:
        if content is None:
            full_path = os.path.join(repo_path, file_path) if repo_path else file_path
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        if any(ann in content for ann in BACKEND_ENTRY_ANNOTATIONS):
            return True
        if "Config" in os.path.basename(file_path) or "Configuration" in os.path.basename(file_path):
            return True
        return False
    except Exception:
        return False
