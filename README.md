# GPT Chat Helper

基于 [jev-chat-windows](https://github.com/jev-chat/jev-chat-windows) v0.1.12 修改。
使用官方 ChatGPT 登录授权，由所选的同一个 GPT 完成意图判断、紧张度评估、三条候选回复及推荐选择。
也可主动生成三条聊天开场消息，按需要填写话题方向。
不再要求填写 Jev、OpenRouter 或 DeepSeek 的 API Key。

## 启动及首次登录

1. 双击项目目录的 `Start-JevChat.cmd`，打开修改后的 GPT Chat Helper。
2. 在设置页点击 **Continue with ChatGPT**。
3. 在系统浏览器中选择自己的 ChatGPT Plus/Pro 账号及工作区，授权此应用使用订阅额度。
4. 回到程序，等待获取该账号可用的模型；选择一个 GPT，判断和起草共用这个模型。
5. 调整关系、说话风格及上下文条数，保存；打开微信或 KakaoTalk 会话。
6. 要回复历史消息时，把消息显示在窗口中并点击“生成回复”；需要自动处理新消息时，再开启采集。

收到对方的新消息后，程序先进行本地 OCR，再将所选上下文交给 GPT。
“填入”只把回复放进聊天输入框，发送仍由你手动确认。候选没有虚构的胜出概率。

账号切换或模型变更后，旧候选失效，采集会暂停；重新开启后使用新设置。
点击“管理用量”可打开 ChatGPT 用量及应用授权设置。
遇到授权限制或额度用尽时，程序暂停采集，显示原因；不会改用 API Key 付费调用。
订阅授权、可用模型及额度以账号/工作区的实际返回为准。

## 手动生成历史消息的回复

“生成回复”会重新读取当前聊天窗口的可见文字，按“参考上下文”条数生成候选。
它不依赖新消息，也不要求最后一条消息来自对方；已经读过的消息仍可再次生成。
暂停自动采集时，按钮仍支持单次读取，完成后继续保持暂停。
生成期间按钮暂时禁用；未打开聊天窗口、未读到文字或读取超时时，会显示原因并允许重试。
需要更早的历史上下文时，请先在聊天软件中滚动，让这些消息显示出来。

## 主动生成话题

打开目标会话，点击 **生成话题**，即可根据当前可见聊天、关系和说话风格生成三条开场消息。
“话题方向”可填写“周末安排”“电影”等，也可留空让 GPT 自行选择。
无需收到新消息，最后一条是自己发的也能生成；当前窗口读不到历史文字时，按方向和关系生成通用开场。
话题模式显示推荐开场和推荐理由；候选可复制或填入聊天输入框，发送仍手动。
暂停采集时也可单次使用，生成期间两个按钮及话题输入框暂时禁用。
自动采集仍只为新消息生成回复，不会自动开启话题。

## 微信窗口识别

程序通过会话头部和输入框边界定位消息区，读取主窗口右侧或窄版聊天面板中的气泡文字。
联系人列表、搜索栏和未发送草稿不作为聊天历史。只有联系人列表而未打开会话时，程序会提示打开目标会话。
切回列表或无法定位消息区时，已生成候选及填写位置失效，避免把旧候选填进错误区域。
浅色及深色主题均已加入真实 OCR 的合成样例验证。
小号英文或特殊字体的会话名称漏检时，会放大头部画面重试；右侧工具按钮及返回箭头不会用作会话名称。

## 文件

- 修改后的程序：`gpt-chat-helper-v0.1.12-chatgpt.5/gpt-chat-helper.exe`
- 启动脚本：`Start-JevChat.cmd`
- 原版程序保留在：`jev-chat-windows/`
- 修改源码：`source/`
- 部署及验证状态：`deployment.json`

普通使用无需安装 Python。

## 登录凭据与数据

登录使用官方 OAuth 授权码、PKCE、state、nonce 和 ID-token 签名验证。
访问与刷新凭据保存在当前 Windows 用户的
`%LOCALAPPDATA%\GPTChatHelper\credentials.bin` 中，由 Windows DPAPI 加密。
凭据不会写入项目配置、日志或 Git；不读取其他应用的登录文件或浏览器 Cookie。
同一设备保留稳定的 host ID，账号注册分开保存，令牌更新串行且原子写入。

截图和 OCR 在本机完成。自动或手动生成建议时，上下文、关系、说话风格、话题方向及近期自己的短句样本会发给 OpenAI。
请求使用官方 Responses 接口，设置 `store=false`；这并不代表服务端所有数据处理/保留政策均被关闭。
退出登录会尝试撤销刷新会话，清除本地令牌，并保留账号注册信息供再次登录。

## 源码开发及验证

在 `source` 目录执行：

```powershell
uv venv --python 3.12 .venv
uv pip sync --python .venv\Scripts\python.exe requirements.lock.txt
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe main.py
```

打包：

```powershell
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean jev.spec
```

53 项离线测试覆盖登录校验、凭据加密、刷新/撤销、订阅授权、流式输出，以及历史消息快照、暂停时单次生成、主动话题、空历史、重复点击和错误恢复。
窗口识别测试包含联系人列表拒绝、相同底色的左右面板、窄版聊天、缩放，以及真实 OCR 对气泡/联系人/草稿的区分。
真实登录和实际 GPT 调用需要本人完成浏览器授权后验证，状态记录在 `deployment.json`。

官方接入文档：
https://developers.openai.com/siwc/token-sharing-open-source/sign-in

## 项目出处

Based on jev-chat-windows: https://github.com/jev-chat/jev-chat-windows
Based on Jev Chat Assistant: https://github.com/jev-chat/jev-chat-jarvis

本修改版由本地源码构建，并非原作者发布的官方版本。
原始 LICENSE 和 NOTICE 保留在源码及程序目录中。
