录音命令的现役连接入口是 src/async_workflow/codex_appserver.py。执行和精确回读调用 sdk_config(..., via_wakewire=True)，通过官方 SDK 的 launch_args_override 启动 WakeWire codex-session；Thread/Turn 操作仍由原生 Codex App Server 完成。

本次接入只改变传输。录音正文、执行授权判断、分块、原 Thread/Turn ID 持久化顺序、命名、Project 参数、已有 Thread 续接及 Unknown 不重放规则继续沿用原代码。SDK 请求结束后，WakeWire 关闭所属私有 App Server，保留常驻服务和真实 Thread。

ASR、Codex Exec 内容生产、RemNote 归档、固定维护 Thread 的原生队列及独立 command_inbox 没有切换。sdk_config 的默认连接仍为原路径，只有录音执行器显式选择 WakeWire。

默认安装：D:/Applications/WakeWire/node_modules/wakewire/dist/cli.js；运行目录：D:/AppData/WakeWire；Windows 后台任务名：WakeWire。原 cfg.codex 及既有的受管理运行时路径解析继续生效，模型与工作目录不变。WAKEWIRE_NODE、WAKEWIRE_CLI、WAKEWIRE_HOME 允许显式覆盖以运行隔离验收。WakeWire 不可用时沿原错误/收据路径报错，不回退直接创建，也不重放未知提交。

完整接口与运行维护合同位于 [WakeWire workflow sessions](/D:/Projects/WakeWire/docs/workflow-sessions.md)。新开发 Thread 先读本文件和当前源码，再读取 D:/Artifacts/WakeWireIntegration/2026-10-30 的验收记录；不要把早期模拟样本当作本工作流的生产数据。

针对性验证：tests/test_wakewire.py、tests/test_appserver.py、tests/test_codex_runtime.py；维护反馈与命令收件箱使用原回归测试确认边界。真实验收用独立 Manifest 和合成正文调用 executor.execute，不重新转写、分类或归档历史录音。
