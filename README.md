# AsyncWorkflow

Windows 本地异步录音工作流：XiaomiAlbumSyncer → Prefect → Qwen 转写 → 原结构化提示词 / DeepSeek → 语义分块 → 独立 RemNote 插件、官方 Codex App Server 或已配置 CLI。

主体代码和本机服务已搭建，正式录音入口 `enabled=false`。App Server 创建的真实 Thread 已通过桌面列表、跨进程续接和桌面继续交互验收。独立 RemNote 插件 0.1.2 的真实符号、公式、层级和幂等回归通过；新的公开混合录音已完成 RemNote 归档与真实 Thread 执行。断线等待、后台 worker 重启后的自动恢复及跨日归档均完成真实回读。小米登录、历史基线、下载配置及全部 440 个文件的原名、字节数和 SHA-1 核验通过；真实手机新增录音端到端仍待验收。

## 边界

- 新命令通过官方 `openai-codex` SDK 创建持久 Thread，先保存 Thread ID，再提交并保存 Turn ID。用户直接在该 Thread 继续；桌面持有写入权时不创建替代 Thread。
- RemNote 使用 `remnote-plugin` 独立子包和 43141 回环服务，不修改或连接现有 RemNote MCP。只提供状态、显式模板快照、文档搜索、日期分支解析和幂等大纲写入。
- “日记模板”显式同步为本地快照；分类时不实时读取模板。口述目标模糊匹配后使用首选；无目标按录音日的模板分支分类；跨类整体或无可用候选进入“其他碎片”。
- 命令依据原录音的语义判断，不依赖固定触发词；引用、否定和撤回不构成执行授权。需要决定的命令在自己的 Thread 中讨论。
- 保存原始输入和副作用记录。未知提交先核对，不盲目重试；重新分析不自动重放已执行动作。
- RemNote 连接中断时使用 Prefect 原生持久暂停；插件实际恢复后自动续接同一运行和效果标识。人工待答与无法核对的旧写入结果不自动放行。

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

独立插件地址为 `http://localhost:43141/plugin/`，0.1.2 已实际加载并通过原输入及独立样例回归。见 [插件说明](remnote-plugin/README.md)。日记模板已显式同步为 19 个有效分支。本任务后续需要重载插件时，可按用户明确授权重启 RemNote，再核验实际版本和连接；本轮无需重启。

小米账户已扫码成功并核验云端访问；按明确账户 ID 绑定，保留旧失效账户。五分钟音频下载任务已启动，440 条历史录音基线已保存，全部原名、字节数与 SHA-1 一致；该下载器的录音路径使用全量元数据枚举及已有文件跳过，不能把相册的游标增量能力推广到录音。已按真实样本补齐月-日-年文件名格式。公开混合录音与断线恢复通过后，仍需一条真实新增手机录音验证上传到归档的完整链路，正式入口继续关闭。目标不确定的低优先级 RemNote 清单后置，明确指向既有 Thread 名称的完整解析尚未完成。

完整 Spec 和当前验收分别位于 `D:/Artifacts/AsyncWorkflow/2026-10-02/02-架构与规格草案.md`、`05-部署与验收.md`、`06-AppServer-实测.md`。
