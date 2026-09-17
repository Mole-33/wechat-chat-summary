# 微信群聊 AI 总结助手

面向 64 位 Windows 10/11 与微信 4.x 的本地只读群聊统计和 AI 总结工具。双击 EXE 后以本地浏览器看板运行。

## v0.1.1 修复

- 修复关闭看板后后台进程残留、再次双击无窗口的问题。
- 增加单实例检测：应用已运行时会重新打开现有看板。
- 启动失败时显示中文错误窗口，并将诊断信息写入 `%LOCALAPPDATA%\WeChatAISummary\logs\app.log`。
- 用户设置、统计数据和导出文件统一保存到 `%LOCALAPPDATA%\WeChatAISummary`，发布包不再夹带开发者数据。
- 修复 API Key 误填到 API 地址后无法获取模型的问题；硅基流动会恢复官方地址并加密保存 Key。
- API 地址同时兼容基础地址、`/models` 完整地址和 `/chat/completions` 完整地址。

## 功能

- 用户选择当前微信账号以及一个或多个群聊。
- 从微信 4.x 本地 SQLCipher 数据库读取历史文字消息，并增量读取新消息。
- 保留发言排行、字数、活跃时段、实时消息流和 Excel 手动导出。
- 支持任意起止时间，按群分别生成固定结构总结。
- 长记录按 Token 分段总结，再合并成最终结果。
- 运行前显示消息数、估算 Token 和预计 API 调用次数。
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
