# 异步工作流 RemNote 插件

本项目独立子包；插件 ID `personal-async-workflow`。直接使用官方 `@remnote/plugin-sdk`，不加载、调用或修改现有 RemNote MCP 插件。

0.1.9 修复 Daily Note 自动模板生成与分支创建之间的竞态。`getDailyDoc` 返回后目标分支可能尚未出现；缺失时每 100ms 重新读取，最多等 3 秒，创建未满一分钟的日期文档仍缺目标时返回 `daily_template_pending`，调用方最多重试三次。旧日期文档确实缺失的分支仍可创建。路径缓存每次重新核对直接子节点；旧创建回执能证明额外节点身份、且只有一个非插件同名节点时改绑，否则保留歧义。已有内容不自动合并或删除。`inspect_daily` 仅用于已知日期文档的分支标题、创建时间与相关缓存诊断，不创建日期文档，不读取录音正文。健康请求独立于写队列，写入仍串行。

录音输出修复在 0.1.2 完成：同时保护 ASCII 卡片分隔符和 Unicode 箭头，公式与 Markdown 继续使用官方 SDK 解析；整树解析通过后才创建节点，避免后续解析失败留下部分内容。`publish_outline` 返回本次创建节点的 SDK 回读快照，便于检查 LaTeX 类型、正文、背面为空和父节点；不提供任意笔记读取接口。0.1.1 的真实回归曾因箭头保护遗漏失败；0.1.2 已实际加载，通过原失败输入、独立样例、数学元素与字面符号回读及重复请求验收。相应 16 项插件测试、类型检查和构建也通过。后续版本增加的其他功能需独立验收，不能沿用此结论。

## 首次加载

构建产物由本机服务在 `http://localhost:43141/plugin/` 提供。服务的 `/status` 是连通性检查，返回 `connected:false` 只代表插件尚未连接。

在 RemNote 的 Settings → Plugins → Build → Develop from localhost 填入上述地址。当前已核验的 SDK/MCP 未提供安装插件接口；首次加载需要 RemNote 原生设置页。无需关闭现有 MCP 插件，也不复用其端口或凭据。

首次连接绑定当前知识库，之后拒绝其他知识库或第二个并发客户端。连接密钥由本机 DPAPI 保管，通过同源 bootstrap 发给插件；静态资源可跨域读取，密钥接口不可跨域读取。实际 Origin、连接、模板读取、搜索和按日期解析已验收；服务重启后插件会自动重连，重连不会重新读取模板。

## 录音归档操作

0.1.7 的 `resolve_meeting` 使用调用方绑定的真实 document ID，支持内部会议预案下的对象分目录和用户改名；复用录音日期与会议记录，缺失时创建真实日期 document 和正文分支，重复调用复用原 ID。已有 M.D 日期沿用，新日期使用 YYYY-MM-DD，重名或日期冲突不猜测。仅修改独立插件，不修改 RemNote MCP。

1. `status`：连接状态和协议版本。
2. `template_snapshot`：显式读取指定模板树并缓存；不轮询。
3. `search_documents`：SDK 模糊搜索并返回真实文档候选。
4. `resolve_daily`：按录音日期解析 Daily Note 和静态模板路径，返回实际节点 ID。
5. `publish_outline`：创建本流程拥有的新节点，核对正文和父子关系，按 effect key 去重。
6. `targets_snapshot`：显式保存待办清单结构，与日记模板分别缓存。
7. `create_document`：创建真实 document，支持根级或明确的父目录，保存并核对创建结果。
8. `resolve_todo`：解析购物清单、信息清单对应软件及杂事待办目标；需要时创建软件分支。
9. `relocate_outline`：仅迁移本插件拥有且内容完整匹配的旧输出，保留 ID；旧元数据移入已核验的根级 trash。

没有任意 SDK 调用、通用替换、删除或执行代码接口。SDK 权限枚举较粗，录音归档实际对外操作限制在以上范围。已有命令收件箱模块由另一任务维护，本轮未改变其语义。

0.1.4 的 `publish_outline` 只写大纲正文，来源和路由元数据保存在运行日志及内部效果记录。迁移先核对原正文、原 ID 和父链，发现人工改动即停止；创建回包未知不盲目重建，迁移回包丢失则核对同一 ID 的位置。34 项插件测试和类型检查通过，真实 document 创建、正文无元数据、同 ID 往返迁移与独立 MCP 回读通过。MCP 仅用于验收读回，不参与插件运行。

## 验收与开发

0.1.5 在每个整体正文块前后创建各一个空白同级 Rem，独立记录空节点身份，并用 SDK `moveRems` 保持相邻顺序。重复发布复用原空节点，迁移时连同正文一起移动；发现空节点被用户写入文字或子节点则停止。36 项插件测试、类型检查和构建通过；真实往返迁移及独立 `list_children` 回读通过。普通树读取可能省略末尾空节点，因此空行验收使用完整直接子节点列表。实际录音共 14 个正文块已验证。

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
.\.venv\Scripts\python.exe scripts/remnote_plugin_canary.py --parent-id '<本次明确使用的验收文档 ID>' --run-id '<本轮公开测试标识>'
```

Canary 使用公开样例；验证嵌套层级、公式、字面符号、只有标题以及相同请求不重复创建。之后必须从 RemNote 实际读取节点核验，构建通过不能代替真实验收。
