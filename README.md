# daily-changelog

`daily-changelog` 是一个面向中文产品更新日志场景的 skill 仓库。

它的目标不是罗列技术改动，而是把多仓库代码变更转成用户能读懂的产品更新说明。

## 这个 skill 解决什么问题

- 把 Java 后端和 React 前端的代码变更合并理解
- 从接口、页面、字段、筛选项、指标等技术信号中提炼产品能力变化
- 生成中文、面向业务方的 changelog / release notes
- 强制避免文件名、类名、方法名、接口路径直接出现在最终结果中
- 要求每条日志带有场景锚点，例如页面、模块、角色或操作场景

## 适用场景

当用户提到以下需求时可使用：

- 今天做了什么
- 更新日志
- changelog
- release notes
- 产品变更说明

## 输入与输出

- 输入：单日或日期区间，可选多仓库路径
- 输出：按日期组织的中文产品更新日志
- 分类：`✨ 新功能`、`🔄 功能变更`、`🔧 技术改造`、`🐛 Bug 修复`
- 规则：空日期不输出，空分类不输出

## 核心约束

- 最终结果必须写成产品语言，而不是研发语言
- 每条内容必须说明“用户能做什么”或“系统发生了什么变化”
- 每条内容必须包含场景锚点
- 不输出文件路径、文件名、类名、方法名、组件名
- 分析时间以合并到主分支的时间为准，而不是单个提交时间

## 仓库结构

- `SKILL.md`：skill 的主入口与运行规则
- `scripts/context_fetcher.py`：本地调试用分析脚本
- `scripts/backend_analyzer.py`：后端变更信号提取
- `scripts/frontend_analyzer.py`：前端变更信号提取
- `references/changelog_template.md`：最终输出模板

## 本地调试

`SKILL.md` 是运行时的权威说明；脚本主要用于本地验证和调试。

```bash
python3 scripts/context_fetcher.py --help
python3 scripts/context_fetcher.py --since 2026-03-01 --until 2026-03-07 --repo-path /path/to/repo
python3 scripts/context_fetcher.py --since 2026-03-01 --repos "backend:/path/to/backend,frontend:/path/to/frontend"
```

## 示例输出

```markdown
## 2026-03-11

### ✨ 新功能

- 列表页新增条件筛选能力，用户可更快定位目标数据

### 🐛 Bug 修复

- 修复统计页中的时间排序异常，最近数据展示更准确
```

## 维护说明

- 如果修改行为规则，优先更新 `SKILL.md`
- 如果修改信号提取逻辑，再同步更新 `scripts/`
- 公共示例应保持中性，不包含真实项目名、业务专有名词或内部路径

## 许可证

MIT License - see `LICENSE` for details.
