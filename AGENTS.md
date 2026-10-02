# AsyncWorkflow

Windows 本地异步录音处理：Prefect 编排，Qwen 转写，冻结 prompt 整理，独立 RemNote 插件和官方 Codex App Server 输出。

- 源码只在此仓库；运行数据 D:/AppData/AsyncWorkflow，产物 D:/Artifacts/AsyncWorkflow，缓存 D:/Caches/AsyncWorkflow。
- 运行入口保持 disabled，直到独立 RemNote 插件和小米新增录音端到端通过。服务运行不等于功能验收。
- 不修改或连接现有 RemNote MCP。插件子包独立依赖、构建和协议；只实现本流程功能。
- 日记模板只在显式 sync-template 时读取。录音按 Asia/Shanghai 的录音日归档；无目标按模板语义分类，整体跨类别或无类别放其他碎片。
- 显式文档名模糊搜索后使用首选，无候选放其他碎片；高不确定清单后置，不阻塞归档。
- 新命令使用官方 openai-codex SDK 调用 app-server，及时保存真实 Thread/Turn ID。未知提交不重放；桌面接管后不抢占已有写入者。
- 脚本失败且没有 Thread 时才新建修复 Thread；已有 Thread 直接续接。
- 保留源录音、原 prompt、人工编辑和外部动作记录。新分析不自动重放副作用。
- 密钥、音频、私人模板、冻结私人 prompt、运行数据和本机配置不得提交 Git。
- 测试：`.venv/Scripts/python.exe -m pytest -q`；插件：`npm run check && npm test && npm run build`（在 remnote-plugin）。Windows 实际命令使用 pwsh 和进度超时工具。
- 当前事实和待验收项见 README；完整用户 Spec 在 D:/Artifacts/AsyncWorkflow/2026-10-02/02-架构与规格草案.md。
