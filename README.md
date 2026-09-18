# 微信群聊 AI 总结助手

面向 64 位 Windows 10/11 与微信 4.x 的本地只读群聊统计和 AI 总结工具。双击 EXE 后会使用 Windows 默认浏览器打开本地看板。

## v0.1.1 修复

- 修复关闭看板后后台进程残留、再次双击无窗口的问题。
- 增加单实例检测：应用已运行时会重新打开现有看板。
- 启动失败时显示中文错误窗口，并将诊断信息写入 `%LOCALAPPDATA%\WeChatAISummary\logs\app.log`。
- 用户设置、统计数据和导出文件统一保存到 `%LOCALAPPDATA%\WeChatAISummary`，发布包不再夹带开发者数据。
- 修复 API Key 误填到 API 地址后无法获取模型的问题；硅基流动会恢复官方地址并加密保存 Key。
- API 地址同时兼容基础地址、`/models` 完整地址和 `/chat/completions` 完整地址。

## v0.1.2 读取与交互优化

- 移除消息量、Token 和预计调用次数的估算步骤。
- 选择群聊、时间范围和 AI 平台后，可一键直接开始总结。
- 实时显示排队、读取聊天记录、分段调用 AI、合并结果和完成进度。
- 改为按起止时间直接跨分片查询，避免从最新消息反向分页时被新消息插入干扰。
- 兼容微信 4.x 复合文字类型，并在数据库错误时明确失败而不是静默返回空列表。
- 优先扫描微信私有内存区域并并行解密大型消息库，缩短首次读取等待时间。

## v0.1.3 默认浏览器与总结前置校验

- 移除 Edge/Chrome 独立应用窗口，统一通过 Windows 默认浏览器打开看板。
- 再次双击程序会在默认浏览器中打开已经运行的实例；结束使用时请点击看板右上角“退出”。
- 在读取微信消息前检查所选平台是否已保存 API Key、是否已选择模型，避免长时间读取后才失败。
- 未配置完整的平台会在下拉框中明确标注，并在开始总结时自动打开设置面板。

## v0.1.4 平台配置简化

- OpenAI、DeepSeek、通义千问、智谱、豆包、硅基流动固定使用内置官方 API 地址，不再显示或接受地址填写。
- 只有“自定义兼容接口”会显示 API 地址输入框；内置平台只需填写 API Key，再获取或手动选择模型。
- 自动修复旧版本中保存的硅基流动控制台、登录页或错误地址，避免 `HTTP 307` 跳转登录。
- 硅基流动在读取微信前先检查 API、Key 与网络连接，失败时不再解密或读取聊天消息。
- 将连接拒绝区分为代理未启动和目标接口未启动，给出可直接处理的中文提示。

## v0.1.5 发言人名称修复

- 解析微信 4.x `chat_room.ext_buffer` 中的群内昵称，名称优先级改为：群内昵称、通讯录备注、微信昵称、成员 ID。
- 自己发送的消息也优先显示所在群的群内昵称，不再固定显示账号昵称。
- 发言统计改为按发送者 ID 聚合，避免同名成员或多个“未知成员”被错误合并。
- 已保存账号和群聊时，程序启动后自动连接微信并恢复实时读取；无需每次手动点击开启。

## v0.1.6 群昵称映射修复

- 修复微信 4.x 将 `real_sender_id` 误当作全局 rowid 导致的发言人与昵称错配；群消息改用正文内嵌用户名定位成员，并在交给统计和 AI 前移除内部用户名前缀。
- 兼容当前微信 4.x 使用发送者编号 `3` 标记本人群消息，自己发言同样优先显示该群设置的群昵称。
- 个别消息缺少内嵌用户名时，只复用同一群内已经验证过的“内部编号—成员”对应关系，避免跨群串名或显示为未知成员。

## v0.1.7 多账号兼容修复

- 兼容微信 4.x 不同版本中缺少 `chat_room.owner`、成员表不完整或群聊仅存在于联系人/会话索引的情况。
- 账号目录使用微信号别名时，通过联系人 `alias` 解析真实内部账号 ID。
- 群聊枚举失败时回滚数据库连接并显示明确错误，不再出现“已连接 undefined”的半连接状态。
- 本地接口异常会写入 `%LOCALAPPDATA%\WeChatAISummary\logs\app.log`，便于定位其他电脑上的数据库结构差异。

## 功能

- 用户选择当前微信账号以及一个或多个群聊。
- 从微信 4.x 本地 SQLCipher 数据库读取历史文字消息，并增量读取新消息。
- 保留发言排行、字数、活跃时段、实时消息流和 Excel 手动导出。
- 支持任意起止时间，按群分别生成固定结构总结。
- 长记录按 Token 分段总结，再合并成最终结果。
- 点击开始后直接在后台读取消息并调用 AI，持续显示处理进度。
- 支持 OpenAI、DeepSeek、通义千问、智谱、豆包、硅基流动及自定义兼容接口。
- 支持多个平台配置、模型列表获取、手动模型名和 HTTP/HTTPS 代理。
- 支持应用运行期间的每日增量总结与 Windows 通知。
- 支持一键复制、Markdown/TXT 下载，以及签名更新检查。

## 隐私设计

- 微信数据库密钥每次启动从当前微信进程重新取得，仅驻留内存，不写入磁盘。
- 解密工作副本位于系统临时目录，断开连接或退出应用时清理。
- 原始消息正文和 AI 总结不写入应用数据库。
- 应用仅持久化真实群名、成员昵称、每日条数/字数/时段等聚合统计，以及最小化调度位置。
- API Key 和代理密码使用 Windows DPAPI 绑定当前 Windows 用户加密保存。
- 云端调用会发送用户确认范围内的原始昵称与文字消息；具体服务商的数据留存规则由服务商决定。
- Excel、Markdown 或 TXT 只有用户主动导出时才会写入磁盘。

本工具不注入、不修改微信进程，只读取本机当前用户明确授权的数据。微信升级可能改变数据库结构；请勿将“只读”理解为对账号风险或兼容性的绝对保证。

## 源码运行

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe gui_app.py
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

真实微信测试默认跳过。只有在用户明确授权了测试群和时间范围后，才可设置 `WECHAT_REAL_TEST=1` 运行 `tests/test_integration.py`。测试不得输出正文。

## 构建便携版

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_portable.ps1
```

产物位于 `dist/WeChatAISummary/` 和 `dist/WeChatAISummary-v<版本>-windows-x64.zip`。

## 签名更新

首次开发时运行 `tools/generate_update_keys.py` 生成 Ed25519 密钥。私钥位于被 Git 忽略的 `.secrets/`，绝不能提交或上传。发布 ZIP 后使用：

```powershell
.\.venv\Scripts\python.exe tools\build_update_manifest.py `
  --asset "dist\WeChatAISummary-v0.1.0-windows-x64.zip" `
  --url "https://github.com/Mole-33/-/releases/download/v0.1.0/WeChatAISummary-v0.1.0-windows-x64.zip" `
  --version "0.1.0" --adapter-version "wechat4-1.2.2.3" `
  --private-key ".secrets\update-signing-key.pem" `
  --output "dist\update-manifest.json"
```

把 ZIP 与 `update-manifest.json` 一起上传到 GitHub Release。应用只会下载签名有效且 SHA-256 匹配的更新包。

## 第三方组件

微信 4.x 数据库兼容层基于 Apache-2.0 许可的 [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica)，详见 `THIRD_PARTY_NOTICES.md` 和 `vendor/LICENSE.wechatauto-replica`。

## License

MIT
