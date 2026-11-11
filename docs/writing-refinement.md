# 写作与非写作的正文整理

普通单人录音在整理正文前，由 `gpt-6-luna` 的 Decisions API 判断内容用途，再由 Responses API 按对应提示词整理。正文默认仍使用原来的 `gpt-6-luna`，可单独配置生成模型。语义拆分、去处判断、执行授权和多人会议整理保持原路径。

| 内容 | 整理方式 |
| --- | --- |
| 任务、需求、步骤、约束、清单、日程、事实条目 | 沿用层级结构化，保留全部要求及限定 |
| 观点展开、思考、叙述、口述文章、消息或发言正文 | 保留自然句式、副词、语气、比喻、个人用语和展开过程 |
| 实质写作与命令混合 | 整份先按写作保护，既有语义拆分随后分别处理 |
| 不确定，或非写作判断 confidence 小于 0.75 | 按写作保护；这是保守策略，不是准确率声明 |

“请写文章”的任务说明可以是非写作；已经口述出来的正文是写作。不能只根据技术词、第一人称、命令动词或“写作”一词选择路线。

非写作正文直接使用原强结构化提示词，主动清理口语、合并重复、重组层级。写作正文使用独立的保真提示词，保留原有句式、语气和表达。直接向助手提交改进反馈属于非写作。两条路线都输出 H1 标题和嵌套 bullet。

## 提示词

- [决策](../src/async_workflow/prompts/decide-writing.md)
- [写作保真与格式规则](../src/async_workflow/prompts/refine-common.md)
- [非写作整理](../src/async_workflow/prompts/refine-non-writing.md)
- [写作整理](../src/async_workflow/prompts/refine-writing.md)

提示词按语义定义规则，没有固定领域、人物、项目或测试案例映射。测试的预期路线和检查词保留在测试 fixture 中，不传给模型。

## 启用、记录与恢复

仓库外的现有配置设置 `refinement_policy: "content_routed"`；可配置 `refinement_model`、`refinement_reasoning`、`openai_env_file`。初始默认 `legacy` 便于受控启用，已部署机器的实际选择以 `doctor` 输出为准。密钥依次读取进程 `OPENAI_API_KEY`、`OPENAI_ENV_FILE` 指向的文件或配置中的共享文件；不写入仓库、不打印、不修改环境。

本机已确认的共享文件是 `D:/AppData/OpenAI/.env.local`。Decisions 使用官方 `/v1/decisions`；正文使用 `/v1/responses`，`store=false`、禁用输入截断。现有固定 SDK 尚无 Decisions 专用方法，因此通过其通用 POST 方法调用官方 REST 接口，不升级运行时。

每个决定保存模型原始选择、概率分布、confidence、实际路线、耗时、用量和来源哈希；生成记录保存所用提示词哈希、实际响应及决定收据。记录都在仓库外。相同输入与决策规则复用已验证决定；正文缓存同时绑定提示词、决策规则、生成模型和推理配置。

接口错误、拒绝、非法输出或未完成响应直接报告失败，不悄悄切模型、不把错误当成非写作。格式错误只从原始转写重新生成，不使用被压缩的候选稿作为来源。

原 `refine.md` 与其校验哈希保留。恢复时将 `refinement_policy` 改回 `legacy`，按现有 `scripts/restart-services.ps1` 重载 runner/watcher。新旧正文缓存相互隔离；已接受的 segments 和已完成副作用继续复用，不因切换路由重放。会议路线继续用原冻结提示词。

## 验证

离线回归：`python -m pytest -q`。

真实 API 隔离验证：`python scripts/refinement_canary.py --output <新的 D 盘产物目录> --split development`，随后对未参与修订的独立样本运行 `--split holdout`。素材全部是公开合成文本，不上传私人录音，不调用 ASR、归档或执行器。验证内容包括路线、原始自然表达和程度词的保留、口癖删除、机械重复合并、格式及错误处理。小样本结果只证明这些样本，不代表所有真实录音的准确率。

官方资料：[Decisions](https://developers.openai.com/api/docs/guides/decisions)、[接口定义](https://developers.openai.com/api/reference/resources/decisions/methods/create)。
