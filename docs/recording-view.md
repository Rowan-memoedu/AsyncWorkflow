# 录音去向

电脑浏览器与手机分类录音 App 的右侧页面共用 `https://124.221.144.155/recordings/`。底部导航和左右滑动由 Android 原生页面承载，网页负责查询和查看。电脑端不提供录音入口。

## 数据与同步

电脑上的 AsyncWorkflow 继续负责小米录音接收、转写、整理与归档。`AsyncWorkflow-mirror` 在当前 Windows 用户登录时启动，每次同步完成后等待 60 秒，再导出完整查看快照。同步失败只影响网页的新鲜度，不阻塞处理后台。

快照包含录音列表、转写、正文分块、实际归档路径与节点、处理状态和 Thread 链接。不包含原音频、本机文件路径、完整 manifest、运行配置或平台凭据。扫描不完整时保留服务器上一份结果；服务端先校验完整结构，再原子替换快照，拒绝旧时间戳、重复 ID 和超过 32 MiB 的更新。

电脑关机后，服务器继续显示最后同步结果。超过三分钟没有同步时网页明确提示；这不代表新录音已经处理。没有转写的录音仅能按文件名搜索。服务器进程重启后从磁盘恢复快照。

## 访问与凭据

HTTPS 网页、静态资源和查询接口均要求登录。电脑与手机使用不同的只读访问密钥；同步器使用另一枚仅可发布快照的密钥，不能用于读取页面。所有密钥随机生成，服务器只保存 SHA-256 摘要。

本机管理材料位于 `D:/AppData/RecordingView`，同步器连接文件为 `D:/AppData/AsyncWorkflow/mirror-connection.json`。这些文件不进入 Git。手机密钥通过应用专属目录临时导入，随后转存不可备份的私有目录，不编入 APK。电脑登录材料保存在本机 `电脑登录.txt`，首次访问网页时填写。

## 服务与部署

- 独立源码模块：`mirror.py`、`mirror_server.py`、`mirror_sync.py`；网页资源与本地查看页共用。
- 腾讯云服务：`recording-view.service`，专用无登录系统用户，监听 `127.0.0.1:43143`。
- Nginx 仅新增 `/recordings/` 路由，沿用现有有效 HTTPS 证书；不记录该路由的访问日志与搜索参数。
- 服务源码位于 `/opt/recording-view/releases`，`app` 符号链接指向部署版本，依赖位于独立虚拟环境。
- 查看数据与凭据摘要位于 `/var/lib/recording-view`，不在 Web 静态目录中。

在 Windows 仓库运行 `python scripts/deploy-recording-view.py`。它沿用现有 SSH 管理入口，打包明确列出的源码与静态资源，拒绝隐式轮换现有服务凭据；先验证 Nginx 和后台，再重载 Nginx。部署信息记录在 `/opt/recording-view/deployment.json`。

本机通过 `scripts/install-startup.ps1 -Roles mirror -Start` 注册同步器。单次核对用 `.venv/Scripts/python.exe -m async_workflow.mirror_sync --once`。状态见 `D:/AppData/AsyncWorkflow/mirror-status.json`，只记录时间、数量、字节数和错误类型。重载仅用 `scripts/restart-services.ps1 -Roles mirror`。

## 验证与恢复

页面分别显示最近同步与最近处理时间。处理时间取自转写起止、归档收据与任务实际起止，不使用镜像同步、恢复扫描或页面读取时间。录音排序使用带时区时间的实际时间戳；分类录音的显示时间优先采用合法 QVR1 文件名中的录制时间。此修正仅用于查看投影，不修改原始处理记录或归档结果。

Python 回归入口为 `tests/test_dashboard.py` 和 `tests/test_mirror.py`。网页验收脚本 `scripts/verify-recording-view.cjs` 使用隔离的无界面 Chrome，不连接用户浏览器配置，覆盖五种宽度、正文搜索、详情返回、登录和过期同步提示。Android 回归位于 QuickVoiceRecorder 的 `DestinationNavigationTest` 与 `scripts/verify-destinations.ps1`。

停止镜像同步不会停止录音处理。撤回云端服务时，停用 `recording-view.service` 并撤掉专属 Nginx include；本地查看页仍可使用。源码回退使用已有 release，快照保留在独立数据目录；若服务器副本丢失，可从电脑重新同步，不重跑转写或归档。
