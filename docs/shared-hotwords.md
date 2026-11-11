# 录音热词

`asr_preference_profile` 可指向 AudioWorkflow 发布的统一 `current.json`。该选项不改变已经成功的录音结果。

- 普通录音经 global-transcription 传入完整 profile，使用 Fun-ASR 原生词表；归一化兼容 `fun-asr` 文件结果和历史 `fun-asr-realtime` 结果。
- 独立通话声道也经同一文件识别入口，读取服务原始词级时间戳；没有真实词级时间戳就停止，不用句子均分伪造词时间。已成功声道分片继续复用。
- 现场录音的异步文件任务在提交前验证 profile 与发布收据相符，并使用该版本的 `fun-asr` 词表 ID。说话人分离参数、人物映射和提交未知状态的处理不变。
- 更新共享 profile 后下一次新请求使用新版，任务已提交时仍沿用其原收据。热词失败不会默默丢弃词表或跨供应商重试。

global-transcription 文件热词依赖 `oss2==2.19.1`，已纳入本项目依赖。部署后使用公开短音频分别验证普通入口和声道入口，再重载 runner、watcher；重载前现有脚本会检查活跃 flow。
