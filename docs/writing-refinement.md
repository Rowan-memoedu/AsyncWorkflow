# 写作与非写作的正文整理

普通单人录音先按原文确定整体去处。Document 有直接写作或非写作 tag 时跳过 Decision；没有预设类型时，由 `gpt-6-luna` 的 Decisions API 判断内容用途，再由 Responses API 按对应提示词整理。写作与非写作正文均使用已确认的 `gpt-6.1-sol`、中等推理。只有明确要求分割才生成多个独立记录，执行授权和多人会议整理保持原路径。

| 内容 | 整理方式 |
| --- | --- |
| 任务、需求、步骤、约束、清单、日程、事实条目 | 沿用层级结构化，保留全部要求及限定 |
| 观点展开、思考、叙述、口述文章、消息或发言正文 | 允许自然改写为完整正文，保留有效信息、立场、整体语气与风格，细化节点并统筹全文 |
| 实质写作与命令混合 | 无预设类型时整份按写作保护；没有明确分割要求时保持整体记录 |
| 不确定，或非写作判断 confidence 小于 0.75 | 按写作保护；这是保守策略，不是准确率声明 |

“请写文章”的任务说明可以是非写作；已经口述出来的正文是写作。不能只根据技术词、第一人称、命令动词或“写作”一词选择路线。

任务参数中的字段值、标题、术语、代码、操作例句或反例引用，不因要求原样保留就变成写作；按任务目的选非写作，引用由强结构化规则保护。已经口述的拟发消息或其他表达性正文，即使简短或加引号也仍是写作。

非写作正文使用已验证的通用强结构化规则，主动清理口语、合并重复、重组层级；明确保留有效限定、引文、阶段、指代和交付要求。写作正文采用独立内容规则：先通读全文、清理重复、确定细致层级，再逐节点成稿和校订。允许补明已有指代、调整语序及组织纲领性总述；保留有效信息、立场力度与整体口吻。列举、观点与说明细拆，完整转折判断和短叙事按自然节奏组织。直接向助手提交改进反馈属于非写作。两条路线都输出 H1 标题和嵌套 bullet。

## 提示词

- [决策](../src/async_workflow/prompts/decide-writing.md)
- [后台输出格式及旧内容规则](../src/async_workflow/prompts/refine-common.md)：新写作分支只取“输出格式”部分
- [非写作整理](../src/async_workflow/prompts/refine-non-writing.md)
- [写作整理](../src/async_workflow/prompts/refine-writing.md)

提示词按语义定义规则，没有固定领域、人物、项目或测试案例映射。测试的预期路线和检查词保留在测试 fixture 中，不传给模型。

## 启用、记录与恢复

仓库外的现有配置设置 `refinement_policy: "content_routed"`；`refinement_model`、`refinement_reasoning` 保留写作及未覆盖路线的默认值。`refinement_non_writing_model`、`refinement_non_writing_reasoning` 仅覆盖最终选定的非写作路线；未配置时继承默认值。当前确认配置中，默认 `refinement_model` / `refinement_reasoning` 与非写作覆盖项均设置为 `gpt-6.1-sol` / `medium`；Decisions 仍使用 Luna。低置信非写作判断回到写作保护时，也使用写作模型。`doctor` 的 `generation_profiles` 显示两条路线的实际设置。

初始默认 `legacy` 便于受控启用；凭据文件仍通过 `openai_env_file` 指定。密钥依次读取进程 `OPENAI_API_KEY`、`OPENAI_ENV_FILE` 指向的文件或配置中的共享文件；不写入仓库、不打印、不修改环境。

本机已确认的共享文件是 `D:/AppData/OpenAI/.env.local`。Decisions 使用官方 `/v1/decisions`；正文使用 `/v1/responses`，`store=false`、禁用输入截断。现有固定 SDK 尚无 Decisions 专用方法，因此通过其通用 POST 方法调用官方 REST 接口，不升级运行时。

每个决定保存模型原始选择、概率分布、confidence、实际路线、耗时、用量和来源哈希；生成记录保存所用提示词哈希、实际响应及决定收据。记录都在仓库外。相同输入与决策规则复用已验证决定；正文缓存同时绑定提示词、决策规则、生成模型和推理配置。

接口错误、拒绝、非法输出或未完成响应直接报告失败，不悄悄切模型、不把错误当成非写作。格式错误只从原始转写重新生成，不使用被压缩的候选稿作为来源。

原 `refine.md` 与其校验哈希保留。恢复时将 `refinement_policy` 改回 `legacy`，按现有 `scripts/restart-services.ps1` 重载 runner/watcher。新旧正文缓存相互隔离；已接受的 segments 和已完成副作用继续复用，不因切换路由重放。会议路线继续用原冻结提示词。

## 验证

离线回归：`python -m pytest -q`。

真实 API 隔离验证：`python scripts/refinement_canary.py --output <新的 D 盘产物目录> --split development`，随后对未参与修订的独立样本运行 `--split holdout`。素材全部是公开合成文本，不上传私人录音，不调用 ASR、归档或执行器。验证内容包括路线、有效信息与立场力度、整体口吻、口癖和重复清理、细致层级、全文纠错、格式及错误处理。词面完全相同不作为新写作规则的验收条件。小样本结果只证明这些样本，不代表所有真实录音的准确率。

官方资料：[Decisions](https://developers.openai.com/api/docs/guides/decisions)、[接口定义](https://developers.openai.com/api/reference/resources/decisions/methods/create)。
