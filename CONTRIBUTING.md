# 贡献约定

本文根据仓库现有提交风格整理，适用于代码、测试、文档和配置示例的修改。
开发者和代理应在提交前阅读本文件。

## 提交说明

标题使用英文，采用以下形式：

```text
type(scope): describe the resulting change
```

常用类型为 `fix`、`feat`、`refactor`、`docs` 和 `test`。`scope` 指明主要组件，如
`web`、`config`、`ingest` 或 `summarize`；一项连贯修改涉及多个组件时可以省略。
冒号后使用简洁的祈使表达，以小写单词开头，末尾不加句号。

标题应说明具体变化，避免 `Fix bugs`、`Update code`、`Address review` 等无法单独
理解的表述。通常以一行内约 72 个字符为目标，不为缩短标题牺牲准确性。

实质性修改需要正文。先说明原有问题、触发条件或缺失能力，再说明修改后的行为，
最后补充有助于审查的原因、取舍和验证结果。正文应让没有读过对话的人也能理解修改，
不以文件清单或对话过程代替解释；按内容自然分段，长行可在约 72 个字符处换行。

例如，下面是一条修复提交的写法：

```text
fix(web): retain the group when submitting feedback

Feedback requests used the last group stored in a cookie. Switching
groups in another tab could therefore apply an ignore action to a
different group from the one shown on the page.

Include the rendered group in the request and use it for both the
state update and the returned fragment. The cookie remains the default
selection for requests that do not specify a group.

The regression case uses two groups sharing one paper and changes the
cookie before submitting feedback from the first group's page.
```

示例展示说明结构，不要求每条提交采用固定段数。简单的文字修正可使用较短说明；
涉及行为、迁移或兼容性的改动应写清边界，只报告实际完成的验证。

## 提交范围

一条提交应围绕一个可解释、可验证的变化组织。修复及其必要测试和文档可以一起提交；
彼此独立的问题应拆开，便于定位、审查和回退。不要仅按“第一轮审查”“第二轮审查”
或“后端部分”划分提交。

提交前检查实际 diff，确认说明与最终内容一致，并检查是否包含无关文件或个人数据。
`.env`、真实配置、数据库和个人邮件不进入版本库。

GitHub 账号启用邮箱隐私保护时，使用该账号的 GitHub 隐私邮箱提交。
不要为完成推送关闭隐私保护。修改已合并的提交说明会影响共享历史，应通过后续
规范提交改进，不擅自重写主分支。

## 验证与文档

验证范围与修改风险相匹配。代码修复优先覆盖实际触发条件；涉及分组或数据库时，
关注跨组状态和旧库迁移。文档修改检查链接、示例以及与代码的对应关系，无需为了
增加测试数量编写重复检查。

PR 和提交说明区分“已验证”“根据代码推断”和“尚未验证”。功能文档描述当前可用
行为；方案文档须明确标注未实现的部分。

## 用户体验

目标用户具备研究背景，但不预设编程能力。用户可以借助基本的代理工具完成安装和
启动，日常使用应在网页内完成。

配置功能优先使用表单、选择器、标签输入和合理默认值。向用户展示“研究方向”“关注
的期刊”“每天更新时间”等任务概念，隐藏存储格式和内部标识。错误信息应说明哪个
输入需要修改、如何修改，以及原有设置是否保留。

中文界面和使用文档应书面、直接、易懂。技术细节只在有助于用户作出选择时出现；
不将 YAML 编辑器、正则输入框或命令行说明作为普通设置页面的替代方案。
