# pams-web

PAMS 使用 nginx 提供静态页面，页面通过 API 获取业务结果。

- [页面与接口边界](../../docs/PAMS_PAGE_BOUNDARY.md)：主页面、子入口、兼容跳转及模块职责。
- [前端设计规则](../../docs/PAMS_FRONTEND_DESIGN_SYSTEM.md)：导航、布局保留、源样式与生成样式。
- [数据契约](../../docs/DATA_AND_RETURNS.md)：事实来源、中文展示和分类边界。
- [开发工作流程](../../docs/DEVELOPMENT_WORKFLOW.md)：候选验证及发布要求。
- [GitHub PR](https://github.com/hzfzzzi-ui/pams/pulls)：开发及验收记录；[配置来源](../../docs/CONFIGURATION.md)说明发布记录与机器观察入口。

默认首页为 `app.html`，默认内容为投资页；外壳唯一持有“投资、收支、更多”主导航，六个业务 HTML 作为同源内容文档加载。旧业务地址经 Nginx 进入外壳并保留参数/片段。页面入口转调 `assets/js/features/`，共享 UI、请求作用域及展示工具分别位于 `components/`、`app/`、`shared/`；历史金额展示模型保留在所属业务域，不引入新金融算法。新页面或模块应先核对上述边界及根目录 AGENTS.md。
