# LitRadar

LitRadar 是面向化学研究者的自托管文献阅读工具。它汇集订阅邮件和学术检索结果，
按研究方向评分，生成中文摘要，帮助你决定先读哪些论文。

A self-hosted literature radar with personalized ranking and Chinese summaries.

## 可以做什么

- 从 X-MOL 订阅邮件、Semantic Scholar、Crossref 和 OpenAlex 收集文献，也可追踪种子论文的被引记录。
- 为多个研究方向分别设置检索条件、关注内容、排除条件和评分偏好。
- 给新文献评分并复用已有结果；偏好改变后按分数或排名重评历史文献，也可手动全部重排。
- 阅读中文简要或深度摘要，收藏、标记已读，或隐藏当前方向中不感兴趣的文献。
- 在网页管理服务连接、获取模型列表、设置每天更新时间，并查看后台任务进度。
- 使用浅色或深色界面，在电脑和手机浏览器中阅读。

文献库保存在自己的设备或服务器上。启用在线检索和 AI 时，检索词、文献内容及必要的
研究偏好会发送到你选择的服务。AI 评分和摘要用于辅助筛选，阅读结论仍应核对原文。

## 快速开始

需要 Python 3.11 或更高版本。安装与常驻部署可交给维护者或代理协助；日常使用在网页完成。

Linux / macOS：

```bash
git clone https://github.com/KaguraSayuki/LitRadar.git
cd LitRadar
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/litradar init-db
.venv/bin/litradar setup-link --url http://127.0.0.1:8090
.venv/bin/litradar serve
```

Windows PowerShell：

```powershell
git clone https://github.com/KaguraSayuki/LitRadar.git
cd LitRadar
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\litradar.exe init-db
.venv\Scripts\litradar.exe setup-link --url http://127.0.0.1:8090
.venv\Scripts\litradar.exe serve
```

打开终端给出的一次性设置链接，设置访问密码。链接有效期为 30 分钟，只交给实例所有者。
无需复制示例配置；网页首次保存会创建设置。

1. 在“数据与邮箱”接入准备使用的文献来源。需要 AI 时，填写兼容 OpenAI Chat Completions 的服务地址、密钥和模型。
2. 在“研究方向”添加关注内容与检索条件，预览结果后保存。
3. 点击“更新这个方向”，或进入统计页选择“全部运行”。可以离开页面，稍后查看进度。
4. 在雷达和本期精选中阅读结果。若要每天更新，请维护者先完成调度接管，再在网页开启计划。

## 选择适合你的文档

| 你想做什么 | 文档 |
| --- | --- |
| 连接服务、使用网页、处理常见问题 | [使用指南](docs/web-settings.md) |
| 管理多个方向，理解评分与重排 | [研究方向与评分](docs/subscription-groups.md) |
| 安装常驻服务、升级、备份或恢复 | [部署与维护](docs/deployment.md) |
| 修改代码并参与协作 | [贡献指南](CONTRIBUTING.md)与[开发导览](docs/architecture.md) |
| 查看版本变化 | [更新记录](CHANGELOG.md) |

LitRadar 当前面向单用户，不提供多人账户权限。邮件连接支持文件上传和 IMAP 密码或授权码，
不支持 OAuth2；模型服务需要支持文本 Chat Completions。各外部服务的密钥、额度和费用由服务方管理。

## 参与项目

欢迎提交问题和改进。请先阅读[贡献指南](CONTRIBUTING.md)，使用合成数据说明问题。
真实研究方向、检索词、邮件、凭据及数据库应留在私有环境中，不能进入公开 issue、PR、截图或发布附件。

本项目采用 [MIT 许可证](LICENSE)。
