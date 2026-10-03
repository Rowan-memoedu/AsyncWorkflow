# AsyncWorkflow

Windows 本地异步录音工作流：XiaomiAlbumSyncer → Prefect → Qwen 转写 → 原结构化提示词 / DeepSeek → 语义分块 → 独立 RemNote 插件、官方 Codex App Server 或已配置 CLI。

主体代码和本机服务已搭建，正式录音入口 `enabled=false`。App Server 创建的真实 Thread 已通过桌面列表、跨进程续接和桌面继续交互验收。独立 RemNote 插件 0.1.4 已实际加载，文档创建、正文归档、受保护迁移与幂等回归通过；真实录音的购物清单、软件消息归档及学习日志边界均已回读。公开混合录音、断线等待、worker 重启自动恢复和跨日归档已通过；小米登录、历史基线与下载完整性已核验。一条真实短录音出现 ASR 语义不稳定，已停止其输出并保留复核结果等待确认；新增录音自动入口尚未开放。

## 边界

- 新命令通过官方 `openai-codex` SDK 创建持久 Thread，先保存 Thread ID，再提交并保存 Turn ID。用户直接在该 Thread 继续；桌面持有写入权时不创建替代 Thread。
- RemNote 使用 `remnote-plugin` 独立子包和 43141 回环服务，不修改或连接现有 RemNote MCP。录音归档接口包括显式快照、文档搜索/创建、日期与待办分支解析、幂等写入和受保护的旧输出迁移。
- “日记模板”显式同步为本地快照；分类时不实时读取模板。口述目标模糊匹配后使用首选；无目标按录音日的模板分支分类；跨类整体或无可用候选进入“其他碎片”。
- 命令依据原录音的语义判断，不依赖固定触发词；引用、否定和撤回不构成执行授权。需要决定的命令在自己的 Thread 中讨论。
- 保存原始输入和副作用记录。未知提交先核对，不盲目重试；重新分析不自动重放已执行动作。
- 明确新建文档时创建真实 document；未指定父目录时为根级。信息清单按软件归档，包括拟发消息和回复草稿；购物清单收购物记录，其余记录型杂事待办进入待办清单根级。保存消息草稿不构成对外发送授权。
- 学习日志只收明确要求或具体学科学习；个人经历与投入取舍归私密日记，一般观点及工具反思归其他碎片。RemNote 仅保存结构化原文，元数据只留日志。
- RemNote 连接中断时使用 Prefect 原生持久暂停；插件实际恢复后自动续接同一运行和效果标识。人工待答与无法核对的旧写入结果不自动放行。
- 历史云端 ID 和本地哈希基线在扫描计数前排除，避免大量历史文件耗尽新录音批次；历史云端文件无需每轮重新读取音频字节。

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

独立插件地址为 `http://localhost:43141/plugin/`，0.1.4 已实际加载，并通过真实文档创建、正文写入、重复请求与原 ID 迁移回读。见 [插件说明](remnote-plugin/README.md)。日记模板已显式同步为 19 个有效分支；待办目标由 `sync-targets` 单独同步。本任务后续需要重载插件时，可按用户明确授权重启 RemNote，再核验实际版本和连接。

## 按失败环节修复

使用 `python -m async_workflow.cli repair <recording_id> --from split|routing|publish` 提交原生 Prefect 修复 flow。`split` 复用原 ASR，仅为重新分出的内容生成大纲；`routing` 保留已接受正文，只重新选择去处并迁移；`publish` 保持原目标，仅修复正文输出格式。三个入口都不重新转写，也不自动重放命令。

修复前快照和结果保存在该录音的 `repairs/<operation>/`；迁移前比对原正文、父链和已有收据，拒绝覆盖人工改动。旧元数据与被替代的本流程输出可恢复地移入根级 trash。失败后使用 `cli retry <flow_run_id>` 恢复同一修复，避免另起重复操作。`scripts/verify_readbacks.py` 可将独立读取的 RemNote 树与结果大纲、原 ASR 哈希逐项核对。

小米账户已扫码成功并核验云端访问；按明确账户 ID 绑定，保留旧失效账户。五分钟音频下载任务已启动，440 条历史录音基线已保存，全部原名、字节数与 SHA-1 一致；该下载器的录音路径使用全量元数据枚举及已有文件跳过，不能把相册的游标增量能力推广到录音。已按真实样本补齐月-日-年文件名格式。公开混合录音与断线恢复通过后，仍需一条真实新增手机录音验证上传到归档的完整链路，正式入口继续关闭。目标不确定的低优先级 RemNote 清单后置，明确指向既有 Thread 名称的完整解析尚未完成。

完整 Spec 和当前验收分别位于 `D:/Artifacts/AsyncWorkflow/2026-10-02/02-架构与规格草案.md`、`05-部署与验收.md`、`06-AppServer-实测.md`。
