import asyncio
import re
import os
import json
import time
from datetime import datetime, timezone
from typing import Dict, Any, List
from astrbot.api import AstrBotConfig, logger, FunctionTool
from astrbot.api.event import AstrMessageEvent, MessageEventResult, MessageChain, filter
from astrbot.api.star import Context, Star, register
from .imap_client import imap_fetch_new, imap_query_since, is_recent_email
from .smtp_client import smtp_send_mail
from astrbot.core.platform import AstrBotMessage, PlatformMetadata, MessageMember
from astrbot.core.message.components import Plain
from astrbot.core.platform.message_type import MessageType
from astrbot.core.agent.tool import ToolSet
from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext


@register(
    "astrbot_plugin_mail",
    "Neo",
    "imap邮箱接受与smtp回复",
    "1.0.0",
)
class MailNotifyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # Background polling task created during initialize().
        self._check_task: asyncio.Task | None = None
        # Runtime-only status used by /mail_status; not persisted.
        self._last_check_time: dict[str, str] = {}
        self._account_status: dict[str, str] = {}
        self.mail_tool = SendMailTool(accounts=self.config.get("mail_accounts", {}))
        self.context.add_llm_tools(self.mail_tool)
        

    async def initialize(self):
        """插件初始化后启动后台邮件检查循环"""
        self._check_task = asyncio.create_task(self._check_loop())
        logger.info("邮件通知插件：后台检查循环已启动。")

    # ── 后台循环 ──────────────────────────────────────────
    async def _check_loop(self):
        await asyncio.sleep(10) # 等待系统初始化完成
        while True:
            try:
                interval = self.config.get("check_interval", 5)
                # ✅ 修改点：移除了这里的 notify_umo 判断，不再阻断整个循环
                accounts = self.config.get("mail_accounts", [])
                
                for account in accounts:
                    if not account.get("email") or not account.get("imap_server"):
                        continue
                    
                    # ✅ 修改点：将 notify_umo 的获取移到循环内部，读取账户自己的配置
                    # 优先使用账户内的 notify_umo，如果没有再用全局的
                    notify_umo = account.get("notify_umo") or self.config.get("notify_umo", "")
                    
                    # ✅ 修改点：只有当这个账户有通知目标时，才去检查
                    if notify_umo:
                        try:
                            await self._check_account(account)
                            self._account_status[account["email"]] = "✅ 正常"
                        except Exception as e:
                            self._account_status[account["email"]] = f"❌ {str(e)[:80]}"
                            logger.error(f"邮件通知插件：{account['email']} 检查失败: {e}")
                        
                        self._last_check_time[account["email"]] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    # 如果没有 notify_umo，这里可以打印一个 debug 日志，但不报错
                    else:
                        logger.debug(f"邮件通知插件：账户 {account['email']} 未配置 notify_umo，跳过检查。")
                
                await asyncio.sleep(max(interval, 1) * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"邮件通知插件：循环异常: {e}")
                await asyncio.sleep(60)

    # ── IMAP逻辑 ───────────────────────────────────────────────
    async def _check_account(self, account: dict):
        # 每个邮箱独立存储状态，避免冲突
        account_email = account["email"]
        max_body_len = max(int(self.config.get("max_body_length", 500) or 500), 1)
        filter_body_len = max(
            int(self.config.get("filter_body_length", 3000) or 3000),
            max_body_len,
        )

        uid_key = f"last_uid_{account_email}"
        init_key = f"init_time_{account_email}"
        last_uid = await self.get_kv_data(uid_key, 0) or 0
        init_time = await self.get_kv_data(init_key, "")

        is_first_run = not init_time
        if is_first_run:
            # 首次运行记录初始化时间和当前UID基线，防止历史邮件被推送
            init_time = datetime.now(timezone.utc).isoformat()
            await self.put_kv_data(init_key, init_time)

        # imaplib为阻塞操作，实际查询在工作线程中执行
        new_emails, new_max_uid = await asyncio.to_thread(
            imap_fetch_new, account, last_uid, max_body_len, filter_body_len
        )

        if new_max_uid > last_uid:
            await self.put_kv_data(uid_key, new_max_uid)

        if is_first_run:
            if new_max_uid > 0:
                logger.info(
                    f"邮件通知插件：{account_email} 初始化完成，最大UID = {new_max_uid}"
                )
            return

        init_dt = datetime.fromisoformat(init_time)
        for mail_info in new_emails:
            # 二次校验邮件时间，避免刚拉取的邮件属于历史存量

            if is_recent_email(mail_info, init_dt):
                # --- 开关1: 是否转发给用户 ---
                should_forward = account.get("forward_to_user", True)
                # --- 开关2: 是否启用 AI 处理 ---
                ai_enabled = account.get("ai_mode", False)
                # --- 目标会话ID ---
                notify_umo = account.get("notify_umo", "")

                # 如果 notify_umo 未配置，则无法进行任何通知或 AI 处理（因为 AI 的工具调用结果也需要发回这个 UMO）
                if not notify_umo:
                    logger.warning(f"账户 {account['email']} 的 notify_umo 未配置，跳过处理。")
                    continue

                # --- AI Agent 处理逻辑 ---
                if ai_enabled:
                    try:
                        # 1. 生成唯一的 Session ID
                        session_id = f"mail_agent_{account['email']}_{notify_umo}"
                        
                        # 2. 获取当前聊天模型 ID (直接使用 notify_umo 作为会话标识)
                        prov_id = await self.context.get_current_chat_provider_id(umo=notify_umo)
                        if not prov_id:
                            prov_id = await self.context.get_current_chat_provider_id(umo="") # 保底

                        # 3. 获取人格设定 (这里只获取纯文本，不包含时间地点)
                        # 假设 self._get_personality_prompt(account) 返回的是类似 "你是一个猫娘..." 的字符串
                        persona_text = await self._get_personality_prompt(account) 
                        if not persona_text:
                            persona_text = "你是一个邮件助手。" # 默认人格

                        # 4. 构造 Fake Event (最终修复版)
                        # 4.1 构造 AstrBotMessage (通过属性赋值，而非构造函数传参)
                        fake_astrbot_message = AstrBotMessage()
                        fake_astrbot_message.type = MessageType.FRIEND_MESSAGE  # 消息类型
                        fake_astrbot_message.self_id = "astrbot"  # 机器人ID
                        fake_astrbot_message.session_id = notify_umo  # 会话ID
                        fake_astrbot_message.message_id = "mail_plugin_" + str(int(time.time()))  # 消息ID
                        fake_astrbot_message.sender = MessageMember(
                            user_id="internal_bot",
                            nickname="Mail Plugin"
                        )  # 发送者
                        fake_astrbot_message.message = [Plain(text="System: New Email Notification")]  # 消息链
                        fake_astrbot_message.message_str = "System: New Email Notification"  # 纯文本消息
                        fake_astrbot_message.raw_message = {}  # 原始消息
                        fake_astrbot_message.timestamp = int(time.time())  # 时间戳
                        # group 属性默认为 None，如果是群聊可以设置
                        # fake_astrbot_message.group = Group(group_id="123456")

                        # 4.2 构造 PlatformMetadata
                        fake_platform_meta = PlatformMetadata(
                            name="MailPlugin",                    # 平台名称
                            description="Mail Notification",      # 描述 (必填)
                            id="mail_plugin"
                        )

                        # 4.3 构造 AstrMessageEvent
                        fake_event = AstrMessageEvent(
                            message_str=fake_astrbot_message.message_str,
                            message_obj=fake_astrbot_message,
                            platform_meta=fake_platform_meta,
                            session_id=notify_umo
                        )

                        # 5. 调用 Agent
                        custom_prompt = account.get("custom_prompt", "你收到了一封新邮件，请阅读后决定是否回复或执行其他操作。")
                        final_system_prompt = f"""# 角色设定-{persona_text}- # 任务指令-{custom_prompt}-""".strip()
                        llm_resp = await self.context.tool_loop_agent(
                        event=fake_event,
                        chat_provider_id=prov_id,
                        system_prompt=final_system_prompt,  # 使用修正后的 Prompt
                        prompt=f"📧 {account.get('name', '邮件')}收到新邮件，请处理：\n主题：{mail_info['subject']}\n发件人：{mail_info['from_addr']}\n内容：{mail_info['body'][:1000]}",
                        max_steps=30,
                        tools=ToolSet([SendMailTool(accounts=[account])])
                    )

                        # 6. 处理结果
                        ai_reply = llm_resp.completion_text
                        logger.info(f"AI 处理完成: {ai_reply}")

                    except Exception as e:
                        logger.error(f"AI Agent 处理失败: {e}", exc_info=True)

                # --- 独立判断：是否转发原始邮件 ---
                # 这个判断不受 AI 处理结果影响，纯粹看开关
                if should_forward:
                    await self.context.send_message(
                        notify_umo, # 使用配置的 UMO 发送
                        MessageChain().message(
                            f"📧 {account.get('name', '邮件')}更新:\n"
                            f"来自: {mail_info['from_name']} <{mail_info['from_addr']}>\n"
                            f"主题: {mail_info['subject']}\n"
                            f"时间: {mail_info['date']}\n"
                            f"内容: {mail_info['body'][:200]}..." # 限制长度
                        )
                    )
    # --- 辅助函数：获取人格设定 ---
    async def _get_personality_prompt(self, account: dict) -> str:
        persona_id = account.get("llm_persona", "")
        if persona_id:
            persona_mgr = self.context.persona_manager
            persona = await persona_mgr.get_persona(persona_id)
            if persona:
                return persona.system_prompt # ✅ 确认为 system_prompt
        return "你是一个邮件助手，请阅读邮件并决定是否需要回复。"
    

    # ── Notification ─────────────────────────────────────────────
    def _get_filter_settings(self, prefix: str) -> dict:
        settings = self.config.get(f"{prefix}_settings", {}) or {}
        if isinstance(settings, dict):
            return settings
        return {}

    def _get_filter_enabled(self, prefix: str) -> bool:
        settings = self._get_filter_settings(prefix)
        if "enable" in settings:
            return bool(settings.get("enable", False))
        return bool(self.config.get(f"enable_{prefix}", False))

    def _get_filter_rules(self, prefix: str, field: str) -> list[str]:
        settings = self._get_filter_settings(prefix)
        nested_values = settings.get(f"{field}_rules", [])
        if not nested_values:
            nested_values = self.config.get(f"{field}_{prefix}", []) or []
        values = nested_values or []
        return [
            str(value).strip()
            for value in values
            if isinstance(value, (str, int, float)) and str(value).strip()
        ]

    @staticmethod
    def _matches_sender_rule(mail_info: dict, rule: str) -> bool:
        normalized_rule = rule.strip().casefold()
        if not normalized_rule:
            return False
        from_addr = (mail_info.get("from_addr") or "").strip().casefold()
        from_name = (mail_info.get("from_name") or "").strip().casefold()

        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized_rule):
            return from_addr == normalized_rule

        if normalized_rule.startswith("@"):
            return from_addr.endswith(normalized_rule)

        return normalized_rule in from_addr or normalized_rule in from_name

    @staticmethod
    def _matches_contains_rule(text: str, rule: str) -> bool:
        normalized_rule = rule.strip().casefold()
        if not normalized_rule:
            return False
        return normalized_rule in (text or "").casefold()

    def _match_rule_group(
        self, mail_info: dict, prefix: str
    ) -> tuple[bool, str | None]:
        sender_rules = self._get_filter_rules(prefix, "sender")
        for rule in sender_rules:
            if self._matches_sender_rule(mail_info, rule):
                return True, f"sender:{rule}"

        subject_rules = self._get_filter_rules(prefix, "subject")
        subject = mail_info.get("subject") or ""
        for rule in subject_rules:
            if self._matches_contains_rule(subject, rule):
                return True, f"subject:{rule}"

        body_rules = self._get_filter_rules(prefix, "body")
        filter_body = mail_info.get("filter_body") or mail_info.get("body") or ""
        for rule in body_rules:
            if self._matches_contains_rule(filter_body, rule):
                return True, f"body:{rule}"

        return False, None

    def _should_notify_mail(self, mail_info: dict) -> tuple[bool, str]:
        enable_blacklist = self._get_filter_enabled("blacklist")
        enable_whitelist = self._get_filter_enabled("whitelist")

        if enable_blacklist:
            is_blacklisted, rule = self._match_rule_group(mail_info, "blacklist")
            if is_blacklisted:
                return False, f"被黑名单屏蔽 ({rule})"

        if enable_whitelist:
            is_whitelisted, rule = self._match_rule_group(mail_info, "whitelist")
            if not is_whitelisted:
                return False, "被白名单屏蔽"
            return True, f"白名单允许 ({rule})"

        return True, "允许，因为不需要匹配白名单限制"

    async def _send_notification(self, account: dict, mail_info: dict, notify_umo: str):
        account_name = account.get("name") or account["email"]
        use_ai = self.config.get("ai_summary", False)
        body_text = mail_info["body"]
        if use_ai and body_text:
            body_text = await self._try_ai_summary(mail_info, notify_umo, body_text)

        lines = [
            f"📬 新邮件通知 [{account_name}]",
            "━━━━━━━━━━━━━━━━",
            f"📤 发件人: {mail_info['from_name']}",
        ]
        if mail_info["from_addr"] and mail_info["from_addr"] != mail_info["from_name"]:
            lines[-1] += f" <{mail_info['from_addr']}>"
        lines.append(f"📋 主题: {mail_info['subject']}")
        lines.append(f"🕐 时间: {mail_info['date']}")
        if body_text:
            label = "📝 AI摘要" if use_ai else "📝 预览"
            lines.append(f"{label}: {body_text}")

        chain = MessageChain().message("\n".join(lines))
        await self.context.send_message(notify_umo, chain)

    async def _try_ai_summary(
        self, mail_info: dict, notify_umo: str, fallback: str
    ) -> str:
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=notify_umo
            )
            if not provider_id:
                return fallback

            prompt = (
                "请用简洁的中文（不超过100字）总结以下邮件内容，只输出摘要：\n"
                f"主题：{mail_info['subject']}\n"
                f"正文：{fallback}"
            )
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            if llm_resp and llm_resp.completion_text:
                return llm_resp.completion_text
        except Exception as e:
            logger.warning(f"MailNotify: AI summary failed: {e}")
        return fallback

    def _get_account_by_name_or_email(self, account_name: str) -> dict | None:
        accounts = self.config.get("mail_accounts", [])
        target_name = account_name.strip()
        for acc in accounts:
            name = (acc.get("name") or "").strip()
            addr = (acc.get("email") or "").strip()
            if target_name in (name, addr):
                return acc
        return None

    def _get_admin_uids(self) -> set[str]:
        admin_uids = self.config.get("admin_uids", []) or []
        return {
            str(uid).strip()
            for uid in admin_uids
            if isinstance(uid, (str, int)) and str(uid).strip()
        }

    def _get_admin_denied_message(self) -> str:
        if not self._get_admin_uids():
            return "❌ 还未指定插件管理员。\n请在插件web设置的admin_uid中添加用户id。"
        return "❌ 无权限使用该命令。"

    def _is_plugin_admin(self, event: AstrMessageEvent) -> bool:
        admin_uids = self._get_admin_uids()
        sender_id = str(event.get_sender_id()).strip()
        return bool(sender_id and sender_id in admin_uids)

    def _parse_mail_reply_args(self, message_str: str) -> tuple[str, str, str, str]:
        raw = re.sub(r"\s+", " ", (message_str or "").strip())
        if not raw:
            raise ValueError("参数为空。")
        parts = raw.split(" ", 1)
        if len(parts) < 2:
            raise ValueError("参数缺失。")
        
        args_text = parts[1].strip()
        args = args_text.split(" ", 2)
        if len(args) < 3:
            raise ValueError("参数不足。")
        
        account_name, to_addr, subject_body = args[0].strip(), args[1].strip(), args[2]
        
        if "|" not in subject_body:
            raise ValueError("缺少主题与正文分隔符。")
        
        subject, body = [s.strip() for s in subject_body.split("|", 1)]
        
        if not account_name:
            raise ValueError("账户名不能为空。")
        if not to_addr:
            raise ValueError("收件人不能为空。")
        if "@" not in to_addr:
            raise ValueError("收件人邮箱格式错误。")
        if not subject:
            raise ValueError("邮件主题不能为空。")
        if not body:
            raise ValueError("邮件正文不能为空。")
        if len(subject) > 200:
            raise ValueError("邮件主题过长（最多 200 字符）。")
        if len(body) > 5000:
            raise ValueError("邮件正文过长（最多 5000 字符）。")
        
        return account_name, to_addr, subject, body

    # ── 邮件发送工具函数 ────────────────────────────────────────────────────────
    @filter.llm_tool(name="send_smtp_mail")
    async def send_smtp_mail(self, event: AstrMessageEvent, to_addr: str, subject: str, body: str, account_name: str = None, attachments: list = None) -> MessageEventResult:
        '''
        发送 SMTP 邮件。
        
        Args:
            to_addr(string): 收件人邮箱地址
            subject(string): 邮件主题
            body(string): 邮件正文内容
            account_name(string): 发件账户名称（可选）
            attachments(list): 附件文件路径列表（可选）
        '''
        # 1. 验证收件人邮箱格式（简单的检查）
        if not to_addr or "@" not in to_addr:
            return "❌ 错误：收件人邮箱格式不正确。"

        # 2. 验证主题和内容
        if not subject or not subject.strip():
            return "❌ 错误：邮件主题不能为空。"
        if not body or not body.strip():
            return "❌ 错误：邮件内容不能为空。"

        # 3. 验证附件（如果提供了的话）
        if attachments:
            for path in attachments:
                if not os.path.exists(path):
                    return f"❌ 错误：附件文件不存在：{path}"
        
        # 4. 获取SMTP账户配置
        smtp_config = self.choose_account(account_name, to_addr)
        if not smtp_config:
            if account_name:
                return f"❌ 错误：未找到匹配的SMTP账户 '{account_name}'。请检查账户备注名。"
            else:
                return "❌ 错误：未找到有效的SMTP账户配置。"


        # 5. 使用 smtp_client 发送邮件
        try:
            # 导入修改后的函数
            from .smtp_client import smtp_send_mail
            # 直接调用，smtp_send_mail内部会判断attachments是否为空
            result = smtp_send_mail(smtp_config, to_addr, subject, body, attachments)
            
            # 构造返回信息
            base_msg = f"✅ 邮件发送成功！\n📬 已发送至：{to_addr}\n📌 主题：{subject}"
            if attachments:
                base_msg += f"\n📎 附有 {len(attachments)} 个文件"
            return base_msg
            
        except ValueError as e:
            return f"❌ 配置错误：{str(e)}"
        except RuntimeError as e:
            return f"❌ 发送失败：{str(e)}"
        except Exception as e:
            # 打印更详细的错误日志，帮助排查
            print(f"Debug - smtp_config keys: {list(smtp_config.keys()) if smtp_config else 'None'}")
            print(f"Debug - Error in sending mail: {e}")
            return f"❌ 未知错误：{str(e)}"
        
    def choose_account(self, account_name: str, to_addr: str) -> dict | None:
        """
        智能选择SMTP账户
        逻辑：从正确的配置路径获取账户列表
        """
        # 关键修改：尝试多个可能的配置路径
        # 1. 优先尝试 self.config.get("accounts")（对应 _conf_schema.json 的结构）
        all_accounts = self.config.get("accounts", [])
        
        # 2. 如果上面没找到，尝试 self.context.get_config().get("accounts")
        if not all_accounts:
            all_accounts = self.context.get_config().get("accounts", [])
        
        # 3. 如果还没找到，尝试 self.config.get("mail_accounts")（旧版兼容）
        if not all_accounts:
            all_accounts = self.config.get("mail_accounts", [])
        
        # 防御性检查
        if not all_accounts:
            logger.warning("❌ choose_account: 所有配置路径均未找到账户列表。")
            logger.warning(f"    self.config 中的键: {list(self.config.keys()) if self.config else 'None'}")
            return None
        

        # --- 情况1：AI填写了账户名 ---
        if account_name and account_name.strip():
            query = account_name.strip()
            logger.debug(f"🔍 正在尝试匹配账户名: '{query}'")
            
            for acc in all_accounts:
                # 只匹配 'name' 字段
                config_name = str(acc.get("name", "")).strip()
            
                # ✅ 修改点：使用“包含”匹配，并且忽略大小写
                if query.lower() in config_name.lower():
                    logger.info(f"✅ 成功匹配账户 (模糊匹配): {config_name} ({acc.get('email')})")
                    return acc

            # 如果没找到，打印所有可用账户名用于调试
            available = [a.get('name', '未命名') for a in all_accounts]
            logger.warning(f"❌ 未找到匹配的账户。搜索词: '{query}', 可用账户名: {available}")
            return None

        # --- 情况2：AI没填写账户名，使用默认账户 ---
        if all_accounts:
            first_name = all_accounts[0].get('name', '未知')
            logger.info(f"✅ 使用默认账户: {first_name}")
            return all_accounts[0]

        return None

    
    # ── Commands ─────────────────────────────────────────────────

    @filter.command("mail_status")
    async def mail_status(self, event: AstrMessageEvent):
        if not self._is_plugin_admin(event):
            yield event.plain_result(self._get_admin_denied_message())
            return
        """查看所有邮箱的监控状态"""
        # Read current config plus runtime cache to render a status snapshot.
        accounts = self.config.get("accounts", [])
        notify_umo = self.config.get("notify_umo", "")
        interval = self.config.get("check_interval", 5)

        if not accounts:
            yield event.plain_result(
                "📭 未配置任何邮箱账户，请在 WebUI 插件配置中添加。"
            )
            return

        lines = [
            f"📊 邮箱监控状态 (间隔: {interval}分钟)",
            f"🔔 通知目标: {'已绑定' if notify_umo else '❗未绑定，请先在webui配置'}",
            "━━━━━━━━━━━━━━━━",
        ]
        for acc in accounts:
            addr = acc.get("email", "?")
            name = acc.get("name") or addr
            status = self._account_status.get(addr, "⏳ 等待首次检查")
            last = self._last_check_time.get(addr, "尚未检查")
            lines.append(f"📧 {name} ({addr})")
            lines.append(f"   状态: {status}")
            lines.append(f"   最近检查: {last}")

        yield event.plain_result("\n".join(lines))

    @filter.command("mail_check")
    async def mail_check(self, event: AstrMessageEvent):
        if not self._is_plugin_admin(event):
            yield event.plain_result(self._get_admin_denied_message())
            return
        """立即手动检查所有邮箱"""
        accounts = self.config.get("mail_accounts", [])
        if not accounts:
            yield event.plain_result(
                "📭 未配置任何邮箱账户，请在 WebUI 插件配置中添加。"
            )
            return

        notify_umo = self.config.get("notify_umo", "")
        if not notify_umo:
            yield event.plain_result(
                "❌ Notification target is not bound yet. "
            )
            return

        yield event.plain_result("🔍 正在检查所有邮箱...")
        # Manual check reuses the same account-checking path as the background loop.
        errors = []
        for account in accounts:
            if not account.get("email") or not account.get("imap_server"):
                continue
            email_addr = account["email"]
            try:
                await self._check_account(account)
                self._account_status[email_addr] = "✅ 正常"
            except Exception as e:
                self._account_status[email_addr] = f"❌ {str(e)[:80]}"
                errors.append(f"{account.get('name') or email_addr}: {e}")
            self._last_check_time[email_addr] = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        if errors:
            yield event.plain_result("⚠️ 部分邮箱检查失败:\n" + "\n".join(errors))
        else:
            yield event.plain_result("✅ 所有邮箱检查完成。")

    @filter.command("mail_query")
    async def mail_query(
        self, event: AstrMessageEvent, account_name: str, since_date: str
    ):
        if not self._is_plugin_admin(event):
            yield event.plain_result(self._get_admin_denied_message())
            return
        """查询指定邮箱自某日期以来的邮件，如 /mail_query qq邮箱 2026-03-01"""
        accounts = self.config.get("mail_accounts", [])
        # Resolve the target account by either display name or full email address.
        target = None
        for acc in accounts:
            name = acc.get("name", "")
            addr = acc.get("email", "")
            if account_name in (name, addr):
                target = acc
                break
        if not target:
            yield event.plain_result(
                f'❌ 未找到名为 "{account_name}" 的邮箱账户。\n'
                f"已配置的账户: {', '.join(a.get('name') or a.get('email', '?') for a in accounts)}"
            )
            return

        # The command accepts only YYYY-MM-DD to keep parsing deterministic.
        try:
            since_dt = datetime.strptime(since_date, "%Y-%m-%d")
        except ValueError:
            yield event.plain_result(
                "❌ 日期格式错误，请使用 YYYY-MM-DD，如 2026-03-01"
            )
            return

        yield event.plain_result(
            f"🔍 正在查询 {account_name} 自 {since_date} 以来的邮件..."
        )
        try:
            max_body_len = self.config.get("max_body_length", 500)
            # History query also uses a worker thread because IMAP access is blocking.
            emails = await asyncio.to_thread(
                imap_query_since, target, since_dt, max_body_len
            )
        except Exception as e:
            yield event.plain_result(f"❌ 查询失败: {e}")
            return

        if not emails:
            yield event.plain_result(
                f"📭 {account_name} 自 {since_date} 以来没有邮件。"
            )
            return

        lines = [
            f"📬 {account_name} 自 {since_date} 以来共 {len(emails)} 封邮件：",
            "━━━━━━━━━━━━━━━━",
        ]
        for i, m in enumerate(emails, 1):
            lines.append(f"{i}. 📋 {m['subject']}")
            lines.append(f"   📤 {m['from_name']}  🕐 {m['date']}")

        yield event.plain_result("\n".join(lines))

    @filter.command("mail_reply")
    async def mail_reply(self, event: AstrMessageEvent):
        if not self._is_plugin_admin(event):
            yield event.plain_result(self._get_admin_denied_message())
            return
        """手动发送邮件回复。格式：/mail_reply <账户备注名> <收件人邮箱> <主题>|<正文>"""
        usage = (
            "❌ 用法错误\n"
            "格式: /mail_reply <账户备注名> <收件人邮箱> <主题>|<正文>\n"
            "示例: /mail_reply qq邮箱 test@example.com 回复主题|你好，已收到你的邮件。"
        )
        try:
            account_name, to_addr, subject, body = self._parse_mail_reply_args(
                event.message_str
            )
        except ValueError as e:
            yield event.plain_result(f"{usage}\n原因: {e}")
            return

        account = self._get_account_by_name_or_email(account_name)
        if not account:
            accounts = self.config.get("mail_accounts", [])
            account_names = ", ".join(
                (a.get("name") or a.get("email") or "?") for a in accounts
            )
            yield event.plain_result(
                f'❌ 未找到名为 "{account_name}" 的邮箱账户。\n已配置账户: {account_names or "(空)"}'
            )
            return

        if not account.get("smtp_server"):
            yield event.plain_result(
                "❌ 该账户未配置 SMTP 服务器。请在插件配置中填写 smtp_server、smtp_port、smtp_use_ssl。"
            )
            return

        yield event.plain_result("📤 正在发送邮件...")
        try:
            await asyncio.to_thread(smtp_send_mail, account, to_addr, subject, body)
        except Exception as e:
            yield event.plain_result(f"❌ 发送失败: {e}")
            return
        account_display = account.get("name") or account.get("email") or account_name
        yield event.plain_result(
            f"✅ 发送成功\n账户: {account_display}\n收件人: {to_addr}\n主题: {subject}"
        )

    # ── Lifecycle ────────────────────────────────────────────────
    async def terminate(self):
        """Cancel background task on plugin unload."""
        if self._check_task and not self._check_task.done():
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        logger.info("MailNotify: plugin terminated.")








@dataclass
class SendMailTool(FunctionTool[AstrAgentContext]):
    accounts: List[Dict[str, Any]] = Field(default_factory=list)  # 改名为 accounts，明确是账户列表
    name: str = "reply_smtp_mail"
    description: str = ("通过SMTP协议发送邮件。需要配置至少一个SMTP账户。"
                        "to_addr: 收件人邮箱地址。"
                        "subject: 邮件主题。"
                        "body: 邮件正文内容。"
                        "account_name: 使用的账户的备注名，支持模糊匹配。"
                        "attachments: (可选) 附件文件路径列表。")
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "to_addr": {"type": "string", "description": "收件人邮箱地址"},
                "subject": {"type": "string", "description": "邮件主题"},
                "body": {"type": "string", "description": "邮件正文内容"},
                "account_name": {"type": "string", "description": "发件账户名称，支持模糊匹配"},
                "attachments": {"type": "array", "items": {"type": "string"}, "description": "附件文件路径列表（可选）"},
            },
            "required": ["to_addr", "subject", "body", "account_name"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        # 直接使用传入的配置
        accounts = self.accounts  # 使用正确的字段名
        
        # 检查是否获取到了账户
        if not accounts:
            return "❌ 错误：未找到可用的SMTP账户配置。请检查插件配置。"

        to_addr = kwargs.get("to_addr")
        subject = kwargs.get("subject")
        body = kwargs.get("body")
        account_name = kwargs.get("account_name")
        attachments = kwargs.get("attachments", [])

        if not to_addr or "@" not in to_addr:
            return "❌ 错误：收件人邮箱格式不正确。"
        if not subject or not subject.strip():
            return "❌ 错误：邮件主题不能为空。"
        if not body or not body.strip():
            return "❌ 错误：邮件内容不能为空。"
        if not account_name or not account_name.strip():
            return "❌ 错误：发件账户名称 (account_name) 不能为空。"
        if attachments:
            for path in attachments:
                if not os.path.exists(path):
                    return f"❌ 错误：附件文件不存在：{path}"

        print(f"[DEBUG] SendMailTool called with account_name: {account_name}")
        print(f"[DEBUG] Available accounts: {accounts}")

        # 查找匹配的账户
        smtp_config = None
        query = account_name.strip()
        available_names = []

        for acc in accounts:
            config_name = str(acc.get("name", "")).strip()
            available_names.append(config_name)
            print(f"[DEBUG] Checking account name: '{config_name}' against query: '{query}'")
            
            if query.lower() in config_name.lower():
                smtp_config = acc
                print(f"[DEBUG] Found matching account: {smtp_config}")
                break
        
        if not smtp_config:
            available_names_str = ", ".join(available_names) if available_names else "无"
            return f"❌ 错误：未找到匹配的SMTP账户。\n正在查找: '{query}'\n可用账户: [{available_names_str}]"

        try:
            from .smtp_client import smtp_send_mail
            smtp_send_mail(smtp_config, to_addr, subject, body, attachments)
            base_msg = f"✅ 邮件发送成功！\n📬 已发送至：{to_addr}\n📌 主题：{subject}"
            if attachments:
                base_msg += f"\n📎 附有 {len(attachments)} 个文件"
            return base_msg
        except Exception as e:
            return f"❌ 发送失败：{str(e)}"