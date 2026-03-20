---
name: daily-changelog
description: 用于更新日志、changelog、release notes、产品变更说明、今天做了什么等请求；基于代码 diff 生成中文产品更新日志。
---

# Daily Changelog

这个 skill 是 vendor-neutral 的，目标是让任何能读取本地文件、执行仓库脚本并使用当前宿主模型的 agent，都能把代码变更整理成中文产品更新日志。

## Skill 定位

- 这是一个 skill 项目，目标是基于代码变更生成中文产品更新文档。
- 调用方只需要表达时间范围、项目路径和是否需要落盘输出；不需要理解内部实现。
- skill 负责提取代码证据、归并主题、驱动宿主模型逐条写作并输出最终结果；调用方 agent 不应绕过 skill 自己拼流程。

## 何时使用

当用户提到以下需求时使用：

- 更新日志
- changelog
- release notes
- 产品变更说明
- 今天做了什么

## 调用方视角

- 调用方只需要表达：时间范围、项目路径、可选输出文件路径。
- 调用方不需要执行 Python 命令。
- 调用方不需要感知主题账本、写作包、生成条目 JSON 或其他中间产物。
- 对调用方来说，这个 skill 应当是黑盒：成功时直接得到最终 changelog，失败时只返回失败原因且不保存最终文件。
- 调用方 agent 不得把中间 JSON、prompt 或条目生成工作外抛给用户，也不得要求用户自己继续处理。

可直接触发的自然语言示例：

- 使用 `$daily-changelog`，基于 `/path/to/project` 在 `2026-03-01` 到 `2026-03-07` 的代码变更，生成中文产品更新日志，并保存到 `/path/to/product-changelog.md`
- 使用 `$daily-changelog`，基于 `/path/to/project` 今天的代码变更生成中文产品更新日志

## 输入与默认值

- 日期支持单日或日期区间；未提供日期时默认今天。
- “上线至今” / “自上线以来” 直接映射为 `earliest`。
- 单个路径优先按 `--repo-path` 处理，可为单仓库或聚合目录。
- 用户明确给出多个独立仓库路径时，再改用 `--repos`。
- 当日期跨度超过 31 天时，skill 会在内部自动分段执行并统一汇总；调用方不要自行按年、按月或按单日拆分。
- 输出顺序只定义最终多个日期块的排列方式：`desc` 为最新在前，`asc` 为最早在前；未指定时默认使用 `desc`。
- 单日和多日使用同一套逐日流程；单日只是只处理一个日期。

## 内部工作流

1. 先运行 `scripts/changelog_pipeline.py prepare`，生成最新主题账本、主题草稿和 pipeline manifest。
2. 再运行 `scripts/changelog_pipeline.py generate`，生成逐条写作包；每个写作包都带 `theme_id`、日期、分类、主题锚点、commit 证据、文件路径和 diff 片段。
3. 读取 `references/writing_rules.md`、`references/changelog_template.md` 和 `references/entry_writer_prompt.md`，使用当前宿主模型逐条生成条目；模型在这一步只产出结构化条目 JSON，不直接自由输出整篇 markdown。
4. 结构化条目 JSON 必须至少包含 `theme_id` 和 `text`；每个可发布主题都要生成且只能生成一条。
5. 运行 `scripts/changelog_pipeline.py finalize`，基于主题账本校验结构化条目，随后由 skill 内部组装最终 markdown，并执行结构校验。
6. 若条目校验失败，skill 内部只重写失败的 `theme_id` 条目；必要时才进一步降级或删除仍无法通过校验的条目。
7. 最终只有通过校验的 markdown 才能落盘；空日期不输出，空分类不输出。

## 宿主模型写作要求

- 宿主模型只负责“逐条 record 写文案”，不要自己决定日期结构、分类结构或整篇 markdown 排版。
- 宿主模型返回的结构化条目 JSON 供 skill 内部继续校验和装配；不要把中间 JSON 直接呈现给最终用户。
- 宿主模型写作时必须保留场景锚点，优先描述用户能做什么或系统发生了什么变化。
- 若产品含义无法确认，宁可降级成抽象业务描述，也不要输出技术细节。

## 失败后的正确动作

- 返回 `REPO_DISCOVERY_ERROR`、条目校验失败摘要、路径不存在或不可访问等明确原因。
- 如果结构化条目校验失败，skill 内部应先针对失败的 `theme_id` 重写；不要手动修补最终 markdown 成品。
- skill 失败时不保存任何无效文件。

## 参考文件

- `references/writing_rules.md`
  用于查看产品化改写规则、禁写项、主题归并原则和常见技术信号的翻译方式。
- `references/changelog_template.md`
  用于约束最终 markdown 的标题结构和最小模板。
- `references/entry_writer_prompt.md`
  用于驱动宿主模型逐条生成结构化条目 JSON；只在 skill 内部写作阶段读取。

## 输出硬约束

1. 最终文件第一条非空内容必须是 `# 产品更新日志`
2. 日期标题必须严格为 `## YYYY-MM-DD`
3. 只能使用这 4 个分类：`✨ 新功能`、`🔄 功能变更`、`🔧 技术改造`、`🐛 Bug 修复`
4. 空日期不输出，空分类不输出
5. 每条内容都必须说明“用户能做什么”或“系统发生了什么变化”，并带一个场景锚点
6. 不得输出文件路径、文件名、类名、组件名、接口路径、DTO、VO、SQL、Controller、Service、Mapper、Req、Resp
7. 不得输出 commit message 风格前缀、统计信息、生成时间、工具名、仓库列表
8. 同一主题下的前后端、SQL、DTO、页面支撑文件默认应归并成一条产品描述，不要按技术文件拆条

## 校验与失败处理

- `scripts/changelog_pipeline.py prepare`、`generate`、`finalize` 构成 skill 内部生成链路。
- `generate` 产物不是最终 changelog，而是给宿主模型使用的逐条写作包。
- `finalize` 会先校验结构化条目，再由 skill 内部组装最终 markdown 并执行结构校验。
- 校验失败时，skill 自动执行重写→降级→删除流程：先只修复失败条目，必要时进一步降级，最终才删除仍无法通过校验的条目。
- 只有在无法恢复时才会报告失败原因并终止；失败时不会保存任何无效文件。
