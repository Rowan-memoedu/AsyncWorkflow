# RemNote 一次性命令：外部自动化规格

## 已确定行为

- 外部脚本接入已经运行的本地 Prefect runner，每 1800 秒读取 `codex命令 / 一次性命令`。不新增 Codex 定时任务、Windows 计划任务或另一套后台进程。
- 直接子节点之间的空节点分隔命令；子树属于同一条命令。只有第一个非空节点以 `%` 开头的块可以递交。不按语义猜测，不优化草稿，不处理同文档的“项目”分支。
- 每条命令通过官方 App Server `thread/start` 创建全新 Thread，再通过 `turn/start` 递交原文。保留 `%`、文字、空格和节点内部换行，子树只增加确定的缩进。富文本通过 RemNote SDK 转为 Markdown。卡片背面暂不支持，发现时明确报错，禁止漏字后继续。
- 不 fork 当前 Thread，不附加其对话、摘要、解释、提示前缀或输出 schema。标题直接截取原文第一行。正常用户全局配置与工作目录 AGENTS.md 仍由 Codex 自己读取，不属于本工作流注入的上下文。
- 模型和推理强度来自文档中的 `一次性命令当前要求使用的模型：<model> <effort>`；`6.1-sol high` 对应 `gpt-6.1-sol` 与 `high`。递交前通过当前账号的 model/list 验证，不自动替换模型。其他机械规则变更需人工检查，不让脚本猜测新规则。
- 收到并持久化真实 Thread ID 和 Turn ID 后，马上将整块原 Rem 移到“已递交”，保留 ID、子树和空行。无需等待命令执行完毕。后台 worker 保持 App Server 连接直到该回合结束。
- 原工作流的录音入口开关独立，不因启用本功能而打开。

## 拼接与文件边界

- `src/async_workflow/command_inbox.py`：确定性分块、规则校验、Prefect scan/delivery deployment、官方 SDK 原文投递。复用 `core.Manifest`、`codex_appserver.sdk_config` 和独立 `remnote` 客户端，不调用录音 Prompt 包装函数。
- `remnote-plugin/src/command-inbox.ts`：只增加 `command_inbox_read` 和 `command_inbox_archive`；原插件 API 和原 MCP 均不修改。通过 SDK 移动现有 Rem，不复制/删除用户命令。
- `src/async_workflow/flows.py`：在原有 runner 注册两个新增 deployment，复用既有后台服务及其启动方式。
- 私有配置 `D:/AppData/AsyncWorkflow/command-inbox.json` 保存 exact Rem ID、工作目录、规则指纹、独立开关和扫描间隔。
- 私有输入与递交凭据位于 `D:/AppData/AsyncWorkflow/command-inbox/items`，最近扫描在 `last-scan.json`；不提交到 Git。Prefect 承担调度状态；App Server 承担原生 Thread 状态，来源凭据不模拟 Thread 管理。

## 防重复与失败处理

- 来源身份为 document ID 加命令首 Rem ID，同一来源最多递交一次。前置读取失败可在后续扫描重试；已经开始 thread/start 或 turn/start 的未知结果不会盲目重放。
- 单条锁避免并发投递，Prefect idempotency key 避免响应丢失重复排队；递交前重新读取，原文、子树或规则已变化则本次不发送。
- Thread ID 在 turn/start 前持久化，Turn ID 在归档和等待执行前持久化。后续只读核对已知回合，不抢占用户桌面接管，不自动追加消息。
- 归档失败不重发命令，保留待移动凭据。移动有插件端操作记录，逐节点校验并回读，可恢复部分移动。已有命令的残留节点也被标为已占用，不能拆出来再次递交。
- 连接断开、规则未知、源内容被改动或移出边界均可见地停止相关步骤。没有 MCP/GUI/本机数据库兜底。
- 关闭 `command-inbox.json` 的 `enabled` 可停止新的投递；不需要停止共享 runner，已有 Thread 和 RemNote 命令不会被删除。

## 验收

1. Python 回归检查精确原文、没有上下文/schema、真实 ID 持久化顺序、未知结果不重发、草稿不执行、部分移动不产生新命令。
2. 插件检查原节点/子树/顺序保留、移动响应丢失恢复、源修改和跨文档拒绝、分隔符不重复及模板缓存不受影响。
3. 运行态需读取新插件的 `command-inbox-1` 协议成功，既有 Prefect runner 中的 deployment 显示 1800 秒周期，并完成一次实际扫描。构建通过不等于插件已经在 RemNote 中加载。
4. 当前无 `%` 新命令时，不制造额外真实任务。首次真实新命令递交后应对照 App Server Thread 首条输入与保存的原文校验和，并检查原 Rem 已移动；在此之前不宣称全链路递交已经验收。

官方协议参考：[Codex App Server](https://learn.chatgpt.com/docs/app-server)。
