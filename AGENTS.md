# AsyncWorkflow

Windows 本地异步录音处理：Prefect 编排，FUN-ASR 转写，冻结 prompt 整理，独立 RemNote 插件和官方 Codex App Server 输出。

- 源码只在此仓库；运行数据 D:/AppData/AsyncWorkflow，产物 D:/Artifacts/AsyncWorkflow，缓存 D:/Caches/AsyncWorkflow。
- 运行入口保持 disabled，直到独立 RemNote 插件和小米新增录音端到端通过。服务运行不等于功能验收。
- 不修改或连接现有 RemNote MCP。插件子包独立依赖、构建和协议；只实现本流程功能。
- 日记模板只在显式 sync-template 时读取。录音按 Asia/Shanghai 的录音日归档；无目标按模板语义分类，整体跨类别或无类别放其他碎片。
- 显式文档名模糊搜索后使用首选，无候选放其他碎片；高不确定清单后置，不阻塞归档。
- 明确要求新建 document 时创建真实文档；未说明父目录时放知识库根级，不模糊替换为无关旧文档。话题结束后不继承前一段目标。
- 学习日志仅收明确要求归入或实际具体学科学习内容；每日碎碎念收具体学习经历，认知科学相关收学习过程中涌现的认知思考，一些注定重复的思考收具体学科知识思考。按实际使用语义区分私密日记与其他碎片。
- 购物、软件信息与杂事待办分别进入待办清单的购物清单、信息清单对应软件和清单根级；使用显式同步的目标快照。
- “放到某软件消息里”的拟发消息/回复草稿属于信息清单；明确覆盖后续全部内容的归档说明跨话题保持，直到结束该范围或指定其他去处。不把草稿归档当作对外发送。
- RemNote 只写结构化原文；来源、时间和路由元数据只留日志。修复仅从失败环节继续，沿用成功 ASR 和未受影响的大纲；只迁移本插件拥有且正文未被修改的节点，保留原 ID。
- 新命令使用官方 openai-codex SDK 调用 app-server，及时保存真实 Thread/Turn ID。未知提交不重放；桌面接管后不抢占已有写入者。
- document 与 Thread 均先分离创建、命名、定位等承载操作和正文；正文沿用冻结结构化提示词。Codex 收到整理后的正文，实际执行要求、否定和限制仍属于正文。
- 新 Thread 固定在当前“新建文件夹”工作区及对应官方 Project；工作目录正确不等于项目归属正确。用官方 project/read 与 thread/metadata/update 验证、绑定和读回，再提交任务；不操作应用数据库。
- 默认复用 global-transcription 的 FUN-ASR 文件接口。已确认转写不重跑；已确认替代结果经 source hash 和旧动作核对后导入，只续跑下游。
- 个人饮食、睡眠、状态、运动时长与身体感受等当天事实归入“私密 日记 / 个人数据”；自我表达与完整反思仍为私密日记。
- 每个 RemNote 正文块前后各写一个同级空节点，程序保证顺序、幂等和迁移复用；不得覆盖用户写入空节点的新内容。用户确认保留删除的旧输出不恢复。
- 脚本失败且没有 Thread 时才新建修复 Thread；已有 Thread 直接续接。
- 保留源录音、原 prompt、人工编辑和外部动作记录。新分析不自动重放副作用。
- 密钥、音频、私人模板、冻结私人 prompt、运行数据和本机配置不得提交 Git。
- 测试：`.venv/Scripts/python.exe -m pytest -q`；插件：`npm run check && npm test && npm run build`（在 remnote-plugin）。Windows 实际命令使用 pwsh 和进度超时工具。
- 当前事实和待验收项见 README；完整用户 Spec 在 D:/Artifacts/AsyncWorkflow/2026-10-02/02-架构与规格草案.md。
