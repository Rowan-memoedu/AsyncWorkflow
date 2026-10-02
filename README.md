# AsyncWorkflow

Windows 本地异步录音工作流：XiaomiAlbumSyncer → Prefect → Qwen 转写 → 原结构化提示词 / DeepSeek → 语义分块 → 独立 RemNote 插件、官方 Codex App Server 或已配置 CLI。

主体代码和本机服务已搭建，正式录音入口 `enabled=false`。App Server 创建的真实 Thread 已通过桌面列表、跨进程续接和桌面继续交互验收。独立 RemNote 插件的静态模板、真实搜索、跨日目标解析和基本层级写入已验证；0.1.1 重载后的实际回归仍发现 Unicode 箭头触发卡片语法，已提供 0.1.2，等待重载复测。小米登录、历史基线、下载配置及落盘文件抽样核验通过；新增录音和新版整体端到端仍待验收。

## 边界

- 新命令通过官方 `openai-codex` SDK 创建持久 Thread，先保存 Thread ID，再提交并保存 Turn ID。用户直接在该 Thread 继续；桌面持有写入权时不创建替代 Thread。
- RemNote 使用 `remnote-plugin` 独立子包和 43141 回环服务，不修改或连接现有 RemNote MCP。只提供状态、显式模板快照、文档搜索、日期分支解析和幂等大纲写入。
- “日记模板”显式同步为本地快照；分类时不实时读取模板。口述目标模糊匹配后使用首选；无目标按录音日的模板分支分类；跨类整体或无可用候选进入“其他碎片”。
- 命令依据原录音的语义判断，不依赖固定触发词；引用、否定和撤回不构成执行授权。需要决定的命令在自己的 Thread 中讨论。
- 保存原始输入和副作用记录。未知提交先核对，不盲目重试；重新分析不自动重放已执行动作。

## 位置

- 源码：`D:/Projects/AsyncWorkflow`
- 运行数据、凭据、配置、静态模板及冻结 prompt：`D:/AppData/AsyncWorkflow`
- 文档、录音产物及插件构建：`D:/Artifacts/AsyncWorkflow`
- 下载与构建缓存：`D:/Caches/AsyncWorkflow`

私人录音、原 prompt、运行记录和本机配置不进入 Git。当前源码是独立私有仓库；原 AudioWorkflow 和 RemNoteMCP 源码保持原状。

## 本机检查

在仓库目录执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m async_workflow.cli doctor
```

在 `remnote-plugin` 目录执行 `npm run check`、`npm test`、`npm run build`。Python 运行依赖锁定在 `requirements.lock`；插件依赖锁定在 `package-lock.json`。初始化安装使用 D 盘虚拟环境和缓存；账户绑定保存在本机 DPAPI 加密文件。

启动项由 `scripts/install-startup.ps1` 管理，角色为 server、runner、watcher、xiaomi、remnote。服务只监听本机。`disable` 只停止新增录音进入处理链，不中断已有任务。

## 下一步验收

独立插件地址为 `http://localhost:43141/plugin/`，当前需在 RemNote 重新加载该插件以使用 0.1.2。见 [插件说明](remnote-plugin/README.md)。日记模板已显式同步为 19 个有效分支；重载后从原输入重新执行公开写入 canary，核验 SDK 原始回读和真实节点。

小米账户已扫码成功并核验云端访问；按明确账户 ID 绑定，保留旧失效账户。五分钟音频下载任务已启动，440 条历史录音基线已保存，抽样原名与 SHA-1 一致；该下载器的录音路径使用全量元数据枚举及已有文件跳过，不能把相册的游标增量能力推广到录音。已按真实样本补齐月-日-年文件名格式。之后还需验证一次新增录音、完整混合录音与断线恢复，保持正式入口关闭。目标不确定的低优先级 RemNote 清单后置，明确指向既有 Thread 名称的完整解析尚未完成。

完整 Spec 和当前验收分别位于 `D:/Artifacts/AsyncWorkflow/2026-10-02/02-架构与规格草案.md`、`05-部署与验收.md`、`06-AppServer-实测.md`。
