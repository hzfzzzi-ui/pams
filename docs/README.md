# 文档入口

文档只维护当前规则与接口。开发进度、测试结论和验收归 GitHub 对应 PR；部署记录归功能 PR 评论。仓库不保存旧开发记录、验收快照或重复状态表。

## 四个业务主题

| 主题 | 负责内容 |
| --- | --- |
| [数据与收益口径](DATA_AND_RETURNS.md) | 事实分层、估值、收益、汇兑、归属、分类与展示 |
| [资料导入与事实写入](IMPORT_AND_FACTS.md) | 原件、访问、暂存、预检、完整持仓、原子发布和来源身份 |
| [券商同步与对账](BROKER_SYNC_AND_RECONCILIATION.md) | 报告日期、同步、日结、只读审计与费用证据 |
| [修复、发布与恢复](REPAIR_RELEASE_AND_RECOVERY.md) | 来源预检、隔离演练、审批、发布身份和恢复边界 |

## 开发与配置

| 入口 | 内容 |
| --- | --- |
| [项目架构](../README.md) | 目录树、产品范围与业务依赖方向 |
| [Agent 规则](../AGENTS.md) | 每轮必读内容、授权及交付要求 |
| [开发工作流程](DEVELOPMENT_WORKFLOW.md) | S/M/L 分级、验证、PR 和发布门禁 |
| [配置来源](CONFIGURATION.md) | 配置读取优先级、环境变量、服务和挂载 |
| [页面与接口边界](PAMS_PAGE_BOUNDARY.md) | 页面、导航、主要 API 与展示职责 |
| [前端设计规则](PAMS_FRONTEND_DESIGN_SYSTEM.md) | 移动端布局、视觉保护及构建样式 |

本文说明所在源码检出；合并、部署和用户验收需分别核对。业务实现位于 `backend/api/domain/`，测试位于 `tests/` 对应目录。策略与回测由其他应用承担，PAMS 源码已移除研究页面、扫描任务和信号接收；运行服务可能仍是旧版本。空库初始化与隔离清理方案以 [PR #90](https://github.com/hzfzzzi-ui/pams/pull/90) 及其合并状态为准，不代表生产库已变更。
