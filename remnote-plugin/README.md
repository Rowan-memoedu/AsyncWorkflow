# 异步工作流 RemNote 插件

本项目独立子包；插件 ID `personal-async-workflow`。直接使用官方 `@remnote/plugin-sdk`，不加载、调用或修改现有 RemNote MCP 插件。

## 首次加载

构建产物由本机服务在 `http://localhost:43141/plugin/` 提供。服务的 `/status` 是连通性检查，返回 `connected:false` 只代表插件尚未连接。

在 RemNote 的 Settings → Plugins → Build → Develop from localhost 填入上述地址。当前已核验的 SDK/MCP 未提供安装插件接口；首次加载需要 RemNote 原生设置页。无需关闭现有 MCP 插件，也不复用其端口或凭据。

首次连接绑定当前知识库，之后拒绝其他知识库或第二个并发客户端。连接密钥由本机 DPAPI 保管，通过同源 bootstrap 发给插件；静态资源可跨域读取，密钥接口不可跨域读取。真实 RemNote 加载后的 Origin 与连接行为尚待验收。

## 仅五项操作

1. `status`：连接状态和协议版本。
2. `template_snapshot`：显式读取指定模板树并缓存；不轮询。
3. `search_documents`：SDK 模糊搜索并返回真实文档候选。
4. `resolve_daily`：按录音日期解析 Daily Note 和静态模板路径，返回实际节点 ID。
5. `publish_outline`：创建本流程拥有的新节点，核对正文和父子关系，按 effect key 去重。

没有任意 SDK 调用、通用替换、删除或执行代码接口。SDK 权限枚举较粗，实际对外操作限制在以上五项。

## 验收与开发

```powershell
npm ci
npm run check
npm test
npm run build
```

产物输出到 `D:/Artifacts/AsyncWorkflow/remnote-plugin`。测试覆盖树预校验、相同请求复用、响应丢失、用户修改保护、日期和缓存父链核对。

实际连接成功后，在主仓库运行：

```powershell
.\.venv\Scripts\python.exe -m async_workflow.cli sync-template
.\.venv\Scripts\python.exe scripts/remnote_plugin_canary.py --parent-id '<本次明确使用的验收文档 ID>'
```

Canary 使用公开样例；验证嵌套层级、公式、字面符号、只有标题以及相同请求不重复创建。之后必须从 RemNote 实际读取节点核验，构建通过不能代替真实验收。
