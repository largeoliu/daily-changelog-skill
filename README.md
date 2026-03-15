# daily-changelog

`daily-changelog` 是一个面向中文产品更新日志场景的 skill 仓库。

它的目标不是罗列技术改动，而是把多仓库代码变更转成用户能读懂的产品更新说明。

它最大的亮点不是“根据 commit message 改写”，而是直接读取代码 diff 与文件上下文来理解真实变更，尽量避免被失真的提交说明带偏。

## 核心亮点

- 不是基于 commit message 拼接更新日志，而是基于实际代码 diff、关键证据和文件上下文提炼结论
- 即使提交说明写得很粗、很泛，甚至和实际改动不完全一致，仍能尽量还原真实产品变化
- 更适合多仓库、多人协作、merge commit 较多、提交信息质量不稳定的团队环境

## 这个 skill 解决什么问题

- 把 Java 后端和 React 前端的代码变更合并理解
- 从接口、页面、字段、筛选项、指标等技术信号中提炼产品能力变化
- 不依赖 commit message 生成日志，而是基于实际代码 diff 和文件上下文判断变更，避免提交说明失真
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
- 规则：空日期不输出，空分类不输出，最终文件保留标题和更新日志正文

## 路径处理

- 可以传单个 git 仓库路径
- 也可以传项目总目录或父目录
- 如果输入路径本身不是 git 仓库，脚本会自动发现子目录中的 git 仓库并切换到多仓库分析模式
- 只有在你明确想指定仓库集合时，才需要手动传 `--repos`

## 核心约束

- 最终结果必须写成产品语言，而不是研发语言
- 每条内容必须说明“用户能做什么”或“系统发生了什么变化”
- 每条内容必须包含场景锚点
- 不输出文件路径、文件名、类名、方法名、组件名
- 结论以实际代码 diff 和文件内容为准，不能把 commit message 当作主要依据
- 分析时间以合并到主分支的时间为准，而不是单个提交时间

## 仓库结构

- `SKILL.md`：skill 的主入口与运行规则
- `scripts/context_fetcher.py`：本地调试用分析脚本
- `scripts/changelog_guard.py`：最终 changelog 结构校验器
- `scripts/backend_analyzer.py`：后端变更信号提取
- `scripts/frontend_analyzer.py`：前端变更信号提取
- `references/changelog_template.md`：最终输出模板

## 本地调试

`SKILL.md` 是运行时的权威说明；脚本主要用于本地验证和调试。

```bash
python3 scripts/context_fetcher.py --help
python3 scripts/context_fetcher.py --since 2026-03-01 --until 2026-03-07 --repo-path /path/to/project-root --compact
python3 scripts/context_fetcher.py --since 2026-03-01 --repos "backend:/path/to/backend,frontend:/path/to/frontend" --compact
```

`--compact` 会把大段 raw diff 压缩成关键证据片段，更适合交给 skill 或 LLM 继续生成最终更新日志。

最终文件生成后，建议再运行结构校验，确保日期标题严格按天输出，而不是被合并成区间：

```bash
python3 scripts/changelog_guard.py --file /path/to/changelog.md --order desc --check-tech
```

如果校验失败，说明结果里出现了日期区间标题、重复日期、非法分类，或仍混入了类名、路径、文件名等技术细节，不能直接作为最终交付文件。

## 示例输出

```markdown
# 产品更新日志

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
