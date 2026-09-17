# 开发导览

本文帮助协作者定位代码和理解必须保持的行为。安装及验证命令见
[贡献指南](../CONTRIBUTING.md)，用户操作见[使用指南](web-settings.md)。

## 运行方式

LitRadar 使用 Python、FastAPI、SQLite、Jinja2 和原生 JavaScript，无独立前端构建服务。
命令行、网页和每日调度调用同一套处理阶段：采集 → 补全 → 评分 → 摘要。

文献题录共享，每个研究方向通过成员关系选择自己的文献。评分、忽略和方向性说明按组
保存；收藏、阅读状态和中性摘要共享。修改分组功能前先确认状态应属于哪一层。

## 从哪里开始修改

| 任务 | 主要入口 |
| --- | --- |
| 编排阶段、添加命令 | [pipeline.py](../litradar/pipeline.py)、[cli.py](../litradar/cli.py) |
| 新数据源或邮件解析 | [sources/](../litradar/sources/)、[normalize.py](../litradar/normalize.py) |
| 元数据补全 | [enrich.py](../litradar/enrich.py) |
| 评分规则与增量重评 | [rank.py](../litradar/rank.py)、[ranking_state.py](../litradar/ranking_state.py) |
| 摘要生成与复用 | [summarize.py](../litradar/summarize.py) |
| 模型协议与结果校验 | [llm.py](../litradar/llm.py) |
| 数据持久化与迁移 | [db.py](../litradar/db.py) |
| 设置、字段和凭据 | [settings.py](../litradar/settings.py)、[settings_fields.py](../litradar/settings_fields.py)、[credentials.py](../litradar/credentials.py) |
| 网页与交互 | [web/](../litradar/web/) 下的路由、模板和静态文件 |
| 后台运行与进度 | [web/jobs.py](../litradar/web/jobs.py)、[progress.py](../litradar/progress.py) |
| 定时更新与运行限制 | [scheduler.py](../litradar/scheduler.py)、[execution.py](../litradar/execution.py) |

## 数据与失败边界

新增题录通过流水线统一清洗和去重，来源适配器负责协议与记录转换。邮件在题录入库成功后
才归档或标记已读。不要在等待网络时持有不必要的写事务。

SQLite 首次连接自动检查迁移。改表结构需同时维护 `SCHEMA` 和迁移列表，旧库逐步升级；
不能用删除或重建用户数据的方式代替迁移。

评分状态分为成功结果、偏好基线和待完成的重评集合。首次评分覆盖本方向所有合格成员，
不受发表日期或旧 `rerank_top_k` 限制。关键词变化时按旧综合分冻结历史范围，每批成功结果
与待完成状态一并提交。失败不得清空旧结果，普通重试不得扩大已选范围。

旧评分缺少偏好版本时保持未知，首次升级只建立变化检测基线。输入证据和反馈指纹用于
追溯，不能因补齐摘要或新增收藏而自动启动全库重评。行为测试见
[test_rank_incremental.py](../tests/test_rank_incremental.py)。

摘要按文献复用中性内容，方向性说明必须限定组。单个方向失败不应让其他方向丢失成功结果。

## 网页、配置与后台任务

页面操作携带当前方向，Cookie 只提供默认选择。新写入口需复用访问控制、同源校验和
适用的操作授权；服务端逐次验证，前端倒计时不是权限依据。

设置按页合并、校验版本并原子保存。凭据独立于普通配置，优先级为部署环境、应用私有文件、
兼容 `.env`。运行中的任务使用启动快照，修改设置只影响后续任务。

所有执行入口共用流水线锁和运行次数检查。网页提交后台任务后轮询持久化进度；同一请求
标识只能对应同一阶段、方向、时间参数和强制模式。刷新页面不能再次发起付费操作。

调度由独立进程负责，Web worker 不各自注册任务。日期认领防止当天重复自动运行，旧系统
定时入口须由部署维护者停用。生命周期配置见[部署与维护](deployment.md)。

## 文档边界

用户文档解释如何完成任务和如何理解结果；开发文档解释组件职责与跨组件约束。
具体字段、算法步骤和协议错误分支以代码与测试为准，避免复制成容易过期的长篇清单。
已完成的方案应合并为当前行为说明，不保留对话记录或逐轮修复日志作为使用文档。
