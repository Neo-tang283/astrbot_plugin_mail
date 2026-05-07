📬 邮件插件
本插件基于gangcaiyoule的astrbot_plugin_mail_notify插件修改，增加sent_smtp_mail函数工具，以及ai自动回复邮件功能

✨ 监控多个 IMAP 邮箱的新邮件，通过 AstrBot 自动推送通知，支持 AI 智能摘要，手动回复，支持ai自动回复。✨

✨ 核心功能

多邮箱监控 —— 同时监控多个 IMAP 邮箱（Gmail、QQ 邮箱、163 邮箱、Outlook、校园邮箱等）。
自动推送 —— 后台定时轮询（默认 5 分钟），仅推送插件启用后收到的新邮件，无历史旧邮件打扰。
自动回复 —— 在收到邮件时使用llm智能回复邮件
自然语言发送邮件 —— 指示ai使用函数工具发送邮件
智能过滤 —— 支持黑白名单规则（发件人、主题、正文关键词），精准筛选通知内容。
AI 摘要 —— 可选调用 LLM 对邮件内容生成简洁中文摘要，快速掌握邮件核心。
手动交互 —— 支持通过指令查询历史邮件、手动发送回复邮件（基于 SMTP）。
WebUI 配置 —— 在 AstrBot 管理面板中可视化配置，无需修改代码。

📖 快速开始

配置邮箱账户
在 AstrBot WebUI → 插件管理 → 📬 邮件通知 → 配置中，找到 mail_accounts 字段添加邮箱列表。
字段   说明   示例
name   账户备注名   qq邮箱

sender_name   邮箱发送名     Astrbot Mail

imap_server   IMAP 服务器地址   imap.qq.com

imap_port   IMAP 端口 (SSL通常993)   993

email   邮箱地址   123456@qq.com

password   密码/应用专用密码   xxxxxxxx

smtp_server   SMTP 服务器地址   smtp.qq.com

smtp_port   SMTP 端口 (SSL通常465)   465

smtp_use_ssl   SMTP 是否使用 SSL   true

smtp_password   SMTP 密码 (留空复用password)   xxxxxxxx

check_interval   轮询间隔(分钟)   60

ai_mode   是否启用 AI 模式   true

forward_to_user   是否转发原始邮件   true

blacklist_settings   黑名单规则配置   见下方示例

whitelist_settings   白名单规则配置   见下方示例

黑白名单配置示例：
{
  "enable": true,
  "sender_rules": ["noreply@", "qq.com", "广告邮件"],
  "subject_rules": ["促销", "优惠"],
  "body_rules": ["垃圾邮件"]
}

配置全局参数
在插件配置的根层级设置以下参数：
参数名   说明   必填
admin_uids   允许使用指令的管理员 ID 列表   ✅ 是

notify_umo   默认的通知目标会话 ID (可选，账户内也可单独配置)   ❌ 否

注意：notify_umo 的格式取决于你的平台，例如 QQ 私聊可能是 qq:123456。若账户内单独配置了 notify_umo，则优先使用账户内的配置。

验证运行
配置完成后，使用下方指令验证插件是否正常工作。

📋 指令列表

llm函数工具：sent_smtp_mail 支持llm使用工具进行smtp邮箱发送

权限说明：所有指令仅限 admin_uids 列表中的管理员使用。
指令   说明   用法示例
/mail_status   查看所有邮箱的连接状态和最近检查时间   /mail_status

/mail_check   立即手动检查所有邮箱的新邮件   /mail_check

/mail_query   查询指定邮箱自某日期以来的邮件（最多 20 条）   /mail_query qq邮箱 2026-03-01

/mail_reply   使用指定账户手动发送邮件回复   /mail_reply qq邮箱 test@example.com 回复主题 你好

🔍 /mail_query 用法详解
/mail_query  

：配置中 mail_accounts 里的 name 字段。
：格式必须为 YYYY-MM-DD。
效果：查询该邮箱自指定日期以来的邮件列表。

📨 /mail_reply 用法详解
/mail_reply   |

：配置中 mail_accounts 里的 name 字段。
：目标收件人地址。
|：使用英文半角竖线 | 分隔主题和正文。
效果：通过指定账户向目标发送邮件。

🔑 各邮箱授权码获取

重要：大多数邮箱需开启 IMAP/SMTP 服务并使用“授权码”而非登录密码。

QQ 邮箱
网页登录 QQ 邮箱 → 设置 → 账户。
在“POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务”中，开启 IMAP/SMTP 服务。
按提示发送短信验证，生成 16位授权码。

163 / 网易邮箱
网页登录 → 设置 → POP3/SMTP/IMAP。
开启 IMAP/SMTP 服务，手机验证后设置授权码。

Gmail (谷歌邮箱)
必须开启两步验证。
在 应用专用密码 页面生成 16 位密码。
国内网络：直连 imap.gmail.com 通常不可用，需配置代理或使用转发方案。

Outlook (微软邮箱)
直接使用账户密码通常即可。
若开启双重验证，需在安全设置中生成应用密码。

📡 通知效果示例

原始模式 (默认)
📧 qq邮箱更新:
来自: 张三 
主题: 项目进度汇报
时间: 2026-03-07 14:30
内容: 你好，本周项目已完成模块A的开发...

AI 模式 (ai_mode: true)
插件会构造系统提示词（System Prompt）调用 LLM，根据你配置的人格设定（Persona）自动处理邮件（如判断是否需要回复），并在日志中输出结果。

⚙️ 常见问题

Q: 收不到通知？
检查 admin_uids 是否配置正确。
检查 notify_umo 是否填写了正确的会话 ID。
检查邮箱是否开启了 IMAP 服务。

Q: 提示“无权限”？
确保发送指令的账号 ID 已添加到插件配置的 admin_uids 列表中。

Q: 连接超时 (Timeout)？
Gmail 用户请检查网络环境或使用代理。
国内服务器建议使用 QQ 邮箱或 163 邮箱。

Q: 如何开启 AI 摘要？
代码中目前通过 ai_mode 字段控制。若需 AI 摘要，请确保账户配置中 ai_mode 为 true，并已在 AstrBot 中配置好 LLM 模型。