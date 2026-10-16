from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import nullcontext
from datetime import date, datetime, time, timedelta
from typing import Any

from .document_changes import (
    INTERACTIVE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS,
    INTERACTIVE_WRITE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS,
    document_authorization_lock,
)
from .confirmation_evidence import (
    database_root,
    evidence_summary,
    held_evidence_paths,
    purge_scheduled_evidence,
    schedule_evidence_file_release,
    schedule_evidence_release,
)
from .user_decisions import (
    decision_text_read_cache,
    contains_absolute_path,
    redact_absolute_paths_in_value,
    safe_decision_text,
    unsafe_decision_text,
)


INTERFACE = "activity-confirmation"
CONTRACT_VERSION = 4
CONFIRMATION_CARD_CONTRACT_VERSION = 5
CONFIRMATION_POLICY_VERSION = 7
CONFIRMATION_POLICY_SETTING_KEY = "activity_confirmation_policy_version"
CONFIRMATION_MODE_SETTING_KEY = "activity_confirmation_mode"
CONFIRMATION_MODES = {"compact", "standard", "max_recall"}
DEFAULT_CONFIRMATION_MODE = "standard"
QUESTION_KINDS = {"occurrence", "business_context", "outcome"}
CANDIDATE_ORIGINS = {"explicit_claim", "business_episode", "manual"}
PRIORITIES = {"high", "normal", "low"}
OUTCOME_VALUES = {"completed", "progressing", "blocked", "preparation_only", "unknown"}
MANUAL_CONFIRMATION_ACTION = "MANUAL_CONFIRMED_ACTIVITY"
BUSINESS_EPISODE_GAP = timedelta(minutes=5)
CONFIRMATION_POLICY_EVALUATED_REASON = f"policy_v{CONFIRMATION_POLICY_VERSION}_evaluated"
CONFIRMATION_POLICY_REASON_PREFIX = f"policy_v{CONFIRMATION_POLICY_VERSION}"
# Revalidate unresolved rows when question admission changes, without changing
# the public policy/schema contracts or treating a downgrade as a user answer.
CONFIRMATION_POLICY_QUESTION_SAFETY_REASON = (
    f"policy_v{CONFIRMATION_POLICY_VERSION}_question_safety_r3"
)
INDEPENDENT_FILE_EVIDENCE_SIGNAL = "independent_file_evidence"
INDEPENDENT_GIT_EVIDENCE_SIGNAL = "independent_git_evidence"
INDEPENDENT_PLATFORM_EVIDENCE_SIGNAL = "independent_platform_receipt"
ANSWERS = {"occurred", "not_occurred", "ignored", "misrecognized", "defer"}
REASONS = {
    "correct",
    "old_content",
    "future_not_occurred",
    "reference_only",
    "preparation_only",
    "sidebar_or_list",
    "vlm_or_ocr",
    "screen_region_or_app",
    "other",
    "user_ignored",
}
GENERALIZE_SCOPES = {"exact", "similar"}
UNRESOLVED_STATES = {"pending", "deferred"}
NEGATIVE_GENERALIZATION_REASONS = {
    "old_content",
    "future_not_occurred",
    "reference_only",
    "preparation_only",
    "sidebar_or_list",
    "vlm_or_ocr",
    "screen_region_or_app",
}
HIGH_IMPACT_EVENT_TYPES = {
    "interview",
    "meeting",
    "application",
    "submission",
    "send",
    "call",
    "completion",
    "purchase",
    "publish",
}
ANSWER_OPTIONS = ["occurred", "not_occurred", "ignored", "defer"]
DEFAULT_CONFIRMATION_CARD_LIMIT = 12
MAX_CONFIRMATION_CARD_LIMIT = 50
GENERIC_CONFIRMATION_OBJECT_IDENTITIES = frozenset(
    {
        "",
        "事项", "任务", "项目", "工作", "操作", "报告", "邮件", "消息",
        "申请", "提交", "发送", "购买", "发布", "测试", "构建", "打包", "安装", "部署",
        "市场", "国家市场", "海外市场", "国际市场", "国内市场", "全球市场", "区域市场",
        "业务", "销售", "渠道", "商务", "运营", "产品", "客户", "公司", "项目管线", "外贸",
        "task", "project", "work", "report", "email", "message", "test", "build",
        "market", "business", "sales", "channel", "customer", "company", "product",
        # A bare platform name is not a concrete object: "sent GitHub" carries
        # no verifiable payload, so it must stay a low-disturbance clue.
        "github", "gitlab", "bitbucket",
        # Bare customer categories mirror the market/business entries above:
        # "sent 海外大客户" names an audience, not a deliverable.
        "大客户", "海外大客户", "海外客户",
    }
)

# OCR artifacts that never appear in a human-written object: an unclosed
# bracket tail ("任务（4"), symbol debris (© 〉), circled list markers (⑧),
# an ellipsis mid-phrase ("pac..."), or a lowercase tooling identifier
# ("release:stage") mean the extractor sliced interface noise.
OCR_ARTIFACT_OBJECT_RE = re.compile(
    r"[（(][^）)]{0,6}$"
    r"|[©®™§〉〈]"
    r"|[①-⑳]"
    r"|…|\.{3}"
    r"|\b[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*\b"
)

# Counts of unnamed items are bookkeeping, not an identifiable occurrence.
# Match the whole residual phrase so a named document containing counts (for
# example, "Atlas 项目 12 项检查清单") retains its concrete subject.
ACTIVITY_STATISTIC_RE = re.compile(
    r"(?:\d[\d,]*|[一二三四五六七八九十百千]+|全部|所有|若干)\s*"
    r"(?:个|条|项|份|处|次)?\s*"
    r"(?:内容(?:项|条目)?|(?:文件|代码)(?:变动|变更|修改)?(?:记录|事件)?|"
    r"数据(?:项|条目|记录)?|记录|条目|事项|任务|操作|测试(?:用例|项)?|用例|检查项)"
    r"|\b\d[\d,]*\s+(?:content\s+items?|file\s+(?:change\s+records?|changes?|events?)|"
    r"records?|items?|tasks?|tests?|checks?)\b",
    re.I,
)
ACTIVITY_STATISTIC_REMAINDER_RE = re.compile(
    r"(?:[\s，,、。.;；:：+&/（）()\-]|"
    r"共计|累计|总计|总共|合计|其中|包括|本次|本轮|今天|当前|已经|"
    r"以及|并且|全部|完整|完全|成功|完成|处理|整理|提交|生成|修改|新增|更新|"
    r"核对|检查|归档|同步|通过|共|和|与|及|并|都|均|已|了|"
    r"\b(?:and|all|total|fully|successfully|completed?|processed|submitted|"
    r"updated|changed|checked|passed|done)\b)*",
    re.I,
)
LOW_VALUE_QUESTION_REASONS = frozenset(
    {"aggregate_summary_unanswerable", "ai_tool_activity_unverified"}
)

EVENT_RULES: tuple[tuple[str, str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "interview",
        "面试",
        (
            re.compile(
                r"(?:今天|今日|刚刚|已经|实际)?[^。！？!?；;,，]{0,20}"
                r"(?:参加了?|进行了?|完成了?|结束了?|面完了?|已面试|已通过|通过了?)"
                r"[^。！？!?；;,，]{0,28}(?:面试|初面|[一二三四五六123456]面|复试|终面)",
                re.I,
            ),
            re.compile(
                r"(?:面试|初面|[一二三四五六123456]面|复试|终面)"
                r"[^。！？!?；;,，]{0,20}(?:已发生|已完成|完成了?|已结束|结束了?|刚结束|面完了?)",
                re.I,
            ),
            re.compile(
                r"\b(?:attended|completed|finished|had)\s+(?:an?\s+)?(?:[\w&.+-]+\s+){0,4}interview\b|"
                r"\binterview\s+(?:ended|completed|finished|took\s+place)\b",
                re.I,
            ),
        ),
    ),
    (
        "meeting",
        "会议",
        (
            re.compile(r"(?:参加|加入|进行|召开|完成|结束|安排|邀请).{0,16}(?:会议|腾讯会议|zoom|google meet|teams)", re.I),
            re.compile(r"(?:会议|meeting|zoom|google meet|teams).{0,16}(?:进行|完成|结束|邀请|joined|started|ended)", re.I),
        ),
    ),
    (
        "application",
        "投递或申请",
        (
            re.compile(r"(?:已投递|投递成功|已申请|申请成功|application submitted|applied to)", re.I),
        ),
    ),
    (
        "submission",
        "提交",
        (
            re.compile(r"(?:已提交|提交成功|submitted successfully|submission complete)", re.I),
        ),
    ),
    (
        "send",
        "发送",
        (
            re.compile(r"(?:已发送|发送成功|message sent|email sent)", re.I),
        ),
    ),
    (
        "call",
        "通话",
        (
            re.compile(r"(?:正在通话|通话结束|参加.{0,8}电话|in call|call ended|joined call)", re.I),
        ),
    ),
    (
        "completion",
        "完成事项",
        (
            re.compile(r"(?:已完成|已经完成|刚刚完成|完成成功|已做完|任务完成|项目完成|工作完成)", re.I),
            re.compile(r"\b(?:completed|finished)\b", re.I),
        ),
    ),
    (
        "purchase",
        "购买",
        (
            re.compile(r"(?:已购买|购买成功|已下单|下单成功|付款成功)", re.I),
            re.compile(r"\b(?:purchased|purchase complete|order placed|bought)\b", re.I),
        ),
    ),
    (
        "publish",
        "发布",
        (
            re.compile(r"(?:已发布|发布成功|已经上线|正式上线|推送成功)", re.I),
            re.compile(r"\b(?:published|publication complete|went live)\b", re.I),
        ),
    ),
)

NON_ACTIONABLE_REGION_KINDS = {
    "sidebar",
    "file_list",
    "desktop_icon",
    "browser_tab",
    "task_list",
    "menu_bar",
    "media_chrome",
}
NON_ACTIONABLE_ACTIONABILITY = {"noise", "context_only"}
REPEATABLE_EVENT_TYPES = {
    "meeting",
    "application",
    "submission",
    "send",
    "call",
    "purchase",
    "publish",
}
DIRECT_REASON_CODES = {"submit_interaction_evidence", "submit_control_geometry"}
REALTIME_APP_TERMS = (
    "tencentmeeting",
    "腾讯会议",
    "zoom",
    "microsoft teams",
    "google meet",
    "facetime",
)
REALTIME_EVENT_STATE_RE = re.compile(
    r"(?:已加入|正在(?:会议|通话)|会议(?:进行|结束)|通话(?:进行|结束)|"
    r"(?:会议|通话).{0,12}(?:分钟|小时)|joined.{0,12}(?:meeting|call)|"
    r"in (?:a )?(?:meeting|call)|(?:meeting|call) (?:ended|completed)|"
    r"(?:meeting|call).{0,12}duration)",
    re.I,
)
INTERVIEW_TERM_FRAGMENT = r"(?:面试|初面|[一二三四五六123456]面|复试|终面|\binterview\b)"
INTERVIEW_PREPARATION_RE = re.compile(
    rf"(?:(?:准备|备考|材料|素材|简历|岗位|JD).{{0,24}}{INTERVIEW_TERM_FRAGMENT}|"
    rf"{INTERVIEW_TERM_FRAGMENT}.{{0,24}}(?:准备|材料|素材|笔记|题库|复盘模板)|"
    r"(?:resume|portfolio).{0,40}interview|interview.{0,24}(?:notes|prep|preparation|material))",
    re.I,
)
INTERVIEW_FUTURE_RE = re.compile(
    r"(?:将于|即将|稍后|未来|以后|之后|届时|预计|打算|明天|后天|下周|下个月|"
    r"计划|准备|待参加|待进行|尚未|要去|会去|会完成|"
    r"安排了?|预约了?|邀请|通知|时间定在|会有|还会|接下来).{0,30}"
    r"(?:面试|初面|[一二三四五六123456]面|复试|终面)|"
    r"(?:面试|初面|[一二三四五六123456]面|复试|终面).{0,30}"
    r"(?:将于|即将|稍后|未来|以后|之后|届时|预计|打算|明天|后天|下周|"
    r"计划|准备|待参加|尚未|要去|会去|会完成|安排|预约|邀请|通知)|"
    r"\b(?:upcoming|scheduled|will\s+(?:have|attend)|plan(?:ned)?\s+to|prepare\w*\s+for)\b"
    r".{0,40}\binterview\b|\binterview\b.{0,40}\b(?:upcoming|scheduled|tomorrow|next\s+week)\b",
    re.I,
)
INTERVIEW_PAST_REFERENCE_RE = re.compile(
    r"(?:以前|之前|过去|曾经|曾|去年|往年|历史|旧内容|过往|面试经历).{0,30}"
    r"(?:面试|初面|[一二三四五六123456]面|复试|终面)|"
    r"(?:面试|初面|[一二三四五六123456]面|复试|终面).{0,20}(?:经历|回顾|复盘|记录)|"
    r"\b(?:previously|formerly|last\s+year|past\s+interview|interview\s+history)\b",
    re.I,
)
INTERVIEW_META_INFERENCE_RE = re.compile(
    r"(?:待你确认|需要(?:你)?确认|请确认|检测到|识别到|系统(?:提示|判断|推断)|"
    r"(?:系统|模型|OCR|页面|截图|文本|文字|内容|提示|活动|记录|结果).{0,30}"
    r"(?:被)?(?:误报|误判|误识别|错误识别|反向识别|识别)(?:成|为)|"
    r"可能(?:表示|意味着|说明)).{0,80}"
    r"(?:面试|初面|[一二三四五六123456]面|复试|终面).{0,24}"
    r"(?:已发生|发生|已完成|完成|已结束|结束)|"
    r"(?:detected|identified|system\s+(?:prompt|inference)|please\s+confirm).{0,80}"
    r"\binterview\b.{0,24}(?:occurred|happened|completed|finished)",
    re.I,
)
INTERVIEW_OCCURRENCE_LANGUAGE_FRAGMENT = (
    rf"(?:{INTERVIEW_TERM_FRAGMENT}.{{0,24}}(?:已发生|发生|已完成|完成|已结束|结束)|"
    rf"(?:已发生|发生|已完成|完成|已结束|结束).{{0,24}}{INTERVIEW_TERM_FRAGMENT})"
)
INTERVIEW_META_LANGUAGE_RE = re.compile(
    rf"(?:"
    rf"(?:避免|防止|不要|不得).{{0,100}}{INTERVIEW_OCCURRENCE_LANGUAGE_FRAGMENT}"
    rf".{{0,16}}(?:句子|文本|表述|字样).{{0,24}}(?:过滤|误报|误判)|"
    rf"{INTERVIEW_OCCURRENCE_LANGUAGE_FRAGMENT}.{{0,20}}"
    rf"(?:能|可以|应当|应该).{{0,8}}(?:提取|识别).{{0,24}}"
    rf"(?:触发句|主体|公司|轮次)"
    rf")",
    re.I,
)
SENTENCE_SPLIT_RE = re.compile(r"[\r\n。！？!?；;,，]+")
CONFIRMATION_UI_RE = re.compile(
    r"(?:待确认|判断依据|系统理解错了|忽略该问题|重新梳理该问题|补充说明)"
    r".{0,120}(?:发生了|没有发生|安全线索|条线索|需要确认)|"
    r"(?:发生了|没有发生|安全线索|条线索)"
    r".{0,120}(?:待确认|判断依据|系统理解错了|忽略该问题|重新梳理)",
    re.I,
)
META_DISCUSSION_RE = re.compile(
    r"(?:如何|避免|防止|不要|不得|规则|正则|触发(?:词|句)|"
    r"过滤|误报|误判|误识别|反向识别|OCR|截图|页面文字|"
    r"系统(?:提示|判断|推断)|模型(?:判断|输出)|需要确认|请确认)"
    r".{0,160}(?:已完成|已经完成|已发送|发送成功|已发布|发布成功|"
    r"已提交|提交成功|completed|finished|message sent|published)|"
    r"(?:已完成|已经完成|已发送|发送成功|已发布|发布成功|"
    r"已提交|提交成功|completed|finished|message sent|published)"
    r".{0,160}(?:如何过滤|误报|误判|触发(?:词|句)|规则|正则)",
    re.I,
)
TOOL_STATUS_RE = re.compile(
    r"(?:全量|所有|本轮|当前|步骤|task|tests?|build|deploy|install|migration|"
    r"测试|构建|打包|安装|部署|迁移|任务|进度|处理)"
    r".{0,80}(?:已完成|已通过|已结束|成功|completed|finished|passed|done)|"
    r"(?:已完成|已通过|已结束|completed|finished|passed|done)"
    r".{0,80}(?:测试|构建|打包|安装|部署|迁移|步骤|tasks?|tests?|build|deploy|install)|"
    r"(?:已完成|完成) ?\s*\d+\s*/\s*\d+",
    re.I,
)
STRONG_TOOL_STATUS_RE = re.compile(
    r"(?:全量|所有|本轮|当前步骤).{0,40}"
    r"(?:测试|构建|打包|安装|部署|迁移|任务).{0,40}"
    r"(?:已完成|已通过|成功|completed|finished|passed|done)|"
    r"(?:测试|构建|打包|安装|部署|迁移|任务).{0,40}"
    r"(?:全部|全量).{0,16}(?:已完成|已通过|成功)|"
    r"(?:已完成|完成) ?\s*\d+\s*/\s*\d+|"
    r"\b(?:all|full|current\s+(?:step|run)|this\s+(?:run|step))\b.{0,24}"
    r"\b(?:tests?|build|deploy(?:ment)?|migration|tasks?)\b.{0,40}"
    r"\b(?:completed|finished|passed|done|succeeded)\b",
    re.I,
)
CONDITIONAL_COMPLETION_RE = re.compile(
    r"(?:任务|子任务|项目|工作|流程|步骤)\s*(?:已经|已)?完成\s*(?:后|前|时|才|即可|方可)|"
    r"\b(?:after|before|when|once|until|if)\b.{0,32}"
    r"\b(?:task|subtask|job|workflow|process|project|work)\b.{0,24}"
    r"\b(?:complete(?:d|s)?|finish(?:ed|es)?)\b.{0,48}"
    r"\b(?:show|display|trigger|enable|allow|render|appear|notify|prompt)\b|"
    r"\b(?:show|display|trigger|enable|allow|render|notify|prompt)\b.{0,48}"
    r"\b(?:after|before|when|once|until|if)\b.{0,32}"
    r"\b(?:task|subtask|job|workflow|process|project|work)\b.{0,24}"
    r"\b(?:complete(?:d|s)?|finish(?:ed|es)?)\b|"
    r"\b(?:task|subtask|job|workflow|process|project|work)\b.{0,24}"
    r"\b(?:complete(?:d|s)?|finish(?:ed|es)?)\b.{0,24}"
    r"\b(?:before|then|so\s+that)\b.{0,32}"
    r"\b(?:show|display|trigger|enable|allow|render|notify|prompt)\b",
    re.I,
)
NEGATED_OCCURRENCE_RE = re.compile(
    r"(?:不能|不可|不应|不该|不得|无法|尚未|未能|并未|没有|不(?:代表|意味着|等于|算作))"
    r".{0,36}(?:宣称|确认|证明|表示|意味着|视为|认为|算作|当作)?"
    r".{0,36}(?:已完成|已经完成|完成|完成成功|已发送|发送成功|已提交|提交成功|"
    r"已发布|发布成功|已购买|购买成功|completed|finished|sent|submitted|published)|"
    r"\b(?:cannot|can't|must not|should not|not yet|did not|has not|have not)\b"
    r".{0,72}\b(?:claim|confirm|prove|mean|count as|completed|finished|sent|submitted|published)\b",
    re.I,
)
NEGATED_FRAGMENT_RE = re.compile(
    r"^\s*(?:(?:但|但是|不过|然而|只是)\s*)?"
    r"(?:(?:还|仍|尚)\s*)?(?:没有|没|未|未能|并未)"
    r"(?:\s*(?:发生|完成|提交|发送|发布|上线|做好|做完))?"
    r"(?:了|呢|过)?\s*$|"
    r"^\s*(?:not\s+yet|still\s+not|did\s+not|has\s+not|have\s+not)\s*$",
    re.I,
)
ANAPHORIC_OBJECT_RE = re.compile(
    r"^(?:有了|好了|行了|成了|完成了|做完了|这个|那个|这些|那些|它|它们|"
    r"此项|该项|这项|那项|这里|那里|这边|那边|其|相关内容|上述内容|"
    r"it|this|that|these|those|done|ready)$",
    re.I,
)
TRUNCATED_OBJECT_RE = re.compile(
    r"(?:[、,，:：/|—-]\s*(?:可(?:直(?:接)?)?|待|未|尚|但|并|和|与|及|或)?|"
    r"(?:可直|以及|并且|然后|接下来|但|但是|不过|和|与|及|或))\s*$",
    re.I,
)
INTERNAL_IMPLEMENTATION_FIELD_STATUS_RE = re.compile(
    r"(?:^|[\r\n。！？!?；;])"
    r"(?=[^\r\n。！？!?；;]{0,180}(?:\b(?:entry|record|payload|settings?|config)\b|"
    r"内部(?:记录|字段|配置)?|载荷|字段|键|\b(?:field|key)\b))"
    r"(?=[^\r\n。！？!?；;]{0,180}\b[a-z][a-z0-9]*(?:_[a-z0-9]+){1,8}\b)"
    r"(?=[^\r\n。！？!?；;]{0,180}(?:检测到|命中|存在|包含|写入|读取|"
    r"\b(?:detected|found|contains?|stored|written|read)\b))"
    r"[^\r\n。！？!?；;]{0,180}",
    re.I,
)
RECRUITMENT_APP_RE = re.compile(
    r"(?:BOSS\s*直聘|boss[-_.\s]*zhipin|\bzhipin\b|猎聘|智联招聘|前程无忧|"
    r"拉勾|招聘|\brecruit(?:ing|ment)\b|\btalent(?:\s+acquisition)?\b|\bjobs?\b)",
    re.I,
)
CHAT_DELIVERY_STATUS_RE = re.compile(
    r"(?:[［\[]\s*(?:送达|已读|未读|delivered|read)\s*[］\]]|"
    r"消息气泡|聊天记录|私信|对方回复|收到|"
    r"(?:对方|联系人)(?:已)?(?:同意|接受)|\b(?:accepted|connected)\b)",
    re.I,
)
RECRUITMENT_CHAT_DELIVERY_RE = re.compile(
    r"(?:招聘|猎头|候选人|职位|岗位|HR|人事|人力资源)"
    r".{0,100}(?:[［\[]\s*(?:送达|已读|未读|delivered|read)\s*[］\]]|"
    r"消息气泡|聊天记录|私信|对方回复|候选人消息|招聘消息|"
    r"(?:对方|联系人)(?:已)?(?:同意|接受)|(?:accepted|connected))|"
    r"(?:[［\[]\s*(?:送达|已读|未读|delivered|read)\s*[］\]]|"
    r"消息气泡|聊天记录|私信|对方回复|候选人消息|招聘消息|"
    r"(?:对方|联系人)(?:已)?(?:同意|接受)|(?:accepted|connected))"
    r".{0,100}(?:招聘|猎头|候选人|职位|岗位|HR|人事|人力资源)",
    re.I,
)
GENERATED_DOCUMENT_OBJECT_RE = re.compile(
    r"(?:\b\d{1,3}\s*(?:页|pages?)\b.{0,32}"
    r"(?:Word|PDF|PPTX?|报告|文档|方案|简历|文章|手册)|"
    r"(?:Word|PDF|PPTX?|报告|文档|方案|简历|文章|手册).{0,32}"
    r"\b\d{1,3}\s*(?:页|pages?)\b)",
    re.I,
)
GENERATED_DOCUMENT_CLAIM_RE = re.compile(
    r"(?:AI|模型|助手|智能体|Codex|ChatGPT|Claude)?"
    r".{0,24}(?:生成|产出|输出|导出|制作|整理)(?:完成|成功)?"
    r".{0,80}(?:Word|PDF|PPTX?|报告|文档|方案|简历|文章|手册)|"
    r"\b(?:generated|exported|produced|created)\b.{0,80}"
    r"\b(?:Word|PDF|PPTX?|report|document|proposal|resume|article|manual)\b",
    re.I,
)
NAMED_ENVIRONMENT_ONLY_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9&.+-]*(?:\s+[A-Za-z0-9&.+-]+){0,3}\s+"
    r"(?:staging|prod(?:uction)?|dev(?:elopment)?|test|preview|sandbox|beta)$",
    re.I,
)
AI_PLAN_OR_SUMMARY_RE = re.compile(
    r"(?:计划|方案|待执行|准备执行|下一步|执行声明|状态总结|进度总结|模型输出|AI生成|"
    r"我将|将会|准备|尚待).{0,100}(?:完成|发送|提交|发布|购买|部署|执行)|"
    r"\b(?:plan|proposal|next step|will|going to|generated summary|execution claim)\b"
    r".{0,100}\b(?:complete|finish|send|submit|publish|deploy|execute)\b",
    re.I,
)
GAME_CONTEXT_NOTE_RE = re.compile(
    r"(?:游戏(?:内|画面|界面|进度|奖励)?|电子游戏|不是现实(?:发生|事项)?|"
    r"\b(?:in[- ]?game|gameplay|game\s+(?:screen|ui|reward|progress))\b)",
    re.I,
)
GAME_UI_SIGNAL_RES = (
    re.compile(r"(?:\+\s*\d+\s*XP\b|\bXP\s*\+?\s*\d+\b|经验值)", re.I),
    re.compile(r"(?:等级|级别)\s*\d+|\b(?:level|rank)\s*\d+\b|(?:新兵|老兵|专家)难度", re.I),
    re.compile(
        r"(?:蓝图|武器|步枪|狙击|手枪|挂件|工具包|装备|皮肤|服装|"
        r"\b(?:blueprint|weapon|rifle|pistol|loadout|attachment|skin)\b)",
        re.I,
    ),
    re.compile(
        r"(?:挑战|战役|关卡|击杀|对局|游戏任务|"
        r"\b(?:mission|quest|challenge|match|campaign|kill)\b)",
        re.I,
    ),
    re.compile(r"(?:奖励|解锁|胜利|\b(?:reward|unlocked|victory)\b)", re.I),
)

WEAK_INTERVIEW_RE = re.compile(
    r"(?:面试|初面|[一二三四五六123456]面|复试|终面|候选人沟通|"
    r"\binterview\b|\bcandidate\s+(?:call|conversation)\b)",
    re.I,
)
WEAK_MEETING_RE = re.compile(
    r"(?:会议|会谈|访谈|沟通会|交流会|腾讯会议|飞书会议|"
    r"\bmeeting\b|\bzoom\b|\bgoogle meet\b|\bteams call\b)",
    re.I,
)
WEAK_CALL_RE = re.compile(
    r"(?:通话|电话沟通|电话会议|语音通话|视频通话|\bcall\b|\bphone\b)",
    re.I,
)
WEAK_APPLICATION_RE = re.compile(
    r"(?:投递|申请职位|求职申请|申请记录|\bapplication\b|\bapply\b)",
    re.I,
)
WEAK_SUBMISSION_RE = re.compile(
    r"(?:提交|确认提交|表单|\bsubmit\b|\bsubmission\b|\bform\b)",
    re.I,
)
WEAK_SEND_RE = re.compile(
    r"(?:发送|发出|邮件|消息|私信|\bsend\b|\bsent items\b|\bemail\b|\bmessage\b)",
    re.I,
)
WEAK_PUBLISH_RE = re.compile(
    r"(?:发布|上线|推送|\bpublish\b|\bgo live\b|\brelease\b)",
    re.I,
)
PREPARATION_OR_JD_RE = re.compile(
    r"(?:面试准备|准备面试|面试题|题库|模拟面试|复盘模板|准备材料|"
    r"岗位职责|岗位要求|职位描述|招聘要求|\bjob description\b|\bJD\b|"
    r"\binterview\s+(?:prep|preparation|notes?|questions?|materials?)\b)",
    re.I,
)
FUTURE_SESSION_RE = re.compile(
    r"(?:将于|即将|稍后|明天|后天|下周|计划|安排|预约|邀请|待参加|"
    r"\bupcoming\b|\bscheduled\b|\bwill\s+(?:meet|interview|join)\b)",
    re.I,
)
OLD_OR_NAVIGATION_RE = re.compile(
    r"(?:旧标题|历史记录|往期|过去|去年|侧栏|标签页|文件列表|"
    r"\bold title\b|\bhistory\b|\bsidebar\b)",
    re.I,
)
MULTI_TURN_RE = re.compile(
    r"(?:问题\s*[一二三四五六123456]?|回答\s*[一二三四五六123456]?|"
    r"问\s*[:：]|答\s*[:：]|\bquestion\s*\d*\b|\banswer\s*\d*\b|"
    r"\bQ\s*[:：]|\bA\s*[:：])",
    re.I,
)
MEETING_ASSISTANT_APP_RE = re.compile(r"(?:meeting\s*assistant|面试助手)", re.I)
MEETING_ASSISTANT_ACTIVE_RE = re.compile(
    r"(?:会话中|正在(?:记录|转写|听取|面试)|录音中|转写中|"
    r"\b(?:session\s+active|live\s+session|listening|recording|transcribing|in\s+progress)\b)",
    re.I,
)
REALTIME_SESSION_APP_RE = re.compile(
    r"(?:tencentmeeting|腾讯会议|zoom|microsoft teams|google meet|facetime|飞书会议)",
    re.I,
)
BUSINESS_ROLE_RE = re.compile(
    r"(?:海外|国际|全球|本地化|销售|渠道|商务|客户|市场|运营|产品|"
    r"\bBD\b|\bGTM\b|\bPOS\b|\bsales\b|\bchannel\b|\bbusiness\b)",
    re.I,
)
INTERVIEW_ACTIVE_RE = re.compile(
    r"(?:正在(?:面试|问答)|面试中|候选人正在回答|"
    r"\b(?:interview|candidate\s+conversation)\s+(?:active|ongoing|in\s+progress)\b)",
    re.I,
)
CALL_ACTION_RE = re.compile(
    r"(?:正在通话|通话中|已接通|通话结束|拨打|接听|"
    r"\b(?:in\s+(?:a\s+)?call|call\s+(?:active|connected|ended)|dial(?:ed|ing)?|answered)\b)",
    re.I,
)
APPLICATION_ACTION_RE = re.compile(
    r"(?:投递|申请职位|点击申请|正在申请|"
    r"\b(?:apply|applying|application\s+(?:submitted|sent|in\s+progress))\b)",
    re.I,
)
SUBMISSION_ACTION_RE = re.compile(
    r"(?:提交|确认提交|\b(?:submit|submitting|submitted|submission\s+(?:sent|complete))\b)",
    re.I,
)
SEND_ACTION_RE = re.compile(
    r"(?:发送|发出|已发|点击发送|\b(?:send|sending|sent)\b)",
    re.I,
)
PUBLISH_ACTION_RE = re.compile(
    r"(?:发布|上线|推送|\b(?:publish|publishing|published|go(?:ing)?\s+live)\b)",
    re.I,
)
HIGH_VALUE_NEGATION_RE = re.compile(
    r"(?:没有|并未|未曾|从未|尚未|没).{0,18}"
    r"(?:参加|进行|发生|完成|提交|发送|投递|申请|发布|通话|面试|会议)|"
    r"(?:面试|会议|通话|投递|申请|提交|发送|发布).{0,18}"
    r"(?:没有|并未|未曾|尚未).{0,8}(?:发生|完成|进行)|"
    r"\b(?:did\s+not|didn't|never|has\s+not|have\s+not|not\s+yet)\b.{0,40}"
    r"\b(?:interview|meeting|call|apply|application|submit|send|publish)\b",
    re.I,
)
ACTIVE_APPLICATION_RE = re.compile(
    r"(?:已投递|投递成功|点击申请|正在申请|申请成功|"
    r"\b(?:applying|application\s+(?:submitted|sent|in\s+progress))\b)",
    re.I,
)
ACTIVE_SUBMISSION_RE = re.compile(
    r"(?:已提交|提交成功|确认提交|正在提交|"
    r"\b(?:submitting|submitted|submission\s+(?:sent|complete|in\s+progress))\b)",
    re.I,
)
ACTIVE_SEND_RE = re.compile(
    r"(?:已发送|发送成功|点击发送|正在发送|"
    r"\b(?:sending|message\s+sent|email\s+sent)\b)",
    re.I,
)
ACTIVE_PUBLISH_RE = re.compile(
    r"(?:已发布|发布成功|正在发布|已上线|正式上线|"
    r"\b(?:publishing|published|went\s+live|going\s+live)\b)",
    re.I,
)
SESSION_TERMINAL_RE = re.compile(
    r"(?:会话已结束|会议结束|通话结束|已停止转写|"
    r"\b(?:session|meeting|call)\s+(?:ended|closed|completed)\b)",
    re.I,
)
SESSION_ID_RE = re.compile(
    r"\b(?:session|meeting|call)[ _-]?(?:id)?\s*[:=#]\s*([A-Za-z0-9_-]{4,40})\b",
    re.I,
)
CONFIRMED_SELF_SUBJECT_RE = re.compile(
    r"(?:\bAlex\b|\bLocal\s+Owner\b|本人|我的|我在|自用|自我介绍)",
    re.I,
)


def upsert_activity_confirmation_candidate(
    conn: sqlite3.Connection,
    activity: dict[str, Any],
) -> dict[str, Any] | None:
    candidate = _candidate_for_activity(conn, activity)
    if candidate is None:
        return None
    return _upsert_confirmation_candidate(conn, candidate)


def _finalize_confirmation_candidate(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Apply the same question-safety result before any candidate is stored."""
    output = dict(candidate)
    reason_codes = _unique(
        [
            *_json_list(output.get("reason_codes")),
            CONFIRMATION_POLICY_EVALUATED_REASON,
            CONFIRMATION_POLICY_QUESTION_SAFETY_REASON,
        ]
    )
    output["reason_codes"] = reason_codes
    if str(output.get("candidate_origin") or "") == "manual":
        return output
    # Screen/App/window/OCR-derived candidates are observations, not proof of
    # occurrence.  Only a later explicit user answer may move the durable row
    # to confirmed; candidate construction itself is permanently fail-closed.
    if str(output.get("state") or "pending") == "confirmed":
        output["state"] = "pending"
        output["notification_eligible"] = False
        reason_codes = _unique([*reason_codes, "auto_confirmation_blocked_unverified_source"])
        output["reason_codes"] = reason_codes
    assessment = _confirmation_question_assessment(
        event_type=str(output.get("event_type") or ""),
        subject=_json_object(output.get("subject")),
        evidence=safe_decision_text(output.get("evidence") or "", max_len=600),
        full_text=safe_decision_text(
            output.get("cleaned_content")
            or output.get("safe_observation")
            or "",
            max_len=1600,
        ),
        app_family=str(output.get("app_family") or ""),
        reason_codes=reason_codes,
        safe_signal_types=_safe_tokens(output.get("safe_signal_types")),
        question_kind=str(output.get("question_kind") or "occurrence"),
    )
    output["subject"] = dict(assessment["subject"])
    status = str(assessment["status"])
    reason = str(assessment["reason"])
    if status == "filtered":
        output["state"] = "filtered"
        output["confidence"] = min(float(output.get("confidence") or 0.0), 0.05)
        output["reason_codes"] = _unique(
            [
                *reason_codes,
                f"{CONFIRMATION_POLICY_REASON_PREFIX}_{reason}",
            ]
        )
    elif status == "information_insufficient":
        if str(output.get("state") or "pending") == "confirmed":
            output["state"] = "pending"
        output["decision_question"] = ""
        output["notification_eligible"] = False
        if reason in LOW_VALUE_QUESTION_REASONS:
            output["priority"] = "low"
        output["reason_codes"] = _unique(
            [
                *reason_codes,
                f"{CONFIRMATION_POLICY_REASON_PREFIX}_information_insufficient_{reason}",
            ]
        )
    else:
        output["decision_question"] = _decision_question(
            dict(assessment["subject"]),
            str(output.get("observed_start") or ""),
        )
    if bool(assessment.get("privacy_safe")):
        safe_summary = "招聘沟通界面中检测到送达或消息状态；联系人和消息正文不进入确认卡。"
        output["safe_observation"] = safe_summary
        output["evidence"] = "招聘沟通界面送达状态"
        output["cleaned_content"] = safe_summary
        output["suggested_workstream"] = "招聘沟通"
    return output


def _upsert_confirmation_candidate(
    conn: sqlite3.Connection,
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    candidate = _finalize_confirmation_candidate(candidate)
    # Never recreate a source-bound decision from a stale in-memory timeline
    # row after retention, manual deletion, or privacy reclassification won.
    source_activity_ids = [
        source_id for source_id in candidate["source_activity_ids"] if source_id
    ]
    if source_activity_ids:
        placeholders = ",".join("?" for _ in source_activity_ids)
        live_source_count = conn.execute(
            f"SELECT COUNT(*) FROM extracted_activities WHERE id IN ({placeholders})",
            tuple(source_activity_ids),
        ).fetchone()[0]
        if int(live_source_count or 0) != len(set(source_activity_ids)):
            return None
    existing = conn.execute(
        "SELECT * FROM activity_confirmation_items WHERE item_key=?",
        (candidate["item_key"],),
    ).fetchone()
    if existing:
        existing_dict = dict(existing)
        preserve_clarification = int(
            _public_clarification(existing_dict.get("clarification")).get("status") == "completed"
        )
        # IDs are content-free provenance. Retain the complete bounded episode
        # source set; the raw evidence itself is independently capped at three.
        activity_ids = _merged_ids(existing_dict.get("source_activity_ids"), candidate["source_activity_ids"], 512)
        deletion_groups = _merged_ids(existing_dict.get("deletion_group_ids"), candidate["deletion_group_ids"], 512)
        state = str(existing_dict.get("state") or "pending")
        if not existing_dict.get("answer"):
            if candidate["state"] == "confirmed":
                state = "confirmed"
            elif state in UNRESOLVED_STATES and candidate["state"] in {"rejected", "filtered"}:
                state = candidate["state"]
        conn.execute(
            """
            UPDATE activity_confirmation_items
            SET event_type=CASE WHEN answer IS NULL THEN ? ELSE event_type END,
                safe_observation=?,
                decision_question=CASE WHEN ?=0 THEN ? ELSE decision_question END,
                why_uncertain=?, app_family=?,
                observed_start=CASE WHEN datetime(observed_start) <= datetime(?) THEN observed_start ELSE ? END,
                observed_end=CASE WHEN datetime(observed_end) >= datetime(?) THEN observed_end ELSE ? END,
                confidence=?, reason_codes=?, region_kind=?, actionability=?,
                source_activity_ids=?, deletion_group_ids=?,
                group_key=CASE
                    WHEN answer IS NULL AND COALESCE(group_key,'')='' THEN ?
                    ELSE group_key
                END,
                group_signature=CASE
                    WHEN answer IS NULL AND ?=0 THEN ? ELSE group_signature
                END,
                subject=CASE WHEN answer IS NULL AND ?=0 THEN ? ELSE subject END,
                evidence=CASE WHEN answer IS NULL AND ?=0 THEN ? ELSE evidence END,
                cleaned_content=CASE WHEN answer IS NULL AND ?=0 THEN ? ELSE cleaned_content END,
                fingerprint=CASE WHEN answer IS NULL THEN ? ELSE fingerprint END,
                calibration_key=CASE WHEN answer IS NULL THEN ? ELSE calibration_key END,
                question_kind=CASE WHEN answer IS NULL THEN ? ELSE question_kind END,
                candidate_origin=CASE WHEN answer IS NULL THEN ? ELSE candidate_origin END,
                priority=CASE WHEN answer IS NULL THEN ? ELSE priority END,
                episode_id=CASE WHEN answer IS NULL THEN ? ELSE episode_id END,
                safe_signal_types=CASE WHEN answer IS NULL THEN ? ELSE safe_signal_types END,
                suggested_workstream=CASE WHEN answer IS NULL THEN ? ELSE suggested_workstream END,
                suggested_outcome=CASE WHEN answer IS NULL THEN ? ELSE suggested_outcome END,
                notification_eligible=CASE WHEN answer IS NULL THEN ? ELSE 0 END,
                state=?, updated_at=?
            WHERE item_key=?
            """,
            (
                candidate["event_type"],
                candidate["safe_observation"],
                preserve_clarification,
                candidate["decision_question"],
                candidate["why_uncertain"],
                candidate["app_family"],
                candidate["observed_start"],
                candidate["observed_start"],
                candidate["observed_end"],
                candidate["observed_end"],
                candidate["confidence"],
                json.dumps(candidate["reason_codes"], ensure_ascii=False),
                candidate["region_kind"],
                candidate["actionability"],
                json.dumps(activity_ids, ensure_ascii=False),
                json.dumps(deletion_groups, ensure_ascii=False),
                candidate["group_key"],
                preserve_clarification,
                candidate["group_signature"],
                preserve_clarification,
                json.dumps(candidate["subject"], ensure_ascii=False, sort_keys=True),
                preserve_clarification,
                candidate["evidence"],
                preserve_clarification,
                candidate["cleaned_content"],
                candidate["fingerprint"],
                candidate["calibration_key"],
                candidate["question_kind"],
                candidate["candidate_origin"],
                candidate["priority"],
                candidate["episode_id"],
                json.dumps(candidate["safe_signal_types"], ensure_ascii=False),
                candidate["suggested_workstream"],
                candidate["suggested_outcome"],
                int(bool(candidate["notification_eligible"])),
                state,
                _utc_now(),
                candidate["item_key"],
            ),
        )
    else:
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO activity_confirmation_items(
              item_key,event_type,safe_observation,decision_question,why_uncertain,
              app_family,observed_start,observed_end,confidence,reason_codes,
              region_kind,actionability,source_activity_ids,deletion_group_ids,
              fingerprint,calibration_key,group_key,group_signature,subject,evidence,
              cleaned_content,clarification,question_kind,candidate_origin,priority,
              episode_id,safe_signal_types,suggested_workstream,suggested_outcome,
              user_workstream,user_outcome,notification_eligible,
              state,answer,answer_reason,user_note,
              generalize_scope,created_at,updated_at,answered_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                candidate["item_key"], candidate["event_type"], candidate["safe_observation"],
                candidate["decision_question"], candidate["why_uncertain"], candidate["app_family"],
                candidate["observed_start"], candidate["observed_end"], candidate["confidence"],
                json.dumps(candidate["reason_codes"], ensure_ascii=False), candidate["region_kind"],
                candidate["actionability"], json.dumps(candidate["source_activity_ids"], ensure_ascii=False),
                json.dumps(candidate["deletion_group_ids"], ensure_ascii=False), candidate["fingerprint"],
                candidate["calibration_key"], candidate["group_key"], candidate["group_signature"],
                json.dumps(candidate["subject"], ensure_ascii=False, sort_keys=True),
                candidate["evidence"], candidate["cleaned_content"], "{}",
                candidate["question_kind"], candidate["candidate_origin"], candidate["priority"],
                candidate["episode_id"], json.dumps(candidate["safe_signal_types"], ensure_ascii=False),
                candidate["suggested_workstream"], candidate["suggested_outcome"], "", "",
                int(bool(candidate["notification_eligible"])), candidate["state"],
                None, None, "", "exact", now, now, None,
            ),
        )
    row = conn.execute(
        "SELECT * FROM activity_confirmation_items WHERE item_key=?",
        (candidate["item_key"],),
    ).fetchone()
    return _public_item(row, conn=conn) if row else None


def materialize_activity_confirmations(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    commit: bool = True,
) -> int:
    from .pipeline import timeline

    started_transaction = not conn.in_transaction
    try:
        # A write-intent transaction makes the timeline read and every
        # source-bound upsert atomic against delete_range/reclassification.
        # If deletion commits first, this reader sees no source; if this writer
        # commits first, deletion subsequently removes both source and card.
        if started_transaction:
            conn.execute("BEGIN IMMEDIATE")
        count = 0
        activities = timeline(conn, start, end)
        for activity in activities:
            if activity.get("quarantine_status") != "clear":
                continue
            if upsert_activity_confirmation_candidate(conn, activity):
                count += 1
        # Materialization normally runs on a very small incremental capture
        # window.  Regrouping only that window leaves an older same-day card
        # invisible to the new row, so equivalent facts accumulate as separate
        # questions.  Revisit every affected local day in this writer
        # transaction; the public list path remains strictly read-only.
        for day_start, day_end in _affected_local_day_windows(start, end):
            day_activities = [
                item
                for item in timeline(conn, day_start, day_end)
                if item.get("quarantine_status") == "clear"
            ]
            count += _materialize_business_episodes(
                conn,
                day_activities,
                start=day_start,
                end=day_end,
            )
            _regroup_confirmation_items(
                conn,
                start=day_start,
                end=day_end,
                unresolved_only=True,
            )
        if commit:
            conn.commit()
        return count
    except Exception:
        if started_transaction and conn.in_transaction:
            conn.rollback()
        raise


def materialize_business_episodes_for_window(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    commit: bool = False,
) -> int:
    """Incrementally reconcile source-bound episodes while raw frames still exist."""
    from .pipeline import timeline

    total = 0
    started_transaction = not conn.in_transaction
    try:
        if started_transaction:
            conn.execute("BEGIN IMMEDIATE")
        for day_start, day_end in _affected_local_day_windows(start, end):
            activities = [
                item
                for item in timeline(conn, day_start, day_end)
                if item.get("quarantine_status") == "clear"
            ]
            total += _materialize_business_episodes(
                conn,
                activities,
                start=day_start,
                end=day_end,
            )
            _regroup_confirmation_items(
                conn,
                start=day_start,
                end=day_end,
                unresolved_only=True,
            )
        if started_transaction and commit:
            conn.commit()
        return total
    except Exception:
        if started_transaction and conn.in_transaction:
            conn.rollback()
        raise


def get_activity_confirmation_mode(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (CONFIRMATION_MODE_SETTING_KEY,),
    ).fetchone()
    mode = str(row["value"] if row else DEFAULT_CONFIRMATION_MODE)
    return mode if mode in CONFIRMATION_MODES else DEFAULT_CONFIRMATION_MODE


def configure_activity_confirmations(conn: sqlite3.Connection, *, mode: str) -> dict[str, Any]:
    mode = str(mode or "").strip()
    if mode not in CONFIRMATION_MODES:
        raise ValueError("unsupported activity confirmation mode")
    now = _utc_now()
    with document_authorization_lock(conn, exclusive=False):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (CONFIRMATION_MODE_SETTING_KEY, mode, now),
            )
            for priority in PRIORITIES:
                conn.execute(
                    "UPDATE activity_confirmation_items SET notification_eligible=? "
                    "WHERE state IN ('pending','deferred') AND priority=?",
                    (int(_notification_eligible(mode, priority)), priority),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "status": "configured",
        "interface": INTERFACE,
        "contract_version": CONTRACT_VERSION,
        "mode": mode,
        "notification_priorities": {
            priority: _notification_eligible(mode, priority)
            for priority in ("high", "normal", "low")
        },
        **_safe_action_flags(),
    }


def _notification_eligible(mode: str, priority: str) -> bool:
    if mode == "compact":
        return priority == "high"
    if mode == "max_recall":
        return priority in PRIORITIES
    return priority in {"high", "normal"}


def _materialize_business_episodes(
    conn: sqlite3.Connection,
    activities: list[dict[str, Any]],
    *,
    start: str,
    end: str,
) -> int:
    desired: dict[str, dict[str, Any]] = {}
    mode = get_activity_confirmation_mode(conn)
    ordinary_cap = {"compact": 5, "standard": 12, "max_recall": 24}[mode]
    existing = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM activity_confirmation_items "
            "WHERE candidate_origin='business_episode' "
            "AND datetime(observed_start)<datetime(?) AND datetime(observed_end)>datetime(?)",
            (end, start),
        ).fetchall()
    ]
    episode_diagnostics: dict[str, Any] = {
        "hard_filtered": 0,
        "filter_reason_counts": {},
    }
    high_value_candidates: list[dict[str, Any]] = []
    ordinary_candidates: list[dict[str, Any]] = []
    for cluster in _business_episode_clusters(activities):
        candidate = _business_episode_candidate(
            conn,
            cluster,
            diagnostics=episode_diagnostics,
        )
        if candidate is not None:
            if candidate["question_kind"] == "business_context":
                ordinary_candidates.append(candidate)
            else:
                high_value_candidates.append(candidate)
    ordinary_by_key = {
        str(candidate["item_key"]): candidate for candidate in ordinary_candidates
    }
    # Once an ordinary pending question has been shown, keep it stable. New
    # same-day candidates may fill unused capacity but never reshuffle an
    # existing unresolved card out of the queue.
    frozen_ordinary_keys = {
        str(row["item_key"])
        for row in existing
        if str(row.get("question_kind") or "") == "business_context"
        and str(row.get("state") or "") in UNRESOLVED_STATES
    }
    frozen_current = [
        ordinary_by_key[key]
        for key in sorted(frozen_ordinary_keys)
        if key in ordinary_by_key
    ]
    new_ordinary = [
        candidate
        for candidate in ordinary_candidates
        if str(candidate["item_key"]) not in frozen_ordinary_keys
    ]
    remaining_capacity = max(0, ordinary_cap - len(frozen_ordinary_keys))
    sampled_ordinary = [
        *frozen_current,
        *_stratified_episode_sample(new_ordinary, remaining_capacity),
    ]
    for candidate in [*high_value_candidates, *sampled_ordinary]:
        desired[candidate["item_key"]] = candidate
    selected_ordinary_keys = {
        str(candidate["item_key"]) for candidate in sampled_ordinary
    }
    sampled_out = sum(
        str(candidate["item_key"]) not in selected_ordinary_keys
        for candidate in ordinary_candidates
    )
    sample_day = _local_date_for_range(start)
    conn.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (f"activity_confirmation_sampled_out:{sample_day}", str(sampled_out), _utc_now()),
    )
    conn.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (
            f"activity_confirmation_episode_diagnostics:{sample_day}",
            json.dumps(episode_diagnostics, ensure_ascii=False, sort_keys=True),
            _utc_now(),
        ),
    )
    stale_occurrence_keys = [
        str(row["item_key"])
        for row in existing
        if str(row.get("state") or "") in UNRESOLVED_STATES
        and str(row.get("question_kind") or "") == "occurrence"
        and str(row["item_key"]) not in desired
    ]
    if stale_occurrence_keys:
        schedule_evidence_release(
            conn,
            stale_occurrence_keys,
            reason="episode_reconciled_filtered",
        )
        placeholders = ",".join("?" for _ in stale_occurrence_keys)
        conn.execute(
            f"UPDATE activity_confirmation_items SET state='filtered',"
            f"notification_eligible=0,reason_codes=?,updated_at=? "
            f"WHERE item_key IN ({placeholders}) AND state IN ('pending','deferred')",
            (
                json.dumps(
                    ["business_episode_reconciled_filtered", CONFIRMATION_POLICY_EVALUATED_REASON],
                    ensure_ascii=False,
                ),
                _utc_now(),
                *stale_occurrence_keys,
            ),
        )
    count = 0
    for candidate in desired.values():
        if _upsert_confirmation_candidate(conn, candidate):
            count += 1
    return count


def _stratified_episode_sample(
    candidates: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    ordered = sorted(
        candidates,
        key=lambda item: (str(item.get("observed_start") or ""), str(item.get("item_key") or "")),
    )
    if limit <= 0:
        return []
    if len(ordered) <= limit:
        return ordered
    chosen: list[dict[str, Any]] = []
    remaining = list(enumerate(ordered))
    app_counts: dict[str, int] = {}
    segment_counts: dict[int, int] = {}
    priority_rank = {"high": 0, "normal": 1, "low": 2}
    targets = [index * (len(ordered) - 1) / max(1, limit - 1) for index in range(limit)]
    for target in targets:
        def score(entry: tuple[int, dict[str, Any]]) -> tuple[float, int, int, int, str]:
            index, candidate = entry
            app = _normalized_app_family(str(candidate.get("app_family") or "unknown"))
            try:
                segment = _local_datetime(str(candidate.get("observed_start") or "")).hour // 4
            except ValueError:
                segment = 0
            return (
                abs(index - target),
                segment_counts.get(segment, 0),
                app_counts.get(app, 0),
                priority_rank.get(str(candidate.get("priority") or "low"), 3),
                str(candidate.get("item_key") or ""),
            )

        picked = min(remaining, key=score)
        remaining.remove(picked)
        candidate = picked[1]
        chosen.append(candidate)
        app = _normalized_app_family(str(candidate.get("app_family") or "unknown"))
        try:
            segment = _local_datetime(str(candidate.get("observed_start") or "")).hour // 4
        except ValueError:
            segment = 0
        app_counts[app] = app_counts.get(app, 0) + 1
        segment_counts[segment] = segment_counts.get(segment, 0) + 1
    return sorted(chosen, key=lambda item: (str(item.get("observed_start") or ""), str(item.get("item_key") or "")))


def _business_episode_clusters(
    activities: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    # Only an independently-active session/action advances a high-value
    # episode. Nearby weak frames may enrich context, but never extend the
    # anchor clock or bridge separate sessions.
    ordered: list[tuple[datetime, datetime, dict[str, Any], dict[str, Any]]] = []
    for activity in activities:
        profile = _episode_anchor_profile(activity)
        try:
            observed_start = _local_datetime(str(activity.get("started_at") or ""))
            observed_end = _local_datetime(
                str(activity.get("ended_at") or activity.get("started_at") or "")
            )
        except ValueError:
            continue
        ordered.append((observed_start, max(observed_start, observed_end), activity, profile))
    ordered.sort(key=lambda item: (item[0], str(item[2].get("id") or "")))

    def build_anchor_clusters(*, strong: bool) -> list[dict[str, Any]]:
        clusters: list[dict[str, Any]] = []
        for observed_start, observed_end, activity, profile in ordered:
            if not profile.get("is_anchor") or bool(profile.get("is_strong")) != strong:
                continue
            current = clusters[-1] if clusters else None
            split = (
                current is None
                or observed_start.date() != current["day"]
                or observed_start - current["anchor_end"] > BUSINESS_EPISODE_GAP
                or _episode_profiles_conflict(current["profiles"], profile)
            )
            maximum_span = timedelta(hours=3) if strong else timedelta(minutes=30)
            if not split and observed_end - current["anchor_start"] > maximum_span:
                split = True
            marked = dict(activity)
            marked["_business_episode_anchor"] = True
            marked["_business_episode_strong_anchor"] = strong
            marked["_business_episode_profile"] = profile
            if split:
                clusters.append(
                    {
                        "activities": [marked],
                        "anchor_start": observed_start,
                        "anchor_end": observed_end,
                        "day": observed_start.date(),
                        "profiles": [profile],
                    }
                )
                continue
            current["activities"].append(marked)
            current["anchor_end"] = max(current["anchor_end"], observed_end)
            current["profiles"].append(profile)
        return clusters

    strong_clusters = build_anchor_clusters(strong=True)
    claimed_context_ids: set[str] = set()
    for observed_start, observed_end, activity, profile in ordered:
        if profile.get("is_strong"):
            continue
        nearest: tuple[timedelta, dict[str, Any]] | None = None
        for cluster in strong_clusters:
            if observed_start.date() != cluster["day"]:
                continue
            if observed_end < cluster["anchor_start"]:
                distance = cluster["anchor_start"] - observed_end
            elif observed_start > cluster["anchor_end"]:
                distance = observed_start - cluster["anchor_end"]
            else:
                distance = timedelta(0)
            if distance > BUSINESS_EPISODE_GAP:
                continue
            if _episode_context_conflicts(cluster["profiles"], profile):
                continue
            if nearest is None or distance < nearest[0]:
                nearest = (distance, cluster)
        if nearest is None:
            continue
        marked = dict(activity)
        marked["_business_episode_anchor"] = False
        marked["_business_episode_strong_anchor"] = False
        marked["_business_episode_profile"] = profile
        nearest[1]["activities"].append(marked)
        source_id = str(activity.get("id") or "")
        if source_id:
            claimed_context_ids.add(source_id)

    ordinary_clusters = build_anchor_clusters(strong=False)
    # Ordinary business sampling also needs the immediately-adjacent safe
    # context that explains the activity.  Attach non-anchor frames to the
    # nearest ordinary anchor without advancing its clock; this recovers a
    # two-frame research/editing episode while still preventing weak context
    # chains from bridging separate sessions.
    for observed_start, observed_end, activity, profile in ordered:
        source_id = str(activity.get("id") or "")
        if profile.get("is_anchor") or source_id in claimed_context_ids:
            continue
        nearest: tuple[timedelta, dict[str, Any]] | None = None
        for cluster in ordinary_clusters:
            if observed_start.date() != cluster["day"]:
                continue
            anchor_apps = {
                str(anchor_profile.get("app") or "")
                for anchor_profile in cluster["profiles"]
                if anchor_profile.get("app")
            }
            if profile.get("app") and anchor_apps and str(profile["app"]) not in anchor_apps:
                continue
            if observed_end < cluster["anchor_start"]:
                distance = cluster["anchor_start"] - observed_end
            elif observed_start > cluster["anchor_end"]:
                distance = observed_start - cluster["anchor_end"]
            else:
                distance = timedelta(0)
            if distance > BUSINESS_EPISODE_GAP:
                continue
            if _episode_context_conflicts(cluster["profiles"], profile):
                continue
            if nearest is None or distance < nearest[0]:
                nearest = (distance, cluster)
        if nearest is None:
            continue
        marked = dict(activity)
        marked["_business_episode_anchor"] = False
        marked["_business_episode_strong_anchor"] = False
        marked["_business_episode_profile"] = profile
        nearest[1]["activities"].append(marked)
    filtered_ordinary: list[dict[str, Any]] = []
    for cluster in ordinary_clusters:
        retained = [
            activity
            for activity in cluster["activities"]
            if str(activity.get("id") or "") not in claimed_context_ids
        ]
        if retained:
            cluster["activities"] = retained
            filtered_ordinary.append(cluster)

    clusters = [*strong_clusters, *filtered_ordinary]
    clusters.sort(
        key=lambda cluster: (
            cluster["anchor_start"],
            str(cluster["activities"][0].get("id") or ""),
        )
    )
    for cluster in clusters:
        cluster["activities"].sort(
            key=lambda activity: (
                str(activity.get("started_at") or ""),
                str(activity.get("id") or ""),
            )
        )
    return [cluster["activities"] for cluster in clusters]


def _episode_anchor_profile(activity: dict[str, Any]) -> dict[str, Any]:
    text = safe_decision_text(
        " ".join(
            part
            for part in (
                activity.get("cleaned_text") or "",
                activity.get("activity_summary") or "",
                activity.get("cleaned_title") or "",
            )
            if part
        ),
        max_len=1200,
    )
    apps = _json_list(activity.get("apps"))
    app_text = " ".join(apps)
    region = _region_signal(activity.get("screen_region_signal"))
    active_reason = bool(
        {
            "session_active",
            "meeting_active",
            "call_active",
            "timer_running",
            "recording_active",
        }.intersection(region["reason_codes"])
    )
    assistant_active = bool(
        MEETING_ASSISTANT_APP_RE.search(app_text)
        and (MEETING_ASSISTANT_ACTIVE_RE.search(text) or active_reason)
    )
    realtime_active = bool(
        REALTIME_SESSION_APP_RE.search(app_text)
        and (
            REALTIME_EVENT_STATE_RE.search(text)
            or MEETING_ASSISTANT_ACTIVE_RE.search(text)
            or active_reason
        )
    )
    explicit_interview_active = bool(INTERVIEW_ACTIVE_RE.search(text))
    event_hint = ""
    if assistant_active and (WEAK_INTERVIEW_RE.search(text) or BUSINESS_ROLE_RE.search(text)):
        event_hint = "interview"
    elif assistant_active:
        event_hint = "meeting"
    elif realtime_active and (explicit_interview_active or WEAK_INTERVIEW_RE.search(text)):
        event_hint = "interview"
    elif realtime_active and CALL_ACTION_RE.search(text):
        event_hint = "call"
    elif realtime_active:
        event_hint = "meeting"
    elif explicit_interview_active:
        event_hint = "interview"
    elif ACTIVE_APPLICATION_RE.search(text):
        event_hint = "application"
    elif ACTIVE_SUBMISSION_RE.search(text):
        event_hint = "submission"
    elif ACTIVE_SEND_RE.search(text):
        event_hint = "send"
    elif ACTIVE_PUBLISH_RE.search(text):
        event_hint = "publish"
    elif CALL_ACTION_RE.search(text):
        event_hint = "call"
    is_strong = bool(event_hint in HIGH_IMPACT_EVENT_TYPES)
    if not event_hint and (
        BUSINESS_ROLE_RE.search(text)
        or WEAK_INTERVIEW_RE.search(text)
        or WEAK_MEETING_RE.search(text)
        or WEAK_CALL_RE.search(text)
        or WEAK_APPLICATION_RE.search(text)
        or WEAK_SUBMISSION_RE.search(text)
        or WEAK_SEND_RE.search(text)
        or WEAK_PUBLISH_RE.search(text)
    ):
        event_hint = "business_context"
    session_match = SESSION_ID_RE.search(text)
    return {
        "is_anchor": bool(event_hint),
        "is_strong": is_strong,
        "event_hint": event_hint,
        "subject": _normalized_phrase(_extract_business_entity(text)),
        "app": _normalized_app_family(apps[0] if apps else "unknown"),
        "session_key": _normalized_phrase(session_match.group(1)) if session_match else "",
        "session_phase": (
            "terminal"
            if SESSION_TERMINAL_RE.search(text)
            else "active"
            if is_strong
            else ""
        ),
    }


def _episode_profiles_conflict(
    existing: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> bool:
    if not existing:
        return False
    prior_high = next(
        (
            profile["event_hint"]
            for profile in reversed(existing)
            if profile.get("event_hint") in HIGH_IMPACT_EVENT_TYPES
        ),
        "",
    )
    candidate_hint = candidate.get("event_hint") or ""
    if prior_high and candidate_hint in HIGH_IMPACT_EVENT_TYPES and prior_high != candidate_hint:
        return True
    prior_session = next(
        (
            str(profile.get("session_key") or "")
            for profile in reversed(existing)
            if profile.get("session_key")
        ),
        "",
    )
    candidate_session = str(candidate.get("session_key") or "")
    if prior_session and candidate_session and prior_session != candidate_session:
        return True
    if (
        str(existing[-1].get("session_phase") or "") == "terminal"
        and str(candidate.get("session_phase") or "") == "active"
    ):
        return True
    prior_subject = next(
        (profile["subject"] for profile in reversed(existing) if profile.get("subject")),
        "",
    )
    candidate_subject = candidate.get("subject") or ""
    return bool(prior_subject and candidate_subject and prior_subject != candidate_subject)


def _episode_context_conflicts(
    existing: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> bool:
    prior_subject = next(
        (
            str(profile.get("subject") or "")
            for profile in reversed(existing)
            if profile.get("subject")
        ),
        "",
    )
    candidate_subject = str(candidate.get("subject") or "")
    return bool(prior_subject and candidate_subject and prior_subject != candidate_subject)


def _business_episode_candidate(
    conn: sqlite3.Connection,
    activities: list[dict[str, Any]],
    *,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not activities:
        return None
    strong_anchors = [
        activity
        for activity in activities
        if bool(activity.get("_business_episode_strong_anchor"))
    ]
    anchor_activities = strong_anchors or [
        activity for activity in activities if bool(activity.get("_business_episode_anchor"))
    ]
    if not anchor_activities:
        return None
    safe_parts: list[str] = []
    apps: list[str] = []
    actionable = False
    hard_region_count = 0
    structured_signal = False
    submit_surface = False
    session_state_signal = False
    anchor_session_state_signal = False
    source_activity_ids: list[str] = []
    deletion_group_ids: list[str] = []
    for activity in activities:
        safe_parts.extend(
            part
            for part in (
                safe_decision_text(activity.get("cleaned_text") or "", max_len=700),
                safe_decision_text(activity.get("activity_summary") or "", max_len=420),
                safe_decision_text(activity.get("cleaned_title") or "", max_len=180),
            )
            if part
        )
        apps.extend(_json_list(activity.get("apps")))
        region = _region_signal(activity.get("screen_region_signal"))
        if region["region_kind"] in NON_ACTIONABLE_REGION_KINDS:
            hard_region_count += 1
        elif region["actionability"] not in {"noise"}:
            actionable = True
        if region["region_kind"] in {"submit_area", "main_content", "document_body", "conversation"}:
            structured_signal = True
        if region["region_kind"] == "submit_area" or bool(
            DIRECT_REASON_CODES.intersection(region["reason_codes"])
        ):
            submit_surface = True
        if {
            "session_active",
            "meeting_active",
            "call_active",
            "timer_running",
            "recording_active",
        }.intersection(region["reason_codes"]):
            session_state_signal = True
            if activity in anchor_activities:
                anchor_session_state_signal = True
        if activity.get("document_contexts") or _json_list(activity.get("domains")):
            structured_signal = True
        source_id = safe_decision_text(activity.get("id") or "", max_len=120)
        deletion_group_id = safe_decision_text(
            activity.get("deletion_group_id") or "",
            max_len=120,
        )
        if source_id:
            source_activity_ids.append(source_id)
        if deletion_group_id:
            deletion_group_ids.append(deletion_group_id)
    safe_parts = _unique(safe_parts)
    apps = _unique([safe_decision_text(app, max_len=80) for app in apps])
    combined = safe_decision_text("\n".join(safe_parts), max_len=2200)
    app_text = " ".join(apps)
    anchor_parts = _unique(
        [
            safe_decision_text(part, max_len=700)
            for activity in anchor_activities
            for part in (
                activity.get("cleaned_text") or "",
                activity.get("activity_summary") or "",
                activity.get("cleaned_title") or "",
            )
            if part
        ]
    )
    anchor_combined = safe_decision_text("\n".join(anchor_parts), max_len=1600)
    anchor_apps = _unique(
        [
            safe_decision_text(app, max_len=80)
            for activity in anchor_activities
            for app in _json_list(activity.get("apps"))
        ]
    )
    anchor_app_text = " ".join(anchor_apps)
    interview_keyword = bool(WEAK_INTERVIEW_RE.search(combined))
    meeting_keyword = bool(WEAK_MEETING_RE.search(combined))
    call_keyword = bool(WEAK_CALL_RE.search(combined))
    application_keyword = bool(WEAK_APPLICATION_RE.search(combined))
    submission_keyword = bool(WEAK_SUBMISSION_RE.search(combined))
    send_keyword = bool(WEAK_SEND_RE.search(combined))
    publish_keyword = bool(WEAK_PUBLISH_RE.search(combined))
    meeting_assistant = bool(MEETING_ASSISTANT_APP_RE.search(app_text))
    realtime_app = bool(REALTIME_SESSION_APP_RE.search(app_text))
    anchor_meeting_assistant = bool(MEETING_ASSISTANT_APP_RE.search(anchor_app_text))
    anchor_realtime_app = bool(REALTIME_SESSION_APP_RE.search(anchor_app_text))
    turn_count = len(MULTI_TURN_RE.findall(combined))
    multi_turn = turn_count >= 2 or (
        len(activities) >= 3
        and bool(re.search(r"(?:问题|回答|\bquestion\b|\banswer\b)", combined, re.I))
    )
    business_context = bool(BUSINESS_ROLE_RE.search(combined))
    preparation_only = bool(PREPARATION_OR_JD_RE.search(combined))
    future_only = bool(FUTURE_SESSION_RE.search(combined))
    old_or_navigation = bool(
        OLD_OR_NAVIGATION_RE.search(combined)
        or INTERVIEW_PAST_REFERENCE_RE.search(combined)
    )
    application_action = bool(APPLICATION_ACTION_RE.search(combined))
    submission_action = bool(SUBMISSION_ACTION_RE.search(combined))
    send_action = bool(SEND_ACTION_RE.search(combined))
    publish_action = bool(PUBLISH_ACTION_RE.search(combined))
    call_action = bool(CALL_ACTION_RE.search(combined))
    explicit_interview_active = bool(INTERVIEW_ACTIVE_RE.search(combined))
    session_state_signal = bool(
        anchor_session_state_signal
        or MEETING_ASSISTANT_ACTIVE_RE.search(anchor_combined)
        or REALTIME_EVENT_STATE_RE.search(anchor_combined)
    )
    strong_hints = _unique(
        [
            str((activity.get("_business_episode_profile") or {}).get("event_hint") or "")
            for activity in strong_anchors
        ]
    )
    potential_signal = bool(
        interview_keyword
        or meeting_keyword
        or call_keyword
        or application_keyword
        or submission_keyword
        or send_keyword
        or publish_keyword
        or business_context
        or (session_state_signal and (meeting_assistant or realtime_app))
    )

    def hard_filter(reason: str) -> None:
        if potential_signal:
            _record_episode_filter(diagnostics, reason)

    # App attribution around native accessibility surfaces is frequently the
    # generic "Accessibility Services" process.  Re-run the content-level
    # policy after the episode has been aggregated so PAM's own pending UI,
    # meta discussions, and tool-status text cannot loop back as a new card.
    if CONFIRMATION_UI_RE.search(combined):
        hard_filter("self_ui")
        return None
    if META_DISCUSSION_RE.search(combined):
        hard_filter("meta_discussion")
        return None
    if _looks_like_tool_status(app_family=" ".join(apps), value=combined):
        hard_filter("tool_status")
        return None
    if not combined and not (
        session_state_signal and (meeting_assistant or realtime_app) and len(activities) >= 2
    ):
        hard_filter("insufficient_session_state")
        return None
    if hard_region_count == len(activities):
        hard_filter("non_actionable_region")
        return None
    if apps and all(_is_personal_ai_monitor_app(app) for app in apps):
        hard_filter("self_ui")
        return None
    if any(_looks_like_game_ui(app, combined) for app in (apps or ["Unknown"])):
        hard_filter("game_ui")
        return None
    if _only_negated_high_value(anchor_combined or combined) and not session_state_signal:
        hard_filter("explicit_negation")
        return None

    signal_types: list[str] = ["post_firewall_activity"]
    if interview_keyword:
        signal_types.append("interview_language")
    if meeting_keyword:
        signal_types.append("meeting_language")
    if call_keyword:
        signal_types.append("call_context")
    if application_keyword:
        signal_types.append("application_context")
    if submission_keyword:
        signal_types.append("submission_context")
    if send_keyword:
        signal_types.append("send_context")
    if publish_keyword:
        signal_types.append("publish_context")
    if meeting_assistant:
        signal_types.append("meeting_assistant_present")
    if meeting_assistant and session_state_signal:
        signal_types.append("meeting_assistant_active")
    if realtime_app:
        signal_types.append("realtime_session_app")
    if multi_turn:
        signal_types.append("multi_turn_exchange")
    if business_context:
        signal_types.append("business_context")
    if structured_signal:
        signal_types.append("structured_surface")
    if submit_surface:
        signal_types.append("submit_surface")

    duration = max(
        timedelta(0),
        max(
            _local_datetime(str(item.get("ended_at") or item.get("started_at") or ""))
            for item in anchor_activities
        )
        - min(_local_datetime(str(item.get("started_at") or "")) for item in anchor_activities),
    )
    active_assistant_episode = bool(
        strong_anchors
        and anchor_meeting_assistant
        and session_state_signal
        and (multi_turn or duration >= timedelta(minutes=2))
        and (business_context or interview_keyword or meeting_keyword)
    )
    active_realtime_episode = bool(
        strong_anchors
        and anchor_realtime_app
        and session_state_signal
        and (len(anchor_activities) >= 2 or multi_turn or duration >= timedelta(minutes=2))
    )
    force_ordinary_context = bool(
        not strong_anchors
        or (
            (preparation_only or future_only or old_or_navigation)
            and not (active_assistant_episode or active_realtime_episode)
        )
    )
    if force_ordinary_context:
        event_type = "business_context"
        signal_types.append("context_reference_only")
    elif active_assistant_episode:
        event_type = "interview"
    elif active_realtime_episode and call_action:
        event_type = "call"
    elif active_realtime_episode and interview_keyword and (multi_turn or explicit_interview_active):
        event_type = "interview"
    elif active_realtime_episode:
        event_type = "meeting"
    elif "interview" in strong_hints and explicit_interview_active:
        event_type = "interview"
    elif "meeting" in strong_hints and session_state_signal:
        event_type = "meeting"
    elif "call" in strong_hints:
        event_type = "call"
    elif "application" in strong_hints:
        event_type = "application"
    elif "submission" in strong_hints:
        event_type = "submission"
    elif "send" in strong_hints:
        event_type = "send"
    elif "publish" in strong_hints:
        event_type = "publish"
    elif potential_signal:
        event_type = "business_context"
    else:
        return None

    observed_start = min(str(item.get("started_at") or "") for item in anchor_activities)
    observed_end = max(
        str(item.get("ended_at") or item.get("started_at") or "")
        for item in anchor_activities
    )
    app_family = next(
        (app for app in apps if not _unknown_app(_normalized_app_family(app))),
        apps[0] if apps else "Unknown",
    )
    event_label = _event_label(event_type)
    evidence_pattern = {
        "interview": WEAK_INTERVIEW_RE,
        "meeting": WEAK_MEETING_RE,
        "call": WEAK_CALL_RE,
        "application": WEAK_APPLICATION_RE,
        "submission": WEAK_SUBMISSION_RE,
        "send": WEAK_SEND_RE,
        "publish": WEAK_PUBLISH_RE,
    }.get(event_type, BUSINESS_ROLE_RE)
    evidence = next(
        (
            safe_decision_text(part, max_len=280)
            for part in safe_parts
            if evidence_pattern.search(part)
            and not (
                event_type in HIGH_IMPACT_EVENT_TYPES
                and _only_negated_high_value(part)
            )
        ),
        safe_decision_text(safe_parts[0], max_len=280),
    )
    episode_match = _event_match([evidence, combined])
    trigger_verified = bool(episode_match and episode_match[0] == event_type)
    subject = _episode_subject(event_type, event_label, evidence, combined)
    workstream = _suggested_workstream(subject, combined)
    high_value = event_type in {"interview", "meeting", "call", "application", "submission", "send", "publish"}
    priority = "high" if high_value else "normal"
    if force_ordinary_context:
        priority = "low"
    if not actionable:
        if not high_value:
            priority = "low"
        signal_types.append("context_only_sampling")
    if not _subject_is_identified(subject) and not (active_assistant_episode or active_realtime_episode):
        if not high_value:
            priority = "low"
        signal_types.append("anonymous_subject")
    strong_source_ids = _unique(
        [
            safe_decision_text(activity.get("id") or "", max_len=120)
            for activity in anchor_activities
            if activity.get("id")
        ]
    )
    stable_source_anchor = strong_source_ids[0] if strong_source_ids else (
        f"{observed_start}|{_normalized_app_family(app_family)}"
    )
    existing_identity = _existing_episode_identity(conn, source_activity_ids)
    episode_id = str(existing_identity.get("episode_id") or "") or (
        "episode_" + _hash(f"business_episode|{stable_source_anchor}")[:24]
    )
    fingerprint = _hash(f"business_episode|{episode_id}")
    item_key = "occ_" + fingerprint[:24]
    group_key = "grp_" + _hash(f"episode_group|{episode_id}")[:24]
    group_signature = "sig_" + _hash(f"episode_signature|{episode_id}")[:24]
    existing_episode = conn.execute(
        "SELECT state,answer FROM activity_confirmation_items WHERE item_key=?",
        (item_key,),
    ).fetchone()
    if existing_episode and (
        existing_episode["answer"]
        or str(existing_episode["state"] or "") not in UNRESOLVED_STATES
    ):
        _filter_terminal_episode_explicit_duplicates(
            conn,
            activities,
            episode_item_key=item_key,
        )
    # An explicit occurrence candidate owns a brand-new cluster. Once an
    # episode has been persisted, however, its stable source anchor wins so
    # incremental frames cannot revive a second question beside a prior answer.
    if not existing_episode and any(
        (explicit := _candidate_for_activity(conn, activity)) is not None
        and str(explicit.get("state") or "") in {"pending", "confirmed"}
        for activity in activities
    ):
        return None
    mode = get_activity_confirmation_mode(conn)
    subject_label = safe_decision_text(subject.get("label") or event_label, max_len=100)
    question_kind = "occurrence" if high_value else "business_context"
    return _finalize_confirmation_candidate({
        "item_key": item_key,
        "event_type": event_type,
        "safe_observation": f"连续的本机安全活动显示可能存在{subject_label}。",
        "decision_question": _decision_question(subject, observed_start),
        "why_uncertain": (
            "这些线索来自相邻不超过五分钟的业务片段，但没有直接证明现实事件已经发生。"
            if question_kind == "occurrence"
            else "这些结构化安全线索可能属于一个业务工作流，需要你确认归属。"
        ),
        "app_family": app_family or "Unknown",
        "observed_start": observed_start,
        "observed_end": observed_end,
        "confidence": 0.45 if priority == "high" else 0.35 if priority == "normal" else 0.25,
        "reason_codes": _unique(
            [
                "business_episode_candidate",
                "weak_occurrence_signal",
                CONFIRMATION_POLICY_EVALUATED_REASON,
                *(
                    []
                    if trigger_verified or event_type == "business_context"
                    else [f"{CONFIRMATION_POLICY_REASON_PREFIX}_trigger_unverified"]
                ),
            ]
        ),
        "region_kind": "business_episode",
        "actionability": "review_required",
        "source_activity_ids": _unique(source_activity_ids),
        "deletion_group_ids": _unique(deletion_group_ids),
        "fingerprint": fingerprint,
        "calibration_key": "cal_" + _hash(f"business_episode|{event_type}|{_normalized_app_family(app_family)}")[:24],
        "group_key": group_key,
        "group_signature": group_signature,
        "subject": subject,
        "evidence": evidence,
        "cleaned_content": safe_decision_text("\n".join(safe_parts), max_len=1600),
        "question_kind": question_kind,
        "candidate_origin": "business_episode",
        "priority": priority,
        "episode_id": episode_id,
        "safe_signal_types": _unique(signal_types)[:12],
        "suggested_workstream": workstream,
        "suggested_outcome": "unknown",
        "notification_eligible": _notification_eligible(mode, priority),
        # Weak episode evidence is intentionally never auto-confirmed.
        "state": "pending",
    })


def _filter_terminal_episode_explicit_duplicates(
    conn: sqlite3.Connection,
    activities: list[dict[str, Any]],
    *,
    episode_item_key: str,
) -> None:
    duplicate_keys: list[str] = []
    for activity in activities:
        explicit = _candidate_for_activity(conn, activity)
        if explicit is None:
            continue
        explicit_key = str(explicit.get("item_key") or "")
        if not explicit_key or explicit_key == episode_item_key:
            continue
        row = conn.execute(
            "SELECT state,answer FROM activity_confirmation_items WHERE item_key=?",
            (explicit_key,),
        ).fetchone()
        if row and str(row["state"] or "") in UNRESOLVED_STATES and not row["answer"]:
            duplicate_keys.append(explicit_key)
    duplicate_keys = _unique(duplicate_keys)
    if not duplicate_keys:
        return
    schedule_evidence_release(
        conn,
        duplicate_keys,
        reason="terminal_episode_explicit_duplicate",
    )
    placeholders = ",".join("?" for _ in duplicate_keys)
    conn.execute(
        f"UPDATE activity_confirmation_items SET state='filtered',notification_eligible=0,"
        f"reason_codes=?,updated_at=? WHERE item_key IN ({placeholders}) "
        f"AND state IN ('pending','deferred') AND answer IS NULL",
        (
            json.dumps(
                ["terminal_episode_explicit_duplicate", CONFIRMATION_POLICY_EVALUATED_REASON],
                ensure_ascii=False,
            ),
            _utc_now(),
            *duplicate_keys,
        ),
    )


def _record_episode_filter(diagnostics: dict[str, Any] | None, reason: str) -> None:
    if diagnostics is None:
        return
    diagnostics["hard_filtered"] = int(diagnostics.get("hard_filtered") or 0) + 1
    counts = diagnostics.setdefault("filter_reason_counts", {})
    counts[reason] = int(counts.get(reason) or 0) + 1


def _existing_episode_identity(
    conn: sqlite3.Connection,
    source_activity_ids: list[str],
) -> dict[str, str]:
    source_ids = set(_unique(source_activity_ids))
    if not source_ids:
        return {}
    for row in conn.execute(
        "SELECT item_key,episode_id,source_activity_ids,state,answer "
        "FROM activity_confirmation_items "
        "WHERE candidate_origin='business_episode' "
        "ORDER BY datetime(created_at),item_key"
    ).fetchall():
        if str(row["state"] or "") not in UNRESOLVED_STATES or row["answer"]:
            continue
        if source_ids.intersection(_json_list(row["source_activity_ids"])):
            return {
                "item_key": str(row["item_key"] or ""),
                "episode_id": str(row["episode_id"] or ""),
            }
    return {}


def _episode_subject(
    event_type: str,
    event_label: str,
    evidence: str,
    full_text: str,
) -> dict[str, Any]:
    subject = _subject_for_event(event_type, event_label, evidence, full_text)
    if _subject_is_identified(subject):
        return subject
    entity = _extract_business_entity(full_text)
    if not entity:
        return subject
    if event_type == "interview":
        subject.update(
            {
                "company": entity,
                "object": entity,
                "label": f"{entity}面试（轮次未识别）",
                "company_identified": True,
                "object_identified": True,
            }
        )
    else:
        subject.update(
            {
                "object": entity,
                "label": f"{entity}{event_label}",
                "object_identified": True,
            }
        )
    return subject


def _extract_business_entity(value: str) -> str:
    text = safe_decision_text(value, max_len=1200)
    patterns = (
        r"([\u4e00-\u9fff]{2,10})(?=[\s·/—_-]*(?:MIXPOS|POS|海外|国际|全球|本地化|BD|销售|渠道|面试|会议))",
        r"(?:公司|客户|项目|品牌)\s*[:：]\s*([A-Za-z][A-Za-z0-9&.+-]{1,30}|[\u4e00-\u9fff·]{2,14})",
        r"\b([A-Z][A-Z0-9&.+-]{2,20})\b(?=.{0,30}(?:BD|GTM|POS|interview|meeting))",
    )
    blocked = {
        "海外", "国际", "全球", "本地化", "销售", "渠道", "商务", "客户", "项目", "公司",
        "meeting", "interview", "assistant", "session", "business", "sales", "channel",
    }
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        entity = safe_decision_text(match.group(1), max_len=40).strip(" ·/-_—")
        if entity and entity.casefold() not in {item.casefold() for item in blocked}:
            return entity
    return ""


def _suggested_workstream(subject: dict[str, Any], value: str) -> str:
    label = safe_decision_text(
        subject.get("company") or subject.get("object") or subject.get("label") or "",
        max_len=80,
    )
    role_match = re.search(
        r"((?:海外|国际|全球|本地化|区域|渠道|商务|销售|市场|产品)[^\r\n。！？!?；;]{0,24}(?:BD|GTM|POS|销售|渠道|商务|市场|产品)?)",
        value or "",
        re.I,
    )
    role = safe_decision_text(role_match.group(1), max_len=80) if role_match else ""
    if label and role and _normalized_phrase(role) not in _normalized_phrase(label):
        return safe_decision_text(f"{label} · {role}", max_len=120)
    return label or role


def backfill_unresolved_confirmation_groups(conn: sqlite3.Connection) -> None:
    """Upgrade only unresolved v25 rows to the v26 grouping metadata."""
    rows = conn.execute(
        "SELECT * FROM activity_confirmation_items "
        "WHERE state IN ('pending','deferred') ORDER BY datetime(observed_start),item_key"
    ).fetchall()
    for raw_row in rows:
        row = dict(raw_row)
        if _is_personal_ai_monitor_app(str(row.get("app_family") or "")):
            reason_codes = _unique(
                [*_json_list(row.get("reason_codes")), "schema26_self_app_filtered"]
            )
            conn.execute(
                "UPDATE activity_confirmation_items "
                "SET state='filtered',reason_codes=?,updated_at=? WHERE item_key=?",
                (json.dumps(reason_codes, ensure_ascii=False), _utc_now(), row["item_key"]),
            )
            continue
        event_type = str(row.get("event_type") or "")
        event_label = _event_label(event_type)
        cleaned_content = safe_decision_text(
            row.get("cleaned_content") or row.get("safe_observation") or "",
            max_len=1600,
        )
        stored_evidence = safe_decision_text(row.get("evidence") or "", max_len=280)
        match = _event_match(
            stored_evidence
            or [cleaned_content, str(row.get("safe_observation") or "")]
        )
        if event_type == "interview" and (not match or match[0] != "interview"):
            reason_codes = _unique(
                [*_json_list(row.get("reason_codes")), "schema26_non_occurrence_filtered"]
            )
            conn.execute(
                "UPDATE activity_confirmation_items "
                "SET state='filtered',reason_codes=?,updated_at=? WHERE item_key=?",
                (json.dumps(reason_codes, ensure_ascii=False), _utc_now(), row["item_key"]),
            )
            continue
        evidence = stored_evidence or safe_decision_text(
            match[2] if match else row.get("safe_observation") or "",
            max_len=280,
        )
        subject = _subject_for_event(event_type, event_label, evidence, cleaned_content)
        group_signature = _semantic_group_signature(
            event_type=event_type,
            app_family=str(row.get("app_family") or "unknown"),
            subject=subject,
            evidence=evidence,
            observed_start=str(row.get("observed_start") or ""),
        )
        group_key = str(row.get("group_key") or "") or (
            "grp_" + _hash(f"{group_signature}|{row['item_key']}")[:24]
        )
        conn.execute(
            """
            UPDATE activity_confirmation_items
            SET group_key=?,group_signature=?,subject=?,evidence=?,cleaned_content=?,
                decision_question=?,why_uncertain=?,updated_at=?
            WHERE item_key=?
            """,
            (
                group_key,
                group_signature,
                json.dumps(subject, ensure_ascii=False, sort_keys=True),
                evidence,
                cleaned_content,
                _decision_question(subject, str(row.get("observed_start") or "")),
                "这项判断只来自本机已清洗文字，需要你确认现实中是否真的发生。",
                _utc_now(),
                row["item_key"],
            ),
        )
    _regroup_confirmation_items(conn, unresolved_only=True)
    orphan_keys = [
        str(row["item_key"])
        for row in conn.execute(
            "SELECT * FROM activity_confirmation_items WHERE state IN ('pending','deferred')"
        ).fetchall()
        if not _confirmation_source_is_live(conn, row)
    ]
    if orphan_keys:
        schedule_evidence_release(conn, orphan_keys, reason="source_missing")
        placeholders = ",".join("?" for _ in orphan_keys)
        conn.execute(
            f"DELETE FROM activity_confirmation_items WHERE item_key IN ({placeholders})",
            tuple(orphan_keys),
        )


def _confirmation_policy_row_is_current(reason_codes: Any) -> bool:
    codes = set(_json_list(reason_codes))
    return {
        CONFIRMATION_POLICY_EVALUATED_REASON,
        CONFIRMATION_POLICY_QUESTION_SAFETY_REASON,
    }.issubset(codes)


def _row_independent_artifact_signals(
    conn: sqlite3.Connection,
    row: dict[str, Any],
) -> list[str]:
    signals = _safe_tokens(row.get("safe_signal_types"))
    source_ids = _json_list(row.get("source_activity_ids"))
    if not source_ids:
        return signals
    placeholders = ",".join("?" for _ in source_ids)
    if conn.execute(
        "SELECT 1 FROM activity_provenance AS prov "
        "JOIN document_signals AS ds ON ds.id=prov.source_id "
        "WHERE prov.derived_type='extracted_activity' "
        "AND prov.source_type='document_signal' "
        f"AND prov.derived_id IN ({placeholders}) "
        "AND COALESCE(ds.semantic_match,1)=1 "
        "AND ds.subsequent_mtime_event='modified_near_capture' LIMIT 1",
        tuple(source_ids),
    ).fetchone():
        signals.append(INDEPENDENT_FILE_EVIDENCE_SIGNAL)
    return _unique(signals)


def revalidate_unresolved_confirmation_policy(
    conn: sqlite3.Connection,
    *,
    commit: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Reclassify unresolved cards under the current policy without user side effects.

    The migration is deliberately reversible: filtered rows remain stored as
    system-rejected records with ``answer=NULL``.  Answered rows and calibration
    tables are never selected or updated.
    """
    # The overwhelmingly common current-policy path stays read-only so opening
    # a second Core connection cannot contend with an unrelated writer. If a
    # legacy tail row is visible, the locked section below re-reads everything
    # before changing it; this preflight is never used as mutation input.
    preflight_marker = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (CONFIRMATION_POLICY_SETTING_KEY,),
    ).fetchone()
    preflight_is_current = bool(
        preflight_marker
        and str(
            preflight_marker["value"]
            if isinstance(preflight_marker, sqlite3.Row)
            else preflight_marker[0]
        )
        == str(CONFIRMATION_POLICY_VERSION)
    )
    if preflight_is_current:
        preflight_unevaluated = any(
            not _confirmation_policy_row_is_current(row["reason_codes"])
            for row in conn.execute(
                "SELECT reason_codes FROM activity_confirmation_items "
                "WHERE state IN ('pending','deferred') AND answer IS NULL"
            ).fetchall()
        )
        if not preflight_unevaluated:
            return {
                "status": "current",
                **_safe_action_flags(),
                "policy_version": CONFIRMATION_POLICY_VERSION,
                "examined": 0,
                "rejected": 0,
                "retained_pending": 0,
                "retained_deferred": 0,
                "reason_counts": {},
                "groups_before": 0,
                "groups_after": 0,
                "groups_merged": 0,
                "groups_split": 0,
                "rows_regrouped": 0,
                "sources_regrouped": 0,
            }

    started_transaction = not conn.in_transaction
    savepoint = "confirmation_policy_revalidate"
    try:
        if started_transaction:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute(f"SAVEPOINT {savepoint}")
            # A savepoint inside a previously deferred transaction does not by
            # itself reserve the writer slot. This semantic no-op acquires the
            # SQLite write lock before any decision row is read.
            conn.execute(
                "UPDATE settings SET value=value WHERE key=?",
                (CONFIRMATION_POLICY_SETTING_KEY,),
            )
        marker = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (CONFIRMATION_POLICY_SETTING_KEY,),
        ).fetchone()
        marker_is_current = bool(
            marker
            and str(marker["value"] if isinstance(marker, sqlite3.Row) else marker[0])
            == str(CONFIRMATION_POLICY_VERSION)
        )
        unresolved_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM activity_confirmation_items "
                "WHERE state IN ('pending','deferred') AND answer IS NULL "
                "ORDER BY datetime(observed_start),item_key"
            ).fetchall()
        ]
        unevaluated_rows = [
            row
            for row in unresolved_rows
            if not _confirmation_policy_row_is_current(row.get("reason_codes"))
        ]
        if marker_is_current and not unevaluated_rows:
            if started_transaction:
                conn.rollback()
            else:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return {
                "status": "current",
                **_safe_action_flags(),
                "policy_version": CONFIRMATION_POLICY_VERSION,
                "examined": 0,
                "rejected": 0,
                "retained_pending": 0,
                "retained_deferred": 0,
                "reason_counts": {},
                "groups_before": 0,
                "groups_after": 0,
                "groups_merged": 0,
                "groups_split": 0,
                "rows_regrouped": 0,
                "sources_regrouped": 0,
            }
        rows = unevaluated_rows if marker_is_current else unresolved_rows
        rejected = 0
        retained_pending = 0
        retained_deferred = 0
        reason_counts: dict[str, int] = {}
        evidence_scheduled = 0
        regroup_required = False
        now = _utc_now()
        for row in rows:
            evaluated_reason_codes = _unique(
                [
                    *_json_list(row.get("reason_codes")),
                    CONFIRMATION_POLICY_EVALUATED_REASON,
                    CONFIRMATION_POLICY_QUESTION_SAFETY_REASON,
                ]
            )
            event_type = str(row.get("event_type") or "")
            cleaned_content = safe_decision_text(
                row.get("cleaned_content") or row.get("safe_observation") or "",
                max_len=1600,
            )
            stored_evidence = safe_decision_text(row.get("evidence") or "", max_len=280)
            stored_subject = _json_object(row.get("subject"))
            safe_signal_types = _row_independent_artifact_signals(conn, row)
            if (
                marker_is_current
                and f"{CONFIRMATION_POLICY_REASON_PREFIX}_question_safety_r2"
                in _json_list(row.get("reason_codes"))
            ):
                # r2 already normalized these subjects and groups. This
                # admission-only upgrade must not reparse old conversation
                # text, split unrelated groups, or hold the UI behind a full
                # historical regroup. Change only the new question gate.
                assessment = _confirmation_question_assessment(
                    event_type=event_type, subject=stored_subject,
                    evidence=stored_evidence, full_text=cleaned_content,
                    app_family=str(row.get("app_family") or "unknown"),
                    reason_codes=evaluated_reason_codes, safe_signal_types=safe_signal_types,
                    question_kind=str(row.get("question_kind") or "occurrence"),
                )
                low_value = assessment["reason"] in LOW_VALUE_QUESTION_REASONS
                if low_value:
                    evaluated_reason_codes.append(
                        f"{CONFIRMATION_POLICY_REASON_PREFIX}_information_insufficient_"
                        f"{assessment['reason']}"
                    )
                conn.execute(
                    "UPDATE activity_confirmation_items SET reason_codes=?,"
                    "decision_question=?,notification_eligible=?,priority=?,updated_at=? "
                    "WHERE item_key=? AND state IN ('pending','deferred') AND answer IS NULL",
                    (
                        json.dumps(_unique(evaluated_reason_codes), ensure_ascii=False),
                        "" if low_value else str(row.get("decision_question") or ""),
                        0 if low_value else int(bool(row.get("notification_eligible"))),
                        "low" if low_value else str(row.get("priority") or "normal"),
                        now, row["item_key"],
                    ),
                )
                retained_deferred += int(str(row.get("state") or "") == "deferred")
                retained_pending += int(str(row.get("state") or "") == "pending")
                continue
            regroup_required = True
            independent_artifact_evidence = _has_independent_artifact_evidence(
                {str(code).casefold() for code in evaluated_reason_codes},
                {str(token).casefold() for token in safe_signal_types},
            )
            match = _event_match(
                [stored_evidence, cleaned_content, str(row.get("safe_observation") or "")]
            )
            filter_reasons: list[str] = []
            if not _confirmation_source_is_live(conn, row):
                filter_reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_source_missing")
            if not match or match[0] != event_type:
                evidence = stored_evidence
                parsed_subject = _subject_for_event(
                    event_type,
                    _event_label(event_type),
                    evidence,
                    cleaned_content,
                )
                subject = _prefer_revalidated_subject(
                    event_type,
                    stored_subject,
                    parsed_subject,
                    stored_evidence=stored_evidence,
                    parsed_evidence=evidence,
                )
                # Older business episodes and low-confidence OCR rows may no
                # longer contain the exact trigger fragment that created them.
                # Missing trigger text is uncertainty, not proof that a row is
                # false.  Re-run the content policy so self UI/meta/tool-status
                # loops are still filtered, while legitimate incomplete rows
                # remain available to the V5 information-insufficient section.
                filter_reasons.extend(
                    _confirmation_policy_filter_reasons(
                        event_type=event_type,
                        app_family=str(row.get("app_family") or "unknown"),
                        region_kind=str(row.get("region_kind") or "unknown"),
                        actionability=str(row.get("actionability") or "unknown"),
                        evidence=evidence,
                        full_text=cleaned_content,
                        subject=subject,
                        require_subject=False,
                        independent_execution_evidence=independent_artifact_evidence,
                    )
                )
                evaluated_reason_codes = _unique(
                    [
                        *evaluated_reason_codes,
                        f"{CONFIRMATION_POLICY_REASON_PREFIX}_trigger_unverified",
                    ]
                )
            else:
                evidence = safe_decision_text(match[2], max_len=280)
                parsed_subject = _subject_for_event(
                    event_type,
                    _event_label(event_type),
                    evidence,
                    cleaned_content,
                )
                subject = _prefer_revalidated_subject(
                    event_type,
                    stored_subject,
                    parsed_subject,
                    stored_evidence=stored_evidence,
                    parsed_evidence=evidence,
                )
                filter_reasons.extend(
                    _confirmation_policy_filter_reasons(
                        event_type=event_type,
                        app_family=str(row.get("app_family") or "unknown"),
                        region_kind=str(row.get("region_kind") or "unknown"),
                        actionability=str(row.get("actionability") or "unknown"),
                        evidence=evidence,
                        full_text=cleaned_content,
                        subject=subject,
                        independent_execution_evidence=independent_artifact_evidence,
                    )
                )
            if _is_personal_ai_monitor_app(str(row.get("app_family") or "")):
                filter_reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_self_ui")
            assessment = _confirmation_question_assessment(
                event_type=event_type,
                subject=subject,
                evidence=evidence,
                full_text=cleaned_content,
                app_family=str(row.get("app_family") or "unknown"),
                reason_codes=evaluated_reason_codes,
                safe_signal_types=safe_signal_types,
                question_kind=str(row.get("question_kind") or "occurrence"),
            )
            if assessment["status"] == "filtered":
                filter_reasons.append(
                    f"{CONFIRMATION_POLICY_REASON_PREFIX}_{assessment['reason']}"
                )
            filter_reasons = _unique(filter_reasons)
            if filter_reasons:
                reason_codes = _unique([*evaluated_reason_codes, *filter_reasons])
                update = conn.execute(
                    "UPDATE activity_confirmation_items "
                    "SET state='filtered',answer=NULL,answer_reason=NULL,answered_at=NULL,"
                    "clarification_note='',"
                    "reason_codes=?,updated_at=? WHERE item_key=? "
                    "AND state IN ('pending','deferred') AND answer IS NULL",
                    (json.dumps(reason_codes, ensure_ascii=False), now, row["item_key"]),
                )
                if update.rowcount != 1:
                    continue
                # Policy-v7 queue cleanup is a reversible presentation
                # reclassification.  Keep retained evidence and provenance in
                # place; no user answer is invented and no evidence is released.
                rejected += 1
                for reason in filter_reasons:
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
                continue

            subject = dict(assessment["subject"])
            safe_observation = safe_decision_text(
                row.get("safe_observation") or "",
                max_len=420,
            )
            if assessment["status"] == "information_insufficient":
                evaluated_reason_codes = _unique(
                    [
                        *evaluated_reason_codes,
                        f"{CONFIRMATION_POLICY_REASON_PREFIX}_information_insufficient_"
                        f"{assessment['reason']}",
                    ]
                )
                decision_question = ""
                notification_eligible = 0
            else:
                decision_question = _decision_question(
                    subject,
                    str(row.get("observed_start") or ""),
                )
                notification_eligible = int(
                    bool(row.get("notification_eligible"))
                )
            if bool(assessment.get("privacy_safe")):
                safe_observation = (
                    "招聘沟通界面中检测到送达或消息状态；"
                    "联系人和消息正文不进入确认卡。"
                )
                evidence = "招聘沟通界面送达状态"
                cleaned_content = safe_observation
            signature = _semantic_group_signature(
                event_type=event_type,
                app_family=str(row.get("app_family") or "unknown"),
                subject=subject,
                evidence=evidence,
                observed_start=str(row.get("observed_start") or ""),
            )
            priority = (
                "low"
                if assessment["reason"] in LOW_VALUE_QUESTION_REASONS
                else str(row.get("priority") or "normal")
            )
            update = conn.execute(
                "UPDATE activity_confirmation_items "
                "SET subject=?,safe_observation=?,evidence=?,cleaned_content=?,"
                "decision_question=?,group_signature=?,reason_codes=?,safe_signal_types=?,"
                "notification_eligible=?,priority=?,updated_at=? "
                "WHERE item_key=? AND state IN ('pending','deferred') AND answer IS NULL",
                (
                    json.dumps(subject, ensure_ascii=False, sort_keys=True),
                    safe_observation,
                    evidence,
                    cleaned_content,
                    decision_question,
                    signature,
                    json.dumps(evaluated_reason_codes, ensure_ascii=False),
                    json.dumps(safe_signal_types, ensure_ascii=False),
                    notification_eligible,
                    priority,
                    now,
                    row["item_key"],
                ),
            )
            if update.rowcount != 1:
                continue
            if str(row.get("state") or "") == "deferred":
                retained_deferred += 1
            else:
                retained_pending += 1

        if regroup_required:
            _regroup_confirmation_items(conn, unresolved_only=True)
        regroup_metrics = _confirmation_revalidation_regroup_metrics(conn, rows)
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (CONFIRMATION_POLICY_SETTING_KEY, str(CONFIRMATION_POLICY_VERSION), now),
        )
        if dry_run:
            if started_transaction:
                conn.rollback()
            else:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        elif started_transaction:
            if commit:
                conn.commit()
                _purge_evidence_after_commit(conn)
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return {
            "status": "preview" if dry_run else "applied",
            **_safe_action_flags(),
            "dry_run": dry_run,
            "policy_version": CONFIRMATION_POLICY_VERSION,
            "examined": len(rows),
            "rejected": rejected,
            "retained_pending": retained_pending,
            "retained_deferred": retained_deferred,
            "reason_counts": dict(sorted(reason_counts.items())),
            "confirmation_evidence_scheduled": evidence_scheduled,
            **regroup_metrics,
        }
    except Exception:
        if started_transaction:
            if conn.in_transaction:
                conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def revalidate_unresolved_confirmation_policy_v3(
    conn: sqlite3.Connection,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    """Compatibility entry point retained for one release."""
    return revalidate_unresolved_confirmation_policy(conn, commit=commit)


def _confirmation_revalidation_regroup_metrics(
    conn: sqlite3.Connection,
    original_rows: list[dict[str, Any]],
) -> dict[str, int]:
    if not original_rows:
        return {
            "groups_before": 0,
            "groups_after": 0,
            "groups_merged": 0,
            "groups_split": 0,
            "rows_regrouped": 0,
            "sources_regrouped": 0,
        }
    original_by_key = {
        str(row.get("item_key") or ""): row
        for row in original_rows
        if str(row.get("item_key") or "")
    }
    if not original_by_key:
        return {
            "groups_before": 0,
            "groups_after": 0,
            "groups_merged": 0,
            "groups_split": 0,
            "rows_regrouped": 0,
            "sources_regrouped": 0,
        }
    placeholders = ",".join("?" for _ in original_by_key)
    current_rows = [
        dict(row)
        for row in conn.execute(
            f"SELECT item_key,group_key,state,source_activity_ids FROM "
            f"activity_confirmation_items WHERE item_key IN ({placeholders})",
            tuple(original_by_key),
        ).fetchall()
    ]
    retained = [
        row
        for row in current_rows
        if str(row.get("state") or "") in UNRESOLVED_STATES
    ]
    before_groups = {
        str(original_by_key[str(row["item_key"])].get("group_key") or "")
        or f"legacy_{row['item_key']}"
        for row in retained
    }
    after_groups = {
        str(row.get("group_key") or "") or f"legacy_{row['item_key']}"
        for row in retained
    }
    changed = [
        row
        for row in retained
        if (
            str(original_by_key[str(row["item_key"])].get("group_key") or "")
            != str(row.get("group_key") or "")
        )
    ]
    changed_source_ids = {
        source_id
        for row in changed
        for source_id in _json_list(row.get("source_activity_ids"))
        if source_id
    }
    source_less_changed = sum(
        not _json_list(row.get("source_activity_ids")) for row in changed
    )
    return {
        "groups_before": len(before_groups),
        "groups_after": len(after_groups),
        "groups_merged": max(0, len(before_groups) - len(after_groups)),
        "groups_split": max(0, len(after_groups) - len(before_groups)),
        "rows_regrouped": len(changed),
        "sources_regrouped": len(changed_source_ids) + int(source_less_changed),
    }


def _regroup_confirmation_items(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
    unresolved_only: bool = True,
) -> None:
    where: list[str] = []
    params: list[Any] = []
    if start and end:
        where.extend(["datetime(observed_start)<datetime(?)", "datetime(observed_end)>datetime(?)"])
        params.extend([end, start])
    if unresolved_only:
        where.extend(
            [
                "state IN ('pending','deferred')",
                "answer IS NULL",
            ]
        )
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM activity_confirmation_items {clause} "
            "ORDER BY datetime(observed_start),datetime(created_at),item_key",
            params,
        ).fetchall()
    ]
    clusters: list[list[dict[str, Any]]] = []
    for row in rows:
        placed = False
        for cluster in reversed(clusters):
            if _cluster_can_accept_confirmation_row(cluster, row):
                cluster.append(row)
                placed = True
                break
        if not placed:
            clusters.append([row])
    now = _utc_now()
    assigned_group_keys: set[str] = set()
    for cluster in clusters:
        group_key = _stable_cluster_group_key(cluster)
        if not group_key:
            group_key = "grp_" + _hash(str(cluster[0].get("item_key") or ""))[:24]
        assessment = _confirmation_group_question_assessment(cluster)
        canonical_subject = dict(assessment["subject"])
        canonical_note = _latest_cluster_clarification_note(cluster)
        encoded_subject = json.dumps(canonical_subject, ensure_ascii=False, sort_keys=True)
        signature = _cluster_signature(cluster, subject=canonical_subject)
        if group_key in assigned_group_keys:
            group_key = "grp_" + _hash(
                f"{signature}|{min(str(row.get('item_key') or '') for row in cluster)}"
            )[:24]
        assigned_group_keys.add(group_key)
        for row in cluster:
            decision_question = (
                _decision_question(
                    canonical_subject,
                    str(row.get("observed_start") or ""),
                )
                if assessment["status"] == "answerable"
                else ""
            )
            if (
                row.get("group_key") == group_key
                and row.get("group_signature") == signature
                and _json_object(row.get("subject")) == canonical_subject
                and str(row.get("decision_question") or "") == decision_question
                and safe_decision_text(
                    row.get("clarification_note") or "",
                    max_len=360,
                )
                == canonical_note
            ):
                continue
            conn.execute(
                "UPDATE activity_confirmation_items SET group_key=?,group_signature=?,"
                "subject=?,decision_question=?,clarification_note=?,updated_at=? "
                "WHERE item_key=?",
                (
                    group_key,
                    signature,
                    encoded_subject,
                    decision_question,
                    canonical_note,
                    now,
                    row["item_key"],
                ),
            )


def _stable_cluster_group_key(cluster: list[dict[str, Any]]) -> str:
    """Keep the durable key of an already displayed group while regrouping."""
    members_by_key: dict[str, list[dict[str, Any]]] = {}
    for row in cluster:
        group_key = str(row.get("group_key") or "")
        if group_key:
            members_by_key.setdefault(group_key, []).append(row)
    if not members_by_key:
        return ""

    # Newly materialized candidates can share the same second-level created_at
    # as rows that were already displayed.  item_key is a hash, so using it as
    # the tie-breaker can let the new candidate steal the visible group's key.
    # Prefer the key that was persisted first.  A later backfill can carry an
    # earlier observed_start, but must not steal a key the UI has already
    # displayed.  observed_start resolves the second-level created_at ties
    # which originally exposed the hashed-item-key bug; membership count and
    # group_key provide the remaining content-free deterministic tie-breakers.
    return min(
        members_by_key,
        key=lambda group_key: (
            min(
                str(row.get("created_at") or "")
                for row in members_by_key[group_key]
            ),
            min(
                str(row.get("observed_start") or "")
                for row in members_by_key[group_key]
            ),
            -len(members_by_key[group_key]),
            group_key,
        ),
    )


def _cluster_can_accept_confirmation_row(
    cluster: list[dict[str, Any]],
    row: dict[str, Any],
) -> bool:
    if not cluster:
        return False
    event_type = str(row.get("event_type") or "")
    row_identity = _subject_identity(
        _json_object(row.get("subject")),
        event_type,
        str(row.get("evidence") or ""),
    )
    if not any(row_identity):
        return False
    identities: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for member in cluster:
        identity = _subject_identity(
            _json_object(member.get("subject")),
            event_type,
            str(member.get("evidence") or ""),
        )
        identities.setdefault(identity, []).append(member)
    if row_identity in identities:
        # Exact non-repeatable identities intentionally merge across the local
        # day.  Repeatable event types retain their existing occurrence window.
        return any(_rows_can_group(member, row) for member in identities[row_identity])
    # A fuzzy completion variant may join only when it is compatible with each
    # distinct identity already represented.  This prevents a chain of small
    # OCR truncations from bridging unrelated subjects over a long interval.
    return bool(identities) and all(
        any(_rows_can_group(member, row) for member in members)
        for members in identities.values()
    )


def _rows_can_group(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if str(left.get("event_type") or "") != str(right.get("event_type") or ""):
        return False
    event_type = str(left.get("event_type") or "")
    left_episode = str(left.get("episode_id") or "")
    right_episode = str(right.get("episode_id") or "")
    if left_episode and right_episode and left_episode != right_episode:
        return False
    same_episode = bool(left_episode and left_episode == right_episode)
    left_subject = _json_object(left.get("subject"))
    right_subject = _json_object(right.get("subject"))
    left_identity = _subject_identity(
        left_subject,
        event_type,
        str(left.get("evidence") or ""),
    )
    right_identity = _subject_identity(
        right_subject,
        event_type,
        str(right.get("evidence") or ""),
    )
    if not any(left_identity) or not any(right_identity):
        return False
    if event_type == "publish":
        if _local_day(str(left.get("observed_start") or "")) != _local_day(
            str(right.get("observed_start") or "")
        ):
            return False
        if left_identity != right_identity:
            return False
        return bool(
            same_episode
            or _within_publish_semantic_window(left, right)
        )
    if _local_day(str(left.get("observed_start") or "")) != _local_day(
        str(right.get("observed_start") or "")
    ):
        return False
    if (
        event_type in REPEATABLE_EVENT_TYPES
        and not same_episode
        and not _within_group_window(left, right)
    ):
        return False
    if left_identity == right_identity:
        return True
    if event_type != "completion":
        return False
    left_object = left_identity[0] if left_identity else ""
    right_object = right_identity[0] if right_identity else ""
    if min(len(left_object), len(right_object)) < 8:
        return False
    if left_object not in right_object and right_object not in left_object:
        return False
    left_app = _normalized_app_family(str(left.get("app_family") or "unknown"))
    right_app = _normalized_app_family(str(right.get("app_family") or "unknown"))
    if not (_unknown_app(left_app) or _unknown_app(right_app) or left_app == right_app):
        return False
    return _within_completion_ocr_window(left, right)


def _canonical_cluster_subject(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    event_type = str(cluster[0].get("event_type") or "") if cluster else ""
    selected_row = max(
        (
            row
            for row in cluster
        ),
        key=lambda row: (
            sum(
                len(part)
                for part in _subject_identity(
                    dict(row.get("_card_subject") or _json_object(row.get("subject"))),
                    event_type,
                    str(row.get("evidence") or ""),
                )
            ),
            sum(
                bool(part)
                for part in _subject_identity(
                    dict(row.get("_card_subject") or _json_object(row.get("subject"))),
                    event_type,
                    str(row.get("evidence") or ""),
                )
            ),
            len(
                str(
                    dict(row.get("_card_subject") or _json_object(row.get("subject"))).get(
                        "object"
                    )
                    or ""
                )
            ),
            len(
                str(
                    dict(row.get("_card_subject") or _json_object(row.get("subject"))).get(
                        "label"
                    )
                    or ""
                )
            ),
        ),
        default={},
    )
    selected = (
        dict(selected_row.get("_card_subject") or _json_object(selected_row.get("subject")))
        if selected_row
        else _subject_for_event(event_type, _event_label(event_type), "", "")
    )
    evidence = " ".join(
        _unique(
            [safe_decision_text(row.get("evidence") or "", max_len=280) for row in cluster]
        )
    )
    if event_type != "interview":
        selected = _organized_confirmation_subject(
            event_type,
            selected,
            evidence=evidence,
        )
    return selected


def _publish_subjects_compatible(
    left_subject: dict[str, Any],
    right_subject: dict[str, Any],
    *,
    left_evidence: str = "",
    right_evidence: str = "",
) -> bool:
    left = _publish_semantic_identity(left_subject, left_evidence)
    right = _publish_semantic_identity(right_subject, right_evidence)
    return _publish_identities_compatible(left, right)


def _publish_identities_compatible(left: str, right: str) -> bool:
    if not left or not right:
        return False
    # A shared product/environment is not enough to prove the same publish
    # target.  For example, a Solo Staging form and a Solo Staging document are
    # separate events and one answer must never settle both.  OCR variants are
    # merged only after they resolve to the same normalized canonical object.
    return left == right


def _publish_semantic_identity(subject: dict[str, Any], evidence: str = "") -> str:
    # Include the full canonical publish target, not only its product or
    # environment, so answer-group cannot cross distinct objects.
    return _normalized_phrase(_canonical_event_object("publish", subject, evidence))


def _within_publish_semantic_window(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    try:
        left_start = _local_datetime(str(left.get("observed_start") or ""))
        right_start = _local_datetime(str(right.get("observed_start") or ""))
    except ValueError:
        return False
    return abs(right_start - left_start) <= timedelta(minutes=15)


def _latest_cluster_clarification_note(cluster: list[dict[str, Any]]) -> str:
    candidates = [
        (
            str(row.get("updated_at") or ""),
            str(row.get("item_key") or ""),
            safe_decision_text(row.get("clarification_note") or "", max_len=360),
        )
        for row in cluster
        if safe_decision_text(row.get("clarification_note") or "", max_len=360)
    ]
    if not candidates:
        return ""
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _cluster_signature(
    cluster: list[dict[str, Any]],
    *,
    subject: dict[str, Any] | None = None,
) -> str:
    first = cluster[0]
    subject = subject or _json_object(first.get("subject"))
    event_type = str(first.get("event_type") or "")
    raw = "|".join(
        (
            _local_day(str(first.get("observed_start") or "")),
            event_type,
            _group_time_bucket(event_type, str(first.get("observed_start") or "")),
            *(
                _subject_identity(
                    subject,
                    event_type,
                    str(first.get("evidence") or ""),
                )
                or ("unknown",)
            ),
        )
    )
    return "sig_" + _hash(raw)[:24]


def list_activity_confirmations(
    conn: sqlite3.Connection,
    *,
    range_name: str = "today",
    start: str | None = None,
    end: str | None = None,
    limit: int = 80,
    include_resolved: bool = False,
) -> dict[str, Any]:
    # Hold one authorization lease through response assembly so a completed
    # delete cannot be followed by a stale card. Grouping and orphan maintenance
    # belong to writer transactions; this public refresh path is strictly read-only.
    with document_authorization_lock(conn, exclusive=False):
        return _list_activity_confirmations_locked(
            conn,
            range_name=range_name,
            start=start,
            end=end,
            limit=limit,
            include_resolved=include_resolved,
        )


def _list_activity_confirmations_locked(
    conn: sqlite3.Connection,
    *,
    range_name: str = "today",
    start: str | None = None,
    end: str | None = None,
    limit: int = 80,
    include_resolved: bool = False,
) -> dict[str, Any]:
    resolved_start, resolved_end = resolve_confirmation_range(range_name, start=start, end=end)
    candidate_rows = conn.execute(
        "SELECT * FROM activity_confirmation_items "
        "WHERE datetime(observed_start)<datetime(?) AND datetime(observed_end)>datetime(?) "
        "ORDER BY datetime(observed_start) DESC,item_key",
        (resolved_end, resolved_start),
    ).fetchall()
    all_rows = [row for row in candidate_rows if _confirmation_source_is_live(conn, row)]
    rows = [
        row
        for row in all_rows
        if include_resolved or str(row["state"] or "") in UNRESOLVED_STATES
    ][: max(1, int(limit))]
    all_groups = _question_groups(conn, all_rows)
    visible_groups = [
        group
        for group in all_groups
        if include_resolved or group["state"] in UNRESOLVED_STATES
    ][: max(1, int(limit))]
    recent_candidates = conn.execute(
        "SELECT * FROM activity_confirmation_items WHERE answer IS NOT NULL AND answer<>'ignored' "
        "ORDER BY datetime(answered_at) DESC LIMIT 100"
    ).fetchall()
    recent = [
        row for row in recent_candidates if _confirmation_source_is_live(conn, row)
    ][:10]
    calibrations = conn.execute(
        "SELECT * FROM activity_confirmation_calibrations ORDER BY datetime(updated_at) DESC LIMIT 50"
    ).fetchall()
    pending_count = sum(str(row["state"] or "") in UNRESOLVED_STATES for row in all_rows)
    resolved_count = len(all_rows) - pending_count
    pending_groups = [group for group in all_groups if group["state"] in UNRESOLVED_STATES]
    resolved_groups = [group for group in all_groups if group["state"] not in UNRESOLVED_STATES]
    pending_source_count = sum(int(group.get("source_count") or 0) for group in pending_groups)
    diagnostics = _confirmation_diagnostics_locked(
        conn,
        resolved_start,
        resolved_end,
        rows=all_rows,
        groups=all_groups,
    )
    response = {
        "status": "ok",
        "interface": INTERFACE,
        "contract_version": CONTRACT_VERSION,
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "range": range_name,
        "time_range": {"from": resolved_start, "to": resolved_end},
        "pending_count": pending_count,
        "resolved_count": resolved_count,
        "pending_question_count": len(pending_groups),
        "pending_source_count": pending_source_count,
        "mode": get_activity_confirmation_mode(conn),
        "priority_counts": diagnostics["priority_counts"],
        "pending_by_kind": diagnostics["pending_by_kind"],
        "candidate_funnel": diagnostics["candidate_funnel"],
        "confirmation_revision": diagnostics["revision"],
        "resolved_question_count": len(resolved_groups),
        "question_groups": visible_groups,
        "items": [_public_item(row, conn=conn) for row in rows],
        "recent_answers": [_public_item(row, conn=conn) for row in recent],
        "calibrations": [_public_calibration(conn, row) for row in calibrations],
        "summary": (
            f"有 {len(pending_groups)} 个问题等待确认（合并 {pending_source_count} 条线索）。"
            if pending_groups
            else "当前没有待确认的活动事实。"
        ),
    }
    # Contract V4 remains available for compatibility and diagnostics, but it
    # has the same privacy boundary as Cards V5.  In particular, legacy
    # ``subject`` dictionaries and ``cleaned_contents`` must not bypass the
    # scalar path firewall used by the newer presentation layer.
    return redact_absolute_paths_in_value(response)


def list_activity_confirmation_cards(
    conn: sqlite3.Connection,
    *,
    range_name: str = "today",
    start: str | None = None,
    end: str | None = None,
    limit: int = DEFAULT_CONFIRMATION_CARD_LIMIT,
    cursor: str | None = None,
    scan_limit: int | None = None,
    section: str | None = None,
) -> dict[str, Any]:
    return _activity_confirmation_cards(
        conn, range_name=range_name, start=start, end=end, limit=limit,
        cursor=cursor, scan_limit=scan_limit, section=section,
    )


def activity_confirmation_card_snapshot(conn, *, range_name="today", start=None, end=None, section=None,
                                        _eligible_group_observer=None):
    """Internal complete-range read: scan and validate once, without pagination.

    The public paginated interface retains its page and scan limits. This
    read-only projection keeps every unresolved group, including old questions.
    """
    return _activity_confirmation_cards(
        conn, range_name=range_name, start=start, end=end, section=section,
        all_pages=True, _eligible_group_observer=_eligible_group_observer,
    )


def _activity_confirmation_cards(
    conn, *, range_name="today", start=None, end=None,
    limit=DEFAULT_CONFIRMATION_CARD_LIMIT, cursor=None, scan_limit=None,
    section=None, all_pages=False, _eligible_group_observer=None,
):
    """Return the unresolved card surface without legacy diagnostics.

    This is intentionally a separate contract from ``list``.  It performs two
    bounded database reads, checks source liveness in batches, and formats only
    the requested page.  ``scan_limit`` is an opt-in bound for latency-sensitive
    callers; the legacy card contract keeps its complete-range behaviour when
    it is omitted.  ``section`` restricts pagination to one card section while
    every count field keeps its full-range meaning, so a filtered page can
    never masquerade as the whole queue.  It never builds the confirmed view,
    funnel diagnostics, recent answers, calibrations, or cleaned-content
    compatibility payloads.
    """
    if section not in (None, "pending_confirmation", "information_insufficient"):
        raise ValueError("unsupported confirmation card section")
    with decision_text_read_cache(reuse_existing=True), document_authorization_lock(
        conn,
        exclusive=False,
        timeout=INTERACTIVE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS,
    ):
        resolved_start, resolved_end = resolve_confirmation_range(
            range_name,
            start=start,
            end=end,
        )
        row_query = (
            "SELECT * FROM activity_confirmation_items "
            "WHERE state IN ('pending','deferred') AND answer IS NULL "
            "AND datetime(observed_start)<datetime(?) "
            "AND datetime(observed_end)>datetime(?) "
            "ORDER BY CASE state WHEN 'pending' THEN 0 ELSE 1 END,"
            "datetime(observed_start) DESC,item_key"
        )
        row_params: list[Any] = [resolved_end, resolved_start]
        bounded_scan_limit: int | None = None
        if scan_limit is not None:
            bounded_scan_limit = max(1, min(int(scan_limit), 5_000))
            # Read one sentinel row so callers can distinguish a complete scan
            # from a deliberately bounded candidate window without scanning
            # the remainder of a large legacy queue.
            row_query += " LIMIT ?"
            row_params.append(bounded_scan_limit + 1)
        queried_rows = [
            dict(row)
            for row in conn.execute(
                row_query,
                tuple(row_params),
            ).fetchall()
        ]
        scan_truncated = bool(
            bounded_scan_limit is not None
            and len(queried_rows) > bounded_scan_limit
        )
        rows = (
            queried_rows[:bounded_scan_limit]
            if bounded_scan_limit is not None
            else queried_rows
        )
        live_rows = _live_confirmation_rows(conn, rows)
        grouped = _visible_confirmation_card_groups(
            _group_confirmation_rows(live_rows)
        )
        visible_rows = [
            row
            for members in grouped.values()
            for row in members
        ]
        revision = _confirmation_card_revision(visible_rows)
        group_sections = {
            group_key: _confirmation_group_section(members)
            for group_key, members in grouped.items()
        }
        ordered_groups = sorted(
            grouped.items(),
            key=lambda entry: (
                0 if group_sections[entry[0]] == "pending_confirmation" else 1,
                0 if any(str(row.get("state") or "") == "pending" for row in entry[1]) else 1,
                -_confirmation_group_priority_rank(entry[1]),
                -max(float(row.get("_card_start_timestamp") or 0.0) for row in entry[1]),
                entry[0],
            ),
        )
        paged_groups = (
            [
                entry
                for entry in ordered_groups
                if group_sections[entry[0]] == section
            ]
            if section
            else ordered_groups
        )
        offset, cursor_reset = _decode_confirmation_card_cursor(cursor, revision)
        page_size = len(paged_groups) if all_pages else min(MAX_CONFIRMATION_CARD_LIMIT, max(1, int(limit)))
        page = paged_groups[offset : offset + page_size]
        next_offset = offset + len(page)
        next_cursor = (
            _encode_confirmation_card_cursor(revision, next_offset)
            if next_offset < len(paged_groups)
            else ""
        )
        cards = [
            _public_confirmation_card_v5(group_key, members)
            for group_key, members in page
        ]
        pending_groups = [
            members
            for group_key, members in ordered_groups
            if group_sections[group_key] == "pending_confirmation"
        ]
        insufficient_groups = [
            members
            for group_key, members in ordered_groups
            if group_sections[group_key] == "information_insufficient"
        ]
        response = {
            "status": "ok",
            "interface": INTERFACE,
            "contract_version": CONFIRMATION_CARD_CONTRACT_VERSION,
            "raw_access": False,
            "destructive_actions": False,
            "memory_write_actions": False,
            "range": range_name,
            "time_range": {"from": resolved_start, "to": resolved_end},
            "revision": revision,
            "cursor": cursor or "",
            "cursor_reset": cursor_reset,
            "next_cursor": next_cursor,
            "has_more": bool(next_cursor),
            "page_size": page_size,
            "total_card_count": len(ordered_groups),
            "pending_confirmation_count": len(pending_groups),
            "information_insufficient_count": len(insufficient_groups),
            "task_status_count": 0,
            "pending_question_count": len(pending_groups),
            "pending_source_count": sum(
                len(_confirmation_group_source_ids(members)) or len(members)
                for members in pending_groups
            ),
            "total_source_count": sum(
                len(_confirmation_group_source_ids(members)) or len(members)
                for _, members in ordered_groups
            ),
            "cards": cards,
        }
        if bounded_scan_limit is not None:
            response["candidate_scan_limit"] = bounded_scan_limit
            response["candidate_scan_truncated"] = scan_truncated
        if _eligible_group_observer is not None and all_pages and not scan_truncated:
            # This private, synchronous read stays inside the same authorization
            # lease. Internal item keys never enter the public card payload.
            _eligible_group_observer({
                str(row["item_key"]): group_key
                for group_key, members in grouped.items()
                if group_sections[group_key] == "pending_confirmation"
                for row in members
            })
        return redact_absolute_paths_in_value(response)


def activity_confirmation_revision(
    conn: sqlite3.Connection,
    *,
    range_name: str = "today",
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Return a cheap content-free invalidation token for the card surface."""
    with document_authorization_lock(
        conn,
        exclusive=False,
        timeout=INTERACTIVE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS,
    ):
        resolved_start, resolved_end = resolve_confirmation_range(
            range_name,
            start=start,
            end=end,
        )
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT item_key,group_key,group_signature,event_type,subject,evidence,"
                "app_family,candidate_origin,episode_id,question_kind,priority,state,"
                "reason_codes,safe_signal_types,observed_start,"
                "observed_end,source_activity_ids,deletion_group_ids,created_at,updated_at "
                "FROM activity_confirmation_items "
                "WHERE state IN ('pending','deferred') AND answer IS NULL "
                "AND datetime(observed_start)<datetime(?) "
                "AND datetime(observed_end)>datetime(?)",
                (resolved_end, resolved_start),
            ).fetchall()
        ]
        live_rows = _live_confirmation_rows(conn, rows)
        groups = _visible_confirmation_card_groups(
            _group_confirmation_rows(live_rows)
        )
        visible_rows = [
            row
            for members in groups.values()
            for row in members
        ]
        group_sections = {
            group_key: _confirmation_group_section(members)
            for group_key, members in groups.items()
        }
        pending_groups = [
            members
            for group_key, members in groups.items()
            if group_sections[group_key] == "pending_confirmation"
        ]
        return {
            "status": "ok",
            "interface": INTERFACE,
            "contract_version": CONFIRMATION_CARD_CONTRACT_VERSION,
            "raw_access": False,
            "range": range_name,
            "time_range": {"from": resolved_start, "to": resolved_end},
            "revision": _confirmation_card_revision(visible_rows),
            "total_card_count": len(groups),
            "pending_confirmation_count": len(pending_groups),
            "information_insufficient_count": len(groups) - len(pending_groups),
            "task_status_count": 0,
            "pending_question_count": len(pending_groups),
            "pending_source_count": sum(
                len(_confirmation_group_source_ids(members)) or len(members)
                for members in pending_groups
            ),
            "total_source_count": sum(
                len(_confirmation_group_source_ids(members)) or len(members)
                for members in groups.values()
            ),
        }


def _group_confirmation_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    for row in rows:
        _prepare_confirmation_card_row(row)
    clusters: list[list[dict[str, Any]]] = []
    buckets: dict[tuple[str, str], list[list[dict[str, Any]]]] = {}
    for row in sorted(
        rows,
        key=lambda item: (
            str(item.get("observed_start") or ""),
            str(item.get("created_at") or ""),
            str(item.get("item_key") or ""),
        ),
    ):
        # Every compatibility branch requires the same event type and day.
        # Index only those candidates; preserve their original newest-first
        # order and every existing semantic compatibility check.
        bucket = buckets.setdefault((str(row.get("event_type") or ""), str(row.get("_card_day") or "")), [])
        for cluster in reversed(bucket):
            if _card_cluster_can_accept_confirmation_row(cluster, row):
                cluster.append(row)
                break
        else:
            cluster = [row]
            clusters.append(cluster)
            bucket.append(cluster)
    keyed_clusters: list[tuple[str, list[dict[str, Any]]]] = []
    for cluster in clusters:
        group_key = _stable_cluster_group_key(cluster) or (
            f"legacy_{cluster[0].get('item_key') or ''}"
        )
        keyed_clusters.append((group_key, cluster))

    # Old policy versions could persist one group key across rows which the
    # current semantic rules correctly split into different objects.  A plain
    # dictionary assignment would silently drop every cluster except the last;
    # retaining the durable key on just one cluster would be worse, because an
    # answer against that key would resolve all of the incompatible rows.  Keep
    # every cluster visible under a content-free synthetic display key and make
    # all colliding clusters read-only until the explicit V7 regroup persists
    # safe, distinct group keys.
    key_counts: dict[str, int] = {}
    for group_key, _ in keyed_clusters:
        key_counts[group_key] = key_counts.get(group_key, 0) + 1

    grouped: dict[str, list[dict[str, Any]]] = {}
    for durable_group_key, cluster in keyed_clusters:
        group_key = durable_group_key
        if key_counts.get(durable_group_key, 0) > 1:
            for row in cluster:
                row["_card_group_key_collision"] = True
            first_item_key = min(
                str(row.get("item_key") or "") for row in cluster
            )
            group_key = "collision_" + _hash(
                f"{durable_group_key}|{first_item_key}"
            )[:24]
        grouped[group_key] = cluster
    return grouped


def _visible_confirmation_card_groups(
    grouped: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Apply the final, read-only self-page firewall after semantic grouping."""
    return {
        group_key: members
        for group_key, members in grouped.items()
        if not _confirmation_group_is_pam_self_loop(members)
    }


def _confirmation_group_is_pam_self_loop(
    members: list[dict[str, Any]],
) -> bool:
    """Hide PAM's own confirmation UI without suppressing real AX activity.

    Older business-episode rows can be attributed to macOS' generic
    Accessibility Services process even when the captured surface was PAM.
    Re-run the explicit self-page text rule after aggregation.  For old rows
    whose normalized card text no longer retains those words, use only the
    narrow legacy signature observed on unresolved PAM loops: generic AX
    attribution, a weak and unverified business episode, and a question that
    is already unanswerable because its subject is anonymous or only context.

    This is presentation-only.  It neither mutates nor deletes the retained
    item/evidence rows, and a concrete, answerable business event attributed
    through Accessibility Services remains visible.
    """
    if not members:
        return False
    apps = _unique(
        [
            safe_decision_text(row.get("app_family") or "", max_len=80)
            for row in members
        ]
    )
    if apps and all(_is_personal_ai_monitor_app(app) for app in apps):
        return True

    text_parts: list[str] = []
    for row in members:
        subject = _json_object(row.get("subject"))
        text_parts.extend(
            str(value or "")
            for value in (
                row.get("evidence"),
                subject.get("object"),
                subject.get("label"),
            )
            if value
        )
    combined = safe_decision_text(
        " ".join(_unique(text_parts)),
        max_len=4800,
    )
    if CONFIRMATION_UI_RE.search(combined):
        return True
    if (
        META_DISCUSSION_RE.search(combined)
        and apps
        and all(_is_generic_accessibility_services_app(app) for app in apps)
    ):
        return True
    if not apps or not all(
        _is_generic_accessibility_services_app(app) for app in apps
    ):
        return False
    if not all(
        str(row.get("candidate_origin") or "") == "business_episode"
        for row in members
    ):
        return False

    reason_codes = {
        code
        for row in members
        for code in _normalized_tokens(_json_list(row.get("reason_codes")))
    }
    legacy_signature = {
        "business_episode_candidate",
        "policy_v6_evaluated",
        f"{CONFIRMATION_POLICY_REASON_PREFIX}_evaluated",
        f"{CONFIRMATION_POLICY_REASON_PREFIX}_trigger_unverified",
        "weak_occurrence_signal",
    }
    if not legacy_signature.issubset(reason_codes):
        return False
    signal_types = {
        signal
        for row in members
        for signal in _normalized_tokens(_safe_tokens(row.get("safe_signal_types")))
    }
    assessment = _confirmation_group_question_assessment(members)
    low_information = bool(
        assessment["status"] != "answerable"
        and (
            any(
                str(row.get("question_kind") or "") == "business_context"
                for row in members
            )
            or "anonymous_subject" in signal_types
            or any(
                code.endswith("_information_insufficient_missing_subject")
                for code in reason_codes
            )
        )
    )
    return low_information


def _confirmation_group_question_assessment(
    members: list[dict[str, Any]],
) -> dict[str, Any]:
    if not members:
        return _question_assessment_result("information_insufficient", "missing_subject", {})
    def joined(key: str, fallback: str = "", limit: int = 1800) -> str:
        values = [
            safe_decision_text(
                row.get(key) or (row.get(fallback) if fallback else ""),
                max_len=700,
            )
            for row in members
        ]
        return safe_decision_text(" ".join(_unique(values)), max_len=limit)

    event_type = str(members[0].get("event_type") or "")
    return _confirmation_question_assessment(
        event_type=event_type,
        subject=_canonical_cluster_subject(members),
        evidence=joined("evidence", limit=1200),
        full_text=joined("cleaned_content", "safe_observation"),
        app_family=joined("app_family", limit=320),
        reason_codes=[
            value for row in members for value in _json_list(row.get("reason_codes"))
        ],
        safe_signal_types=[
            value
            for row in members
            for value in _safe_tokens(row.get("safe_signal_types"))
        ],
        path_probe=[
            (row.get("_card_subject"), row.get("subject"))
            for row in members
        ],
        question_kind=(
            "business_context"
            if any(
                str(row.get("question_kind") or "occurrence")
                == "business_context"
                for row in members
            )
            else str(members[0].get("question_kind") or "occurrence")
        ),
    )


def _question_assessment_result(
    status: str,
    reason: str,
    subject: dict[str, Any],
    *,
    privacy_safe: bool = False,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "subject": subject,
        "privacy_safe": privacy_safe,
    }


def _confirmation_question_assessment(
    *,
    event_type: str,
    subject: dict[str, Any],
    evidence: str,
    full_text: str,
    app_family: str,
    reason_codes: list[str] | tuple[str, ...] | set[str],
    safe_signal_types: list[str] | tuple[str, ...] | set[str],
    path_probe: Any = None,
    question_kind: str = "occurrence",
) -> dict[str, Any]:
    """Shared fail-closed gate for candidates, revalidation, cards, and writers."""
    if contains_absolute_path(
        (subject, evidence, full_text, app_family, path_probe)
    ):
        return _question_assessment_result(
            "information_insufficient",
            "absolute_path_redacted",
            _question_safe_unknown_subject(event_type),
        )
    organized = _organized_confirmation_subject(
        event_type,
        subject,
        evidence=evidence,
    )
    canonical_object = _canonical_event_object(event_type, organized, evidence)
    combined = safe_decision_text(
        " ".join(
            _unique(
                [
                    safe_decision_text(canonical_object, max_len=160),
                    safe_decision_text(evidence, max_len=600),
                    safe_decision_text(full_text, max_len=1000),
                ]
            )
        ),
        max_len=1800,
    )
    normalized_reason_codes = _normalized_tokens(reason_codes)
    normalized_signal_types = _normalized_tokens(safe_signal_types)
    hard_reasons = _confirmation_semantic_filter_reasons(
        subject=subject,
        evidence=evidence,
        full_text=full_text,
    )
    if hard_reasons:
        return _question_assessment_result(
            "filtered",
            hard_reasons[0].removeprefix(f"{CONFIRMATION_POLICY_REASON_PREFIX}_"),
            _question_safe_unknown_subject(event_type),
        )
    if question_kind == "business_context":
        return _question_assessment_result(
            "information_insufficient", "business_context", organized
        )
    if _is_aggregate_activity_statistic(subject.get("object") or canonical_object) or (
        not canonical_object
        and (
            _is_aggregate_activity_statistic(evidence)
            or f"{CONFIRMATION_POLICY_REASON_PREFIX}_information_insufficient_"
            "aggregate_summary_unanswerable" in normalized_reason_codes
        )
    ):
        return _question_assessment_result(
            "information_insufficient", "aggregate_summary_unanswerable", organized
        )
    if (
        f"{CONFIRMATION_POLICY_REASON_PREFIX}_information_insufficient_"
        "recruitment_chat_privacy"
        in normalized_reason_codes
        or _looks_like_recruitment_chat_delivery(app_family, combined)
    ):
        return _question_assessment_result(
            "information_insufficient",
            "recruitment_chat_privacy",
            _question_safe_unknown_subject(event_type),
            privacy_safe=True,
        )
    if (
        event_type == "completion"
        and _looks_like_generated_document_claim(
            app_family=app_family,
            object_label=canonical_object,
            evidence=evidence,
            full_text=full_text,
        )
        and not _has_independent_artifact_evidence(
            normalized_reason_codes,
            normalized_signal_types,
        )
    ):
        return _question_assessment_result(
            "information_insufficient", "generated_artifact_unverified", organized
        )
    if (
        event_type
        in {"completion", "send", "submission", "publish", "application", "purchase"}
        and (_is_ai_tool_app(app_family) or _is_ai_agent_terminal_app(app_family))
        and not _has_independent_artifact_evidence(
            normalized_reason_codes,
            normalized_signal_types,
        )
    ):
        # AI chat and terminal text cannot establish whose action is described.
        # A chat's send/submit control is not an external submission receipt;
        # geometry and interaction hints must not bypass this gate. Named
        # interview/meeting/call events remain outside this branch.
        return _question_assessment_result(
            "information_insufficient", "ai_tool_activity_unverified", organized
        )
    if not canonical_object:
        raw_object = safe_decision_text(subject.get("object") or "", max_len=180)
        publish_dimensions = (
            _named_publish_dimensions(raw_object)
            if event_type == "publish"
            else ("", "", "")
        )
        malformed = bool(
            raw_object
            and (
                _normalized_phrase(raw_object)
                in GENERIC_CONFIRMATION_OBJECT_IDENTITIES
                or ANAPHORIC_OBJECT_RE.fullmatch(raw_object)
                or TRUNCATED_OBJECT_RE.search(raw_object)
                or (
                    event_type == "publish"
                    and (
                        NAMED_ENVIRONMENT_ONLY_RE.fullmatch(raw_object)
                        or (
                            bool(publish_dimensions[0])
                            and bool(publish_dimensions[1])
                            and not publish_dimensions[2]
                        )
                    )
                )
            )
        )
        return _question_assessment_result(
            "information_insufficient",
            "malformed_subject" if malformed else "missing_subject",
            organized,
        )
    if (
        event_type == "publish"
        and f"{CONFIRMATION_POLICY_REASON_PREFIX}_trigger_unverified"
        in normalized_reason_codes
        and INDEPENDENT_PLATFORM_EVIDENCE_SIGNAL not in normalized_signal_types
    ):
        return _question_assessment_result(
            "information_insufficient",
            "publish_target_unverified",
            _question_safe_unknown_subject(event_type),
        )
    return _question_assessment_result("answerable", "identified", organized)


def _is_aggregate_activity_statistic(value: Any) -> bool:
    text = safe_decision_text(value or "", max_len=240)
    remainder, count = ACTIVITY_STATISTIC_RE.subn("", text)
    return bool(count and ACTIVITY_STATISTIC_REMAINDER_RE.fullmatch(remainder))


def _normalized_tokens(values: Any) -> set[str]:
    return {
        str(value).strip().casefold()
        for value in values
        if str(value or "").strip()
    }


def _looks_like_recruitment_chat_delivery(app_family: str, text: str) -> bool:
    return bool(
        RECRUITMENT_CHAT_DELIVERY_RE.search(text)
        or (
            RECRUITMENT_APP_RE.search(app_family)
            and CHAT_DELIVERY_STATUS_RE.search(text)
        )
    )


def _confirmation_semantic_filter_reasons(
    *,
    subject: dict[str, Any],
    evidence: str,
    full_text: str,
) -> list[str]:
    combined = safe_decision_text(
        " ".join(
            _unique([str(subject.get("object") or ""), evidence, full_text])
        ),
        max_len=1800,
    )
    object_label = safe_decision_text(subject.get("object") or "", max_len=180)
    reasons: list[str] = []
    if INTERNAL_IMPLEMENTATION_FIELD_STATUS_RE.search(combined):
        reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_internal_field_status")
    fragments = [object_label, evidence, *re.split(r"[\r\n。！？!?；;,，]+", full_text)]
    if any(
        NEGATED_FRAGMENT_RE.fullmatch(safe_decision_text(value, max_len=180))
        for value in fragments
        if value
    ):
        reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_negated_fragment")
    return _unique(reasons)


def _question_safe_unknown_subject(
    event_type: str,
) -> dict[str, Any]:
    return {
        "event_label": _event_label(event_type),
        "object": "",
        "label": f"{_event_label(event_type)}对象未识别",
        "object_identified": False,
        "object_ambiguous": True,
    }


def _looks_like_generated_document_claim(
    *,
    app_family: str,
    object_label: str,
    evidence: str,
    full_text: str,
) -> bool:
    combined = safe_decision_text(
        " ".join(
            _unique(
                [
                    safe_decision_text(object_label, max_len=180),
                    safe_decision_text(evidence, max_len=600),
                    safe_decision_text(full_text, max_len=1000),
                ]
            )
        ),
        max_len=1800,
    )
    compact_app = re.sub(r"[^0-9a-z]+", "", str(app_family or "").casefold())
    generative_surface = _is_ai_tool_app(app_family) or compact_app in {
        "dia",
        "diabrowser",
    }
    return bool(
        GENERATED_DOCUMENT_CLAIM_RE.search(combined)
        or (
            generative_surface
            and GENERATED_DOCUMENT_OBJECT_RE.search(combined)
        )
    )


def _generated_document_object(value: str) -> str:
    """Recover only a bounded, typed document object from a generation claim."""
    text = safe_decision_text(value or "", max_len=1000)
    page_match = re.search(r"(?<!\d)(\d{1,3})\s*页", text, re.I)
    if not page_match:
        return ""
    pages = int(page_match.group(1))
    if pages <= 0 or pages > 500:
        return ""
    nearby = text[
        max(0, page_match.start() - 40) : min(len(text), page_match.end() + 100)
    ]
    if re.search(r"\bWord\s*版?\s*(?:报告|文档)\b", nearby, re.I):
        return f"{pages}页 Word 版报告"
    if re.search(r"\bPDF\s*(?:报告|文档)\b", nearby, re.I):
        return f"{pages}页 PDF 报告"
    if re.search(r"\bPPTX?\s*(?:报告|演示|幻灯片)?\b", nearby, re.I):
        return f"{pages}页 PPT 演示文稿"
    typed_match = re.search(r"(报告|文档|方案|简历|文章|手册)", nearby, re.I)
    if typed_match:
        return f"{pages}页{typed_match.group(1)}"
    return ""


def _has_independent_artifact_evidence(
    reason_codes: set[str],
    signal_types: set[str],
) -> bool:
    accepted = frozenset(
        {
            INDEPENDENT_FILE_EVIDENCE_SIGNAL,
            INDEPENDENT_GIT_EVIDENCE_SIGNAL,
            INDEPENDENT_PLATFORM_EVIDENCE_SIGNAL,
            "confirmed_file_event",
            "file_change_event",
            "document_modified_near_capture",
            "git_commit_receipt",
            "git_push_receipt",
            "platform_delivery_receipt",
            "platform_publish_receipt",
        }
    )
    return bool((reason_codes | signal_types).intersection(accepted))


def _activity_independent_artifact_signals(
    activity: dict[str, Any],
    region_reason_codes: list[str],
) -> list[str]:
    signals: list[str] = []
    contexts_value = activity.get("document_contexts")
    if isinstance(contexts_value, str):
        try:
            contexts_value = json.loads(contexts_value)
        except json.JSONDecodeError:
            contexts_value = []
    contexts = (
        [item for item in contexts_value if isinstance(item, dict)]
        if isinstance(contexts_value, list)
        else []
    )
    if any(
        str(context.get("subsequent_mtime_event") or "")
        == "modified_near_capture"
        and bool(
            context.get("document_ref")
            or context.get("file_name")
            or context.get("change_summary")
        )
        for context in contexts
    ):
        signals.append(INDEPENDENT_FILE_EVIDENCE_SIGNAL)
    normalized_reasons = {str(code or "").strip().casefold() for code in region_reason_codes}
    if normalized_reasons.intersection(
        {"git_commit_receipt", "git_push_receipt", INDEPENDENT_GIT_EVIDENCE_SIGNAL}
    ):
        signals.append(INDEPENDENT_GIT_EVIDENCE_SIGNAL)
    if normalized_reasons.intersection(
        {
            "platform_delivery_receipt",
            "platform_publish_receipt",
            INDEPENDENT_PLATFORM_EVIDENCE_SIGNAL,
        }
    ):
        signals.append(INDEPENDENT_PLATFORM_EVIDENCE_SIGNAL)
    return _unique(signals)


def _confirmation_group_section(members: list[dict[str, Any]]) -> str:
    if any(bool(row.get("_card_group_key_collision")) for row in members):
        return "information_insufficient"
    return (
        "pending_confirmation"
        if _confirmation_group_question_assessment(members)["status"] == "answerable"
        else "information_insufficient"
    )


def _confirmation_rows_are_answerable(members: list[dict[str, Any]]) -> bool:
    """Keep every writer aligned with the fail-closed Cards V5 classification."""
    return bool(members) and _confirmation_group_section(members) == "pending_confirmation"


def _confirmation_group_priority_rank(members: list[dict[str, Any]]) -> int:
    ranks = {"high": 3, "normal": 2, "low": 1}
    return max(
        (ranks.get(str(row.get("priority") or "normal"), 2) for row in members),
        default=2,
    )


def _prepare_confirmation_card_row(row: dict[str, Any]) -> None:
    if "_card_identity" in row:
        return
    event_type = str(row.get("event_type") or "")
    subject = _json_object(row.get("subject"))
    try:
        start_dt = _confirmation_card_datetime(str(row.get("observed_start") or ""))
        end_dt = _confirmation_card_datetime(
            str(row.get("observed_end") or row.get("observed_start") or "")
        )
        start_timestamp = start_dt.timestamp()
        end_timestamp = end_dt.timestamp()
        day = start_dt.date().isoformat()
    except (TypeError, ValueError):
        start_timestamp = 0.0
        end_timestamp = 0.0
        day = str(row.get("observed_start") or "")[:10]
    organized_subject = _organized_confirmation_subject(
        event_type,
        subject,
        evidence=str(row.get("evidence") or ""),
    )
    row["_card_subject"] = organized_subject
    row["_card_identity"] = _subject_identity(
        organized_subject,
        event_type,
        str(row.get("evidence") or ""),
    )
    row["_card_publish_identity"] = (
        _publish_semantic_identity(subject, str(row.get("evidence") or ""))
        if event_type == "publish"
        else ""
    )
    row["_card_day"] = day
    row["_card_start_timestamp"] = start_timestamp
    row["_card_end_timestamp"] = end_timestamp
    row["_card_app"] = _normalized_app_family(str(row.get("app_family") or "unknown"))
    row["_card_source_ids"] = tuple(_json_list(row.get("source_activity_ids")))
    row["_card_deletion_group_ids"] = tuple(_json_list(row.get("deletion_group_ids")))


def _confirmation_card_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    # PAM persists local capture timestamps with an explicit offset.  Preserve
    # that local wall-clock value; only UTC-form legacy rows need conversion.
    if str(value).endswith("Z") or str(value).endswith("+00:00"):
        return parsed.astimezone()
    return parsed


def _card_cluster_can_accept_confirmation_row(
    cluster: list[dict[str, Any]],
    row: dict[str, Any],
) -> bool:
    if not cluster:
        return False
    row_identity = tuple(row.get("_card_identity") or ())
    if not any(row_identity):
        durable_group_key = str(row.get("group_key") or "")
        return bool(
            durable_group_key
            and all(
                str(member.get("group_key") or "") == durable_group_key
                and str(member.get("event_type") or "")
                == str(row.get("event_type") or "")
                and str(member.get("_card_day") or "")
                == str(row.get("_card_day") or "")
                and not any(tuple(member.get("_card_identity") or ()))
                for member in cluster
            )
        )
    identities: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for member in cluster:
        identities.setdefault(tuple(member.get("_card_identity") or ()), []).append(member)
    if row_identity in identities:
        return any(_card_rows_can_group(member, row) for member in identities[row_identity])
    return bool(identities) and all(
        any(_card_rows_can_group(member, row) for member in members)
        for members in identities.values()
    )


def _card_rows_can_group(left: dict[str, Any], right: dict[str, Any]) -> bool:
    event_type = str(left.get("event_type") or "")
    if event_type != str(right.get("event_type") or ""):
        return False
    if str(left.get("_card_day") or "") != str(right.get("_card_day") or ""):
        return False
    left_episode = str(left.get("episode_id") or "")
    right_episode = str(right.get("episode_id") or "")
    if left_episode and right_episode and left_episode != right_episode:
        return False
    same_episode = bool(left_episode and left_episode == right_episode)
    left_start = float(left.get("_card_start_timestamp") or 0.0)
    right_start = float(right.get("_card_start_timestamp") or 0.0)
    if event_type == "publish":
        return bool(
            (same_episode or abs(right_start - left_start) <= 15 * 60)
            and _publish_identities_compatible(
                str(left.get("_card_publish_identity") or ""),
                str(right.get("_card_publish_identity") or ""),
            )
        )
    if event_type in REPEATABLE_EVENT_TYPES and not same_episode:
        if left_start > right_start:
            left, right = right, left
            left_start, right_start = right_start, left_start
        left_end = float(left.get("_card_end_timestamp") or left_start)
        if right_start > max(left_end, left_start) + 30 * 60:
            return False
    left_identity = tuple(left.get("_card_identity") or ())
    right_identity = tuple(right.get("_card_identity") or ())
    if not any(left_identity) or not any(right_identity):
        return False
    if left_identity == right_identity:
        return True
    if event_type != "completion":
        return False
    left_object = left_identity[0] if left_identity else ""
    right_object = right_identity[0] if right_identity else ""
    if min(len(left_object), len(right_object)) < 8:
        return False
    if left_object not in right_object and right_object not in left_object:
        return False
    left_app = str(left.get("_card_app") or "unknown")
    right_app = str(right.get("_card_app") or "unknown")
    if not (_unknown_app(left_app) or _unknown_app(right_app) or left_app == right_app):
        return False
    return abs(right_start - left_start) <= 15 * 60


def _confirmation_group_source_ids(rows: list[dict[str, Any]]) -> set[str]:
    return {
        source_id
        for row in rows
        for source_id in _confirmation_row_ids(
            row,
            "source_activity_ids",
            "_card_source_ids",
        )
        if source_id
    }


def _confirmation_row_ids(
    row: dict[str, Any],
    field: str,
    cache_field: str,
) -> tuple[str, ...]:
    cached = row.get(cache_field)
    if isinstance(cached, tuple):
        return cached
    values = tuple(_json_list(row.get(field)))
    row[cache_field] = values
    return values


def _live_confirmation_rows(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_ids = {
        source_id
        for row in rows
        for source_id in _confirmation_row_ids(
            row,
            "source_activity_ids",
            "_card_source_ids",
        )
        if source_id
    }
    deletion_group_ids = {
        group_id
        for row in rows
        for group_id in _confirmation_row_ids(
            row,
            "deletion_group_ids",
            "_card_deletion_group_ids",
        )
        if group_id
    }
    live_source_ids = _existing_activity_values(conn, "id", source_ids)
    live_deletion_group_ids = _existing_activity_values(
        conn,
        "deletion_group_id",
        deletion_group_ids,
    )
    output: list[dict[str, Any]] = []
    for row in rows:
        row_sources = set(
            _confirmation_row_ids(row, "source_activity_ids", "_card_source_ids")
        )
        row_groups = set(
            _confirmation_row_ids(row, "deletion_group_ids", "_card_deletion_group_ids")
        )
        if not row_sources and not row_groups:
            output.append(row)
        elif row_sources.intersection(live_source_ids) or row_groups.intersection(
            live_deletion_group_ids
        ):
            output.append(row)
    return output


def _existing_activity_values(
    conn: sqlite3.Connection,
    column: str,
    values: set[str],
) -> set[str]:
    if column not in {"id", "deletion_group_id"}:
        raise ValueError("unsupported activity provenance column")
    found: set[str] = set()
    ordered = sorted(values)
    for index in range(0, len(ordered), 800):
        chunk = ordered[index : index + 800]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        found.update(
            str(row[0] or "")
            for row in conn.execute(
                f"SELECT DISTINCT {column} FROM extracted_activities "
                f"WHERE {column} IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            if str(row[0] or "")
        )
    return found


def _confirmation_card_revision(rows: list[dict[str, Any]]) -> str:
    members = [
        "|".join(
            (
                str(row.get("item_key") or ""),
                str(row.get("group_key") or ""),
                str(row.get("group_signature") or ""),
                str(row.get("state") or ""),
                str(row.get("observed_start") or ""),
                str(row.get("observed_end") or ""),
                str(row.get("updated_at") or ""),
                ",".join(
                    sorted(
                        _confirmation_row_ids(
                            row,
                            "source_activity_ids",
                            "_card_source_ids",
                        )
                    )
                ),
                ",".join(
                    sorted(
                        _confirmation_row_ids(
                            row,
                            "deletion_group_ids",
                            "_card_deletion_group_ids",
                        )
                    )
                ),
            )
        )
        for row in rows
    ]
    return "cards_" + _hash(
        "\n".join([CONFIRMATION_POLICY_QUESTION_SAFETY_REASON, *sorted(members)])
    )[:24]


def _encode_confirmation_card_cursor(revision: str, offset: int) -> str:
    return f"{revision}.{max(0, int(offset))}"


def _decode_confirmation_card_cursor(cursor: str | None, revision: str) -> tuple[int, bool]:
    if not cursor:
        return 0, False
    prefix, separator, raw_offset = str(cursor).rpartition(".")
    if not separator or prefix != revision:
        return 0, True
    try:
        return max(0, int(raw_offset)), False
    except ValueError:
        return 0, True


def _question_groups(conn: sqlite3.Connection, rows: Any) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        group_key = str(row.get("group_key") or "") or f"legacy_{row.get('item_key') or ''}"
        grouped.setdefault(group_key, []).append(row)
    output = [
        _public_question_group(group_key, members, conn=conn)
        for group_key, members in grouped.items()
    ]
    output.sort(
        key=lambda group: (str(group.get("observed_start") or ""), str(group.get("group_key") or "")),
        reverse=True,
    )
    output.sort(
        key=lambda group: {"pending": 0, "deferred": 1}.get(str(group.get("state") or ""), 2)
    )
    return output


def _public_question_group(
    group_key: str,
    members: list[dict[str, Any]],
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    ordered = sorted(
        members,
        key=lambda row: (str(row.get("observed_start") or ""), str(row.get("item_key") or "")),
    )
    subjects = [_json_object(row.get("subject")) for row in ordered]
    subject = max(
        subjects,
        key=lambda item: (
            bool(item.get("company")),
            bool(item.get("round")),
            len(str(item.get("label") or "")),
        ),
        default=_subject_for_event("", "活动", "", ""),
    )
    starts = [str(row.get("observed_start") or "") for row in ordered]
    ends = [str(row.get("observed_end") or row.get("observed_start") or "") for row in ordered]
    observed_start = min(starts) if starts else ""
    observed_end = max(ends) if ends else observed_start
    states = [str(row.get("state") or "pending") for row in ordered]
    if any(state == "pending" for state in states):
        state = "pending"
    elif any(state == "deferred" for state in states):
        state = "deferred"
    elif states and len(set(states)) == 1:
        state = states[0]
    else:
        state = "rejected"
    low_value_question = (
        state in UNRESOLVED_STATES
        and _confirmation_group_question_assessment(ordered)["reason"]
        in LOW_VALUE_QUESTION_REASONS
    )
    content_counts: dict[str, int] = {}
    evidence_counts: dict[str, int] = {}
    source_ids: set[str] = set()
    reason_codes: list[str] = []
    clarification: dict[str, Any] = {"status": "not_requested"}
    clarification_at = ""
    for row in ordered:
        content = safe_decision_text(
            row.get("cleaned_content") or row.get("safe_observation") or "",
            max_len=1600,
        )
        if content:
            content_counts[content] = content_counts.get(content, 0) + 1
        evidence = safe_decision_text(row.get("evidence") or "", max_len=280)
        if evidence:
            evidence_counts[evidence] = evidence_counts.get(evidence, 0) + 1
        source_ids.update(_json_list(row.get("source_activity_ids")))
        reason_codes.extend(_json_list(row.get("reason_codes")))
        candidate_clarification = _public_clarification(row.get("clarification"))
        candidate_at = str(candidate_clarification.get("generated_at") or "")
        if candidate_clarification.get("status") != "not_requested" and candidate_at >= clarification_at:
            clarification = candidate_clarification
            clarification_at = candidate_at
    trigger_sentence = ""
    if evidence_counts:
        trigger_sentence = sorted(
            evidence_counts,
            key=lambda value: (-evidence_counts[value], len(value), value),
        )[0]
    raw_answers = [str(row.get("answer") or "") for row in ordered]
    answers = set(raw_answers)
    answer_reasons = {str(row.get("answer_reason") or "") for row in ordered}
    scopes = {str(row.get("generalize_scope") or "exact") for row in ordered}
    apps = _unique(
        [safe_decision_text(row.get("app_family") or "", max_len=80) for row in ordered]
    )
    public_app = _public_confirmation_app(apps)
    group_signature = str(ordered[0].get("group_signature") or "") if ordered else ""
    evidence = (
        evidence_summary(conn, [str(row.get("item_key") or "") for row in ordered])
        if conn is not None
        else {"evidence_status": "unavailable", "evidence_count": 0, "available_source_count": 0}
    )
    priority_order = {"high": 3, "normal": 2, "low": 1}
    priorities = [
        str(row.get("priority") or "normal")
        for row in ordered
        if str(row.get("priority") or "normal") in PRIORITIES
    ]
    priority = max(priorities, key=lambda value: priority_order[value], default="normal")
    question_kinds = [
        str(row.get("question_kind") or "occurrence")
        for row in ordered
        if str(row.get("question_kind") or "occurrence") in QUESTION_KINDS
    ]
    candidate_origins = [
        str(row.get("candidate_origin") or "explicit_claim")
        for row in ordered
        if str(row.get("candidate_origin") or "explicit_claim") in CANDIDATE_ORIGINS
    ]
    episode_ids = _unique([str(row.get("episode_id") or "") for row in ordered])
    safe_signal_types = _unique(
        [token for row in ordered for token in _safe_tokens(row.get("safe_signal_types"))]
    )
    suggested_workstreams = _unique(
        [safe_decision_text(row.get("suggested_workstream") or "", max_len=120) for row in ordered]
    )
    suggested_outcomes = _unique(
        [
            _normalized_outcome(row.get("suggested_outcome") or "unknown")
            for row in ordered
        ]
    )
    user_workstreams = _unique(
        [safe_decision_text(row.get("user_workstream") or "", max_len=120) for row in ordered]
    )
    user_outcomes = _unique(
        [
            _normalized_outcome(row.get("user_outcome"))
            for row in ordered
            if str(row.get("user_outcome") or "")
        ]
    )
    return {
        "group_key": group_key,
        "group_signature": group_signature,
        "member_revision": _member_revision(ordered),
        "event_type": str(ordered[0].get("event_type") or "") if ordered else "",
        "question_kind": question_kinds[0] if question_kinds else "occurrence",
        "candidate_origin": candidate_origins[0] if candidate_origins else "explicit_claim",
        "priority": "low" if low_value_question else priority,
        "episode_id": episode_ids[0] if len(episode_ids) == 1 else "",
        "safe_signal_types": safe_signal_types[:12],
        "suggested_workstream": suggested_workstreams[0] if suggested_workstreams else "",
        "suggested_outcome": suggested_outcomes[0] if suggested_outcomes else "unknown",
        "user_workstream": user_workstreams[0] if len(user_workstreams) == 1 else "",
        "user_outcome": user_outcomes[0] if len(user_outcomes) == 1 else "",
        "notification_eligible": bool(
            not low_value_question
            and state in UNRESOLVED_STATES
            and any(int(row.get("notification_eligible") or 0) for row in ordered)
        ),
        "subject": subject,
        "subject_label": safe_decision_text(subject.get("label") or "", max_len=100),
        "trigger_sentence": trigger_sentence,
        "decision_question": "" if low_value_question else _decision_question(subject, observed_start),
        "why_uncertain": "这项判断只来自本机已清洗文字，需要你确认现实中是否真的发生。",
        "app": public_app,
        "time_label": _time_label(observed_start, observed_end),
        "observed_start": observed_start,
        "observed_end": observed_end,
        "confidence": round(max((float(row.get("confidence") or 0.0) for row in ordered), default=0.0), 3),
        "reason_codes": _unique(reason_codes)[:20],
        "state": state,
        "answer_options": [] if low_value_question else ANSWER_OPTIONS,
        "user_answer": next(iter(answers)) if len(answers) == 1 and "" not in answers else "",
        "answer_reason": (
            next(iter(answer_reasons))
            if len(answer_reasons) == 1 and "" not in answer_reasons and "" not in answers
            else ""
        ),
        "generalize_scope": next(iter(scopes)) if len(scopes) == 1 and "" not in answers else "exact",
        "answered_at": max((str(row.get("answered_at") or "") for row in ordered), default=""),
        "source_count": len(source_ids) if source_ids else len(ordered),
        **evidence,
        "cleaned_contents": [
            {"text": text, "count": count}
            for text, count in sorted(content_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "clarification": clarification,
    }


def _public_confirmation_card_v5(
    group_key: str,
    members: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(
        members,
        key=lambda row: (str(row.get("observed_start") or ""), str(row.get("item_key") or "")),
    )
    event_type = str(ordered[0].get("event_type") or "") if ordered else ""
    subject = _canonical_cluster_subject(ordered)
    observed_start = min(
        (str(row.get("observed_start") or "") for row in ordered),
        default="",
    )
    observed_end = max(
        (str(row.get("observed_end") or row.get("observed_start") or "") for row in ordered),
        default=observed_start,
    )
    apps = _unique(
        [safe_decision_text(row.get("app_family") or "", max_len=80) for row in ordered]
    )
    app = _public_confirmation_app(apps)
    evidence = next(
        (
            safe_decision_text(row.get("evidence") or "", max_len=280)
            for row in reversed(ordered)
            if safe_decision_text(row.get("evidence") or "", max_len=280)
        ),
        "",
    )
    presentation = _confirmation_presentation(
        event_type=event_type,
        subject=subject,
        evidence=evidence,
        app_family=app,
        observed_start=observed_start,
    )
    question_kinds = {
        str(row.get("question_kind") or "occurrence") for row in ordered
    }
    if "business_context" in question_kinds:
        presentation = _business_context_presentation(
            presentation,
            app_family=app,
            observed_start=observed_start,
        )
    assessment = _confirmation_group_question_assessment(ordered)
    if assessment["status"] != "answerable":
        presentation = _question_safety_information_presentation(
            event_type=event_type,
            assessment=assessment,
            app_family=app,
            observed_start=observed_start,
        )
    if any(bool(row.get("_card_group_key_collision")) for row in ordered):
        presentation = {
            "canonical_subject": presentation["canonical_subject"],
            "subject_status": "information_insufficient",
            "semantic_key": presentation["semantic_key"],
            "display_title": "信息不足：旧线索分组存在对象冲突",
            "display_question": "",
            "display_evidence": (
                "同一旧分组中识别到多个不同对象，已分别保留展示；"
                "完成安全重整前不会要求你确认。"
            ),
            "verification_route": "information_insufficient",
        }
    presentation["semantic_key"] = _confirmation_card_semantic_key(
        event_type=event_type,
        canonical_subject=dict(presentation.get("canonical_subject") or {}),
        members=ordered,
        group_key=group_key,
        observed_start=observed_start,
    )
    states = {str(row.get("state") or "pending") for row in ordered}
    state = "pending" if "pending" in states else "deferred"
    priority_order = {"high": 3, "normal": 2, "low": 1}
    priority = max(
        (
            str(row.get("priority") or "normal")
            for row in ordered
            if str(row.get("priority") or "normal") in PRIORITIES
        ),
        key=lambda value: priority_order[value],
        default="normal",
    )
    if assessment["reason"] in LOW_VALUE_QUESTION_REASONS:
        priority = "low"
    source_ids = _confirmation_group_source_ids(ordered)
    reason_codes = _unique(
        [code for row in ordered for code in _json_list(row.get("reason_codes"))]
    )
    card = {
        "group_key": group_key,
        "member_revision": _member_revision(ordered),
        "event_type": event_type,
        "section": (
            "pending_confirmation"
            if presentation["subject_status"] == "identified"
            else "information_insufficient"
        ),
        "priority": priority,
        "state": state,
        "canonical_subject": presentation["canonical_subject"],
        "subject_status": presentation["subject_status"],
        "semantic_key": presentation["semantic_key"],
        "display_title": presentation["display_title"],
        "display_question": presentation["display_question"],
        "display_evidence": presentation["display_evidence"],
        "verification_route": presentation["verification_route"],
        "app": app,
        "time_label": _time_label(observed_start, observed_end),
        "observed_start": observed_start,
        "observed_end": observed_end,
        "source_count": len(source_ids) if source_ids else len(ordered),
        "reason_codes": reason_codes[:20],
        "answer_options": (
            ANSWER_OPTIONS
            if presentation["subject_status"] == "identified"
            else []
        ),
        "raw_access": False,
    }
    return redact_absolute_paths_in_value(card)


def _confirmation_presentation(
    *,
    event_type: str,
    subject: dict[str, Any],
    evidence: str,
    app_family: str,
    observed_start: str,
) -> dict[str, Any]:
    event_label = _event_label(event_type)
    object_label = _canonical_event_object(event_type, subject, evidence)
    identified = bool(object_label)
    separator = " " if identified and re.match(r"[A-Za-z0-9]", object_label) else ""
    canonical_subject = {
        "event_label": event_label,
        "object": object_label,
        "label": (
            object_label
            if identified
            else f"{event_label}对象未识别"
        ),
        "object_identified": identified,
    }
    if event_type == "interview":
        company = safe_decision_text(subject.get("company") or "", max_len=40)
        round_label = safe_decision_text(subject.get("round") or "", max_len=20)
        canonical_subject.update({"company": company, "round": round_label})
    subject_status = "identified" if identified else "information_insufficient"
    semantic_identity = _normalized_phrase(object_label) if identified else "unknown"
    semantic_key = "sem_" + _hash(
        "|".join((_local_day(observed_start), event_type, semantic_identity))
    )[:24]
    app_label = _presentation_app_label(app_family)
    if not identified:
        return {
            "canonical_subject": canonical_subject,
            "subject_status": subject_status,
            "semantic_key": semantic_key,
            "display_title": f"信息不足：{event_label}对象未识别",
            "display_question": "",
            "display_evidence": (
                f"在{app_label}中检测到可能的{event_label}线索，但没有识别出具体对象。"
                "这条线索不会要求你回答“是否发生”。"
            ),
            "verification_route": "information_insufficient",
        }
    question_action = {
        "interview": "参加了",
        "meeting": "参加了",
        "application": "投递了",
        "submission": "提交了",
        "send": "发送了",
        "call": "进行了",
        "completion": "完成了",
        "purchase": "购买了",
        "publish": "发布了",
    }.get(event_type, "完成了")
    title_action = {
        "meeting": "参加",
        "application": "投递",
        "submission": "提交",
        "send": "发送",
        "call": "进行",
        "completion": "完成",
        "purchase": "购买",
        "publish": "发布",
    }.get(event_type, "")
    receipt = {
        "publish": "发布回执",
        "send": "发送回执",
        "submission": "提交回执",
        "application": "投递回执",
        "purchase": "购买回执",
        "completion": "完成证据",
        "interview": "会议或日程证据",
        "meeting": "会议或日程证据",
        "call": "通话记录",
    }.get(event_type, "证据")
    title = object_label if event_type == "interview" else f"{title_action}{separator}{object_label}"
    latin_tail_separator = " " if re.search(r"[A-Za-z0-9]$", object_label) else ""
    evidence_claim = {
        "interview": f"可能参加了{separator}{object_label}",
        "meeting": f"可能参加了{separator}{object_label}",
        "application": f"可能已投递{separator}{object_label}",
        "submission": f"可能已提交{separator}{object_label}",
        "send": f"可能已发送{separator}{object_label}",
        "call": f"可能进行了{separator}{object_label}",
        "completion": f"可能已完成{separator}{object_label}",
        "purchase": f"可能已购买{separator}{object_label}",
        "publish": f"{object_label}{latin_tail_separator}可能已发布",
    }.get(event_type, f"可能已完成{separator}{object_label}")
    return {
        "canonical_subject": canonical_subject,
        "subject_status": subject_status,
        "semantic_key": semantic_key,
        "display_title": title,
        "display_question": (
            f"{_date_label(observed_start)}，你是否"
            f"{question_action}{separator}{object_label}？"
        ),
        "display_evidence": (
            f"在{app_label}中识别到“{evidence_claim}”的线索，"
            f"但尚未检测到独立{receipt}。"
        ),
        "verification_route": "user_confirmation",
    }


def _business_context_presentation(
    presentation: dict[str, Any],
    *,
    app_family: str,
    observed_start: str,
) -> dict[str, Any]:
    canonical_subject = dict(presentation.get("canonical_subject") or {})
    semantic_key = "sem_" + _hash(
        "|".join(
            (
                _local_day(observed_start),
                "business_context",
                _normalized_phrase(canonical_subject.get("object")) or "unknown",
            )
        )
    )[:24]
    return {
        "canonical_subject": canonical_subject,
        "subject_status": "information_insufficient",
        "semantic_key": semantic_key,
        "display_title": "信息不足：业务片段归属未确定",
        "display_question": "",
        "display_evidence": (
            f"在{_presentation_app_label(app_family)}中检测到一段可能相关的业务活动，"
            "但现有线索不足以形成“是否发生”的问题。"
        ),
        "verification_route": "information_insufficient",
    }


def _question_safety_information_presentation(
    *,
    event_type: str,
    assessment: dict[str, Any],
    app_family: str,
    observed_start: str,
) -> dict[str, Any]:
    reason = str(assessment.get("reason") or "missing_subject")
    subject = dict(assessment.get("subject") or {})
    event_label = _event_label(event_type)
    object_label = _canonical_event_object(event_type, subject)
    canonical_subject = {
        "event_label": event_label,
        "object": object_label,
        "label": object_label or f"{event_label}对象未识别",
        "object_identified": bool(object_label),
    }
    semantic_key = "sem_" + _hash(
        "|".join(
            (
                _local_day(observed_start),
                event_type,
                _normalized_phrase(object_label) or "unknown",
                reason,
            )
        )
    )[:24]
    if reason == "business_context":
        return _business_context_presentation(
            {
                "canonical_subject": canonical_subject,
                "semantic_key": semantic_key,
            },
            app_family=app_family,
            observed_start=observed_start,
        )
    if reason == "absolute_path_redacted":
        canonical_subject = {
            "event_label": event_label,
            "object": "",
            "label": f"{event_label}对象未识别",
            "object_identified": False,
        }
        title = "信息不足：本机路径已隐藏"
        evidence = (
            "线索包含本机绝对路径，路径已隐藏；"
            "不能仅依据这条线索确认现实事项。"
        )
    elif reason == "recruitment_chat_privacy":
        canonical_subject = {
            "event_label": event_label,
            "object": "",
            "label": f"{event_label}对象未识别",
            "object_identified": False,
        }
        title = "信息不足：招聘沟通发送对象未核实"
        evidence = (
                "检测到招聘沟通界面的送达或消息状态，但不会显示联系人、公司或消息正文，"
                "也不会据此要求你确认现实中的发送事项。"
        )
    elif reason == "generated_artifact_unverified":
        title = "信息不足：报告完成证据不足"
        evidence = (
                "检测到 AI 或浏览器中的文档生成声明，但没有独立文件变化、Git 回执"
                "或平台交付回执，因此不会要求你确认已经完成。"
        )
    elif reason == "aggregate_summary_unanswerable":
        title = "信息不足：只有处理数量，缺少具体事项"
        evidence = (
            "这条线索只是文件、内容或记录的数量汇总，没有说明具体完成或提交了什么。"
            "系统会保留线索，不要求你核对这些统计数字。"
        )
    elif reason == "ai_tool_activity_unverified":
        title = "信息不足：AI 工具界面的执行声明"
        evidence = (
            "这条线索来自 AI 聊天或终端，尚不能判断它描述的是谁的操作，"
            "也没有关联的独立结果证据。聊天发送按钮和重复出现的文字不能补足证据，"
            "因此不会要求你确认。"
        )
    elif reason == "publish_target_unverified":
        title = "信息不足：发布对象类型未识别"
        evidence = (
            "只识别到名称或环境线索，没有识别出网站、表单、文档等可发布对象，"
            "因此不会要求你确认是否发布。"
        )
    else:
        title = f"信息不足：{event_label}对象未识别"
        evidence = (
            f"在{_presentation_app_label(app_family)}中检测到可能的{event_label}线索，"
            "但没有识别出完整、具体的对象。这条线索不会要求你回答“是否发生”。"
        )
    return {
        "canonical_subject": canonical_subject,
        "subject_status": "information_insufficient",
        "semantic_key": semantic_key,
        "display_title": title,
        "display_question": "",
        "display_evidence": evidence,
        "verification_route": "information_insufficient",
    }


def _confirmation_card_semantic_key(
    *,
    event_type: str,
    canonical_subject: dict[str, Any],
    members: list[dict[str, Any]],
    group_key: str,
    observed_start: str,
) -> str:
    episode_ids = _unique(
        [str(row.get("episode_id") or "") for row in members if row.get("episode_id")]
    )
    binding = (
        f"episode:{episode_ids[0]}"
        if len(episode_ids) == 1
        else f"group:{group_key}"
    )
    window_minutes = 15 if event_type == "publish" else 30 if event_type in REPEATABLE_EVENT_TYPES else 0
    if window_minutes:
        try:
            window = str(
                int(
                    _confirmation_card_datetime(observed_start).timestamp()
                    // (window_minutes * 60)
                )
            )
        except (TypeError, ValueError):
            window = _local_day(observed_start)
    else:
        window = _local_day(observed_start)
    semantic_identity = _normalized_phrase(canonical_subject.get("object")) or "unknown"
    return "sem_" + _hash(
        "|".join((event_type, semantic_identity, binding, window))
    )[:24]


def _presentation_app_label(value: str) -> str:
    app = safe_decision_text(value or "", max_len=80)
    normalized = _normalized_app_family(app)
    if normalized == "chatgpt":
        return " ChatGPT "
    if "codex" in normalized:
        return " Codex "
    if _unknown_app(normalized) or normalized == "accessibility services":
        return "本机界面"
    return f" {app} "


def _prefer_revalidated_subject(
    event_type: str,
    stored_subject: dict[str, Any],
    parsed_subject: dict[str, Any],
    *,
    stored_evidence: str,
    parsed_evidence: str,
) -> dict[str, Any]:
    """Keep a safe stored object unless the new parse is a strict refinement."""
    stored = _organized_confirmation_subject(
        event_type,
        stored_subject,
        evidence=stored_evidence,
    )
    parsed = _organized_confirmation_subject(
        event_type,
        parsed_subject,
        evidence=parsed_evidence,
    )
    stored_object = _canonical_event_object(event_type, stored, stored_evidence)
    parsed_object = _canonical_event_object(event_type, parsed, parsed_evidence)
    if not stored_object:
        return parsed
    if not parsed_object:
        return stored
    stored_identity = _normalized_phrase(stored_object)
    parsed_identity = _normalized_phrase(parsed_object)
    if (
        stored_identity != parsed_identity
        and stored_identity
        and stored_identity in parsed_identity
        and len(parsed_identity) > len(stored_identity)
    ):
        return parsed
    return stored


def _canonical_event_object(
    event_type: str,
    subject: dict[str, Any],
    evidence: str = "",
) -> str:
    if contains_absolute_path((subject, evidence)):
        return ""
    if event_type == "interview":
        company = safe_decision_text(subject.get("company") or "", max_len=40)
        round_label = safe_decision_text(subject.get("round") or "", max_len=20)
        if company and round_label:
            return f"{company}{round_label}面试"
        if company:
            return f"{company}面试"
        # A round without a company or interview target is ambiguous and must
        # remain a low-attention clue rather than a binary occurrence question.
        return ""
    if subject.get("object_ambiguous"):
        return ""
    raw = safe_decision_text(subject.get("object") or "", max_len=160)
    safe_evidence = safe_decision_text(evidence or "", max_len=280)
    if event_type == "publish":
        raw_product, raw_environment, raw_target = _named_publish_dimensions(raw)
        evidence_product, evidence_environment, evidence_target = _named_publish_dimensions(
            safe_evidence
        )
        raw_product = _question_safe_publish_product(raw, raw_product)
        evidence_product = _question_safe_publish_product(
            safe_evidence,
            evidence_product,
        )
        if not raw_product:
            raw_environment = ""
            raw_target = ""
        if not evidence_product:
            evidence_environment = ""
            evidence_target = ""
        if raw_environment or evidence_environment:
            if (
                raw_product
                and evidence_product
                and (
                    _normalized_phrase(raw_product)
                    != _normalized_phrase(evidence_product)
                    or (
                        raw_environment
                        and evidence_environment
                        and raw_environment != evidence_environment
                    )
                    or (
                        raw_target
                        and evidence_target
                        and raw_target != evidence_target
                    )
                )
            ):
                return ""
            product = raw_product or evidence_product
            environment = raw_environment or evidence_environment
            target = raw_target or evidence_target
            if not product:
                return ""
            if environment and target:
                return f"{product} {environment} {target}"
            # A product/environment label identifies where a publish action
            # might have happened, not what was published.
            return ""
    candidate = _normalize_event_object_candidate(event_type, raw)
    if candidate:
        return candidate
    extracted_from_evidence = _extract_event_object(event_type, safe_evidence)
    return _normalize_event_object_candidate(event_type, extracted_from_evidence)


def _question_safe_publish_product(source: str, product: str) -> str:
    if not product or not _normalize_event_object_candidate("publish", source):
        return ""
    return _normalize_event_object_candidate("publish", product)


def _normalize_event_object_candidate(event_type: str, value: str) -> str:
    """Return a deterministic, question-safe object or an empty string.

    The confirmation surface must not turn OCR syntax, outline fragments or
    broad categories into statements about the real world.  This normalizer is
    intentionally local and deterministic so candidate creation, policy
    revalidation and V5 presentation all make the same decision.
    """
    if contains_absolute_path(value):
        return ""
    raw = safe_decision_text(value or "", max_len=240).strip()
    if not raw:
        return ""
    if _object_has_fragment_prefix(raw):
        return ""
    candidate = re.sub(
        r"^(?:zc\s*[:：]?|\d{1,2}[月/-]\d{1,2}[日号]?|\d{1,2}:\d{2}|"
        r"今天|今日|刚刚|刚才|我|本人)+\s*",
        "",
        raw,
        flags=re.I,
    )
    if _object_has_fragment_prefix(candidate):
        return ""
    if event_type == "completion":
        candidate = re.sub(
            r"^(?:(?:已|已经|刚刚)\s*)?(?:生成|制作|整理)(?:完成|成功)?"
            r"(?:了)?\s*(?:一份|一版|一个)?\s*",
            "",
            candidate,
            flags=re.I,
        )
        candidate = re.sub(
            r"\s*[、,]\s*可直接(?:提交|发送|发布|交付)的\s*",
            " ",
            candidate,
            flags=re.I,
        )
    candidate = re.sub(
        r"(?:已|已经|成功|真的|发生|完成|提交|发送|购买|上线|发布)+$",
        "",
        candidate,
        flags=re.I,
    ).strip(" ·•:：-_—&|()[]（）")
    candidate = re.sub(r"(?<=[A-Za-z0-9])\s*(?:也|的|了)$", "", candidate).strip()
    candidate = re.sub(r"\s+", " ", candidate)
    candidate = re.sub(
        r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])",
        "",
        candidate,
    )
    normalized = _normalized_phrase(candidate)
    ui_action_count = len(
        re.findall(
            r"(?:查看|打开|展开|关闭|设置|页面|菜单|点击|返回|"
            r"当页开始|格式刷|专享|母版)",
            candidate,
            re.I,
        )
    )
    digit_run_count = len(re.findall(r"(?:^|\s)\d{1,3}(?:\s|$)", candidate))
    outline_marker_count = sum(
        bool(re.search(pattern, candidate, re.I))
        for pattern in (
            r"(?:纪要|会议纪要)",
            r"(?:下一步|后续动作)",
            r"(?:项目管线|pipeline)",
            r"(?:交付物|deliverable)",
            r"(?:理想伙伴画像|伙伴画像)",
        )
    )
    numbered_outline = bool(
        re.search(r"(?:^|\s)\d{1,3}\s*[.)）、:：]", candidate)
        or re.search(r"(?:交付物|项目管线)\s*\d{1,3}", candidate, re.I)
    )
    menu_step_signal = bool(
        re.search(r"\bzc\s*[:：]", raw, re.I)
        or re.search(r"^(?:\d{1,3}\s+){2,}", candidate)
        or re.search(r"(?:第\s*\d+\s*步|步骤\s*\d+)", candidate)
        or ui_action_count >= 2
        or digit_run_count >= 2
        or (
            re.match(r"^(?:发布|完成|提交|发送|购买|安装|部署)\b", candidate, re.I)
            and ui_action_count > 0
        )
    )
    if (
        normalized in GENERIC_CONFIRMATION_OBJECT_IDENTITIES
        or ANAPHORIC_OBJECT_RE.fullmatch(candidate)
        or NEGATED_FRAGMENT_RE.fullmatch(candidate)
        or TRUNCATED_OBJECT_RE.search(candidate)
        or OCR_ARTIFACT_OBJECT_RE.search(candidate)
        or (
            event_type == "publish"
            and NAMED_ENVIRONMENT_ONLY_RE.fullmatch(candidate)
        )
        or (
            event_type == "publish"
            and re.search(
                r"(?:同意项|完成页).{0,16}(?:设置|查看|打开|展开)|"
                r"(?:查看|打开|展开).{0,16}(?:设置|菜单)",
                candidate,
                re.I,
            )
        )
        or len(candidate) > 56
        or menu_step_signal
        or (numbered_outline and outline_marker_count >= 2)
        or bool(re.search(r"(?:与|和|以及|并|及|或)$", candidate))
    ):
        return ""
    if event_type == "meeting" and not re.search(
        r"(?:会议|周会|例会|会谈|沟通会|复盘会|评审会|同步会|启动会|讨论会|"
        r"交流会|\bmeeting\b|\bmeet\b|\bstandup\b|\bsync\b)",
        candidate,
        re.I,
    ):
        return ""
    if event_type == "call" and not re.search(
        r"(?:通话|电话|连线|电话会|\bcall\b)",
        candidate,
        re.I,
    ):
        return ""
    return safe_decision_text(candidate, max_len=56) if len(normalized) >= 2 else ""


def _object_has_fragment_prefix(value: str) -> bool:
    return bool(
        re.match(
            r"^(?:[、，,；;:：。.!！?？·•|/\\]+|[-—_]{1,}|"
            r"(?:以及|并且|然后|接下来|随后)|"
            r"(?:和|与|并|或|及|且)(?=[、，,\s]|"
            # A conjunction followed by a verb is always the tail of a
            # sentence whose real object was cut off before it
            # ("…验收报告，并通过最终验收" → "并通过最终验收").  The
            # lookahead stays explicit so ordinary words that merely start
            # with one of these characters (和平, 及时, 或许) are untouched.
            r"(?:通过|完成|提交|发送|发布|确认|测试|部署|安装|购买|上线|"
            r"交付|验收|审核|批准|执行|处理|修复|更新|运行|生成)|"
            r"客户|项目|会议|纪要|下一步|报告|文档|方案|市场|业务|销售|"
            r"渠道|公司|产品))",
            value,
            re.I,
        )
        or re.match(
            r"^(?:(?:\d{1,3}|[一二三四五六七八九十]+)\s*[.)）、:：-]|"
            r"[（(]\s*\d{1,3}\s*[)）])",
            value,
            re.I,
        )
    )


def _organized_confirmation_subject(
    event_type: str,
    subject: dict[str, Any],
    *,
    evidence: str = "",
) -> dict[str, Any]:
    """Apply the shared deterministic presentation identity to a subject."""
    if event_type == "interview":
        return dict(subject)
    organized = dict(subject)
    raw_object = safe_decision_text(organized.get("object") or "", max_len=160)
    canonical_object = _canonical_event_object(event_type, organized, evidence)
    event_label = _event_label(event_type)
    organized.update(
        {
            "event_label": event_label,
            "object": canonical_object,
            "label": (
                f"{canonical_object}的{event_label}"
                if canonical_object
                else f"{event_label}对象未识别"
            ),
            "object_identified": bool(canonical_object),
            "object_ambiguous": bool(
                organized.get("object_ambiguous")
                or (raw_object and not canonical_object)
            ),
        }
    )
    return organized


def _named_publish_dimensions(value: str) -> tuple[str, str, str]:
    text = safe_decision_text(value or "", max_len=300)
    environment_match = re.search(
        r"\bstaging\b|\bprod(?:uction)?\b|生产环境|"
        r"\bdev(?:elopment)?\b|\btest\b|\bpreview\b|\bsandbox\b|\bbeta\b",
        text,
        re.I,
    )
    environment = ""
    if environment_match:
        environment_token = environment_match.group(0).casefold()
        environment = (
            "Staging"
            if environment_token == "staging"
            else "Production"
            if environment_token in {"prod", "production", "生产环境"}
            else environment_match.group(0).title()
        )
    target = ""
    target_start: int | None = None
    for pattern, label in (
        (r"(?:表单|\bform\b)", "表单"),
        (r"(?:文档|说明书|\bdocs?\b|\bdocument\b)", "文档"),
        (r"(?:网站|站点|\bweb(?:site)?\b|\bsite\b)", "网站"),
        (r"(?:接口|\bapi\b)", "API"),
        (r"(?:扩展|插件|\bextension\b|\bplugin\b)", "扩展"),
        (r"(?:应用|客户端|\bapp\b|\bclient\b)", "应用"),
    ):
        target_match = re.search(pattern, text, re.I)
        if target_match:
            target = label
            target_start = target_match.start()
            break
    boundary = (
        environment_match.start()
        if environment_match
        else target_start
    )
    product = ""
    if boundary is not None:
        product = re.sub(
            r"^(?:(?:今天|今日|刚刚|刚才)\s*)?"
            r"(?:(?:已|已经|刚刚)\s*)?(?:发布|上线|推送)(?:成功|了)?\s*",
            "",
            text[:boundary],
            flags=re.I,
        ).strip(" ·•:：,，;；-_—&|()[]（）")
        product = safe_decision_text(re.sub(r"\s+", " ", product), max_len=48)
        if (
            not _normalized_phrase(product)
            or re.fullmatch(
                r"(?:staging|prod(?:uction)?|dev(?:elopment)?|test|preview|sandbox|beta|"
                r"生产环境)",
                product,
                re.I,
            )
        ):
            product = ""
    return product, environment, target


def _member_revision(rows: list[dict[str, Any]]) -> str:
    members = [
        "|".join(
            (
                str(row.get("item_key") or ""),
                str(row.get("observed_start") or ""),
                str(row.get("observed_end") or ""),
                ",".join(
                    sorted(
                        _confirmation_row_ids(
                            row,
                            "source_activity_ids",
                            "_card_source_ids",
                        )
                    )
                ),
                ",".join(
                    sorted(
                        _confirmation_row_ids(
                            row,
                            "deletion_group_ids",
                            "_card_deletion_group_ids",
                        )
                    )
                ),
                str(row.get("group_signature") or ""),
            )
        )
        for row in rows
    ]
    return "rev_" + _hash("\n".join(sorted(members)))[:24]


def confirmation_completeness(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    view: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return only content-free confirmation funnel and settlement state."""
    return _confirmation_diagnostics_locked(conn, start, end, view=view)[
        "confirmation_completeness"
    ]


def _confirmation_diagnostics_locked(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    rows: list[Any] | None = None,
    groups: list[dict[str, Any]] | None = None,
    view: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_rows = (
        list(rows)
        if rows is not None
        else conn.execute(
            "SELECT * FROM activity_confirmation_items "
            "WHERE datetime(observed_start)<datetime(?) AND datetime(observed_end)>datetime(?) "
            "ORDER BY datetime(observed_start),item_key",
            (end, start),
        ).fetchall()
    )
    live_rows = [row for row in raw_rows if _confirmation_source_is_live(conn, row)]
    orphan_count = len(raw_rows) - len(live_rows)
    resolved_groups = groups if groups is not None else _question_groups(conn, live_rows)
    pending_groups = [
        group for group in resolved_groups if str(group.get("state") or "") in UNRESOLVED_STATES
    ]
    priority_counts = {priority: 0 for priority in ("high", "normal", "low")}
    pending_by_kind = {kind: 0 for kind in ("occurrence", "business_context", "outcome")}
    for group in pending_groups:
        priority = str(group.get("priority") or "normal")
        kind = str(group.get("question_kind") or "occurrence")
        priority_counts[priority if priority in priority_counts else "normal"] += 1
        pending_by_kind[kind if kind in pending_by_kind else "occurrence"] += 1

    filter_reason_counts: dict[str, int] = {}
    for row in live_rows:
        row_dict = dict(row)
        if str(row_dict.get("state") or "") != "filtered":
            continue
        for reason in _safe_tokens(row_dict.get("reason_codes")):
            filter_reason_counts[reason] = filter_reason_counts.get(reason, 0) + 1
    filtered_groups = [group for group in resolved_groups if str(group.get("state") or "") == "filtered"]
    confirmed_groups = [group for group in resolved_groups if str(group.get("state") or "") == "confirmed"]
    eligible_groups = [group for group in resolved_groups if str(group.get("state") or "") != "filtered"]
    report_date = _local_date_for_range(start)
    sampled_out_row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (f"activity_confirmation_sampled_out:{report_date}",),
    ).fetchone()
    try:
        sampled_out = max(0, int(sampled_out_row["value"] if sampled_out_row else 0))
    except (TypeError, ValueError):
        sampled_out = 0
    episode_diagnostics_row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (f"activity_confirmation_episode_diagnostics:{report_date}",),
    ).fetchone()
    try:
        episode_diagnostics = json.loads(
            str(episode_diagnostics_row["value"] if episode_diagnostics_row else "{}")
        )
    except (TypeError, json.JSONDecodeError):
        episode_diagnostics = {}
    if not isinstance(episode_diagnostics, dict):
        episode_diagnostics = {}
    episode_hard_filtered = max(0, int(episode_diagnostics.get("hard_filtered") or 0))
    episode_filter_reasons = episode_diagnostics.get("filter_reason_counts") or {}
    if isinstance(episode_filter_reasons, dict):
        for reason, count in episode_filter_reasons.items():
            safe_reason = safe_decision_text(reason, max_len=80)
            if safe_reason:
                filter_reason_counts[safe_reason] = (
                    filter_reason_counts.get(safe_reason, 0) + max(0, int(count or 0))
                )
    if view is None:
        view = confirmed_activity_view(conn, start, end, materialize=False)
    candidate_funnel = {
        "clear_activities": int(view.get("clear_activity_count") or 0),
        "event_signals": len(resolved_groups) + sampled_out + episode_hard_filtered,
        "eligible_questions": len(eligible_groups) + sampled_out,
        "hard_filtered": len(filtered_groups) + episode_hard_filtered,
        "pending": len(pending_groups),
        "confirmed": len(confirmed_groups),
        "reportable": int(view.get("reportable_activity_count") or 0),
        "sampled_out": sampled_out,
        "filter_reason_counts": dict(sorted(filter_reason_counts.items())),
    }
    fact_tokens = [
        "|".join(
            (
                str(group.get("group_key") or ""),
                str(group.get("member_revision") or ""),
                str(group.get("state") or ""),
                str(group.get("user_answer") or ""),
                str(group.get("candidate_origin") or ""),
                str(group.get("user_outcome") or ""),
                _hash(str(group.get("user_workstream") or ""))[:16],
            )
        )
        for group in resolved_groups
    ]
    # Bind the settlement revision to the exact safe fact projection consumed
    # by report-context/Hermes.  An answer may keep the same item/group ID while
    # changing its workstream, outcome, time range, or safe summary; those edits
    # must invalidate the previous settlement so a corrected report can be
    # emitted.  Only already-sanitized fields are included and the projection is
    # hashed below, so pending evidence/raw OCR never crosses this boundary.
    reportable_tokens = [
        json.dumps(
            {
                "id": str(
                    activity.get("id")
                    or activity.get("confirmation_group_key")
                    or ""
                ),
                "started_at": str(activity.get("started_at") or ""),
                "ended_at": str(activity.get("ended_at") or ""),
                "summary": safe_decision_text(
                    activity.get("activity_summary")
                    or activity.get("cleaned_text")
                    or "",
                    max_len=420,
                ),
                "apps": sorted(_json_list(activity.get("apps"))),
                "domains": sorted(_json_list(activity.get("domains"))),
                "event_type": str(activity.get("event_type") or ""),
                "outcome": _normalized_outcome(activity.get("outcome") or "unknown"),
                "workstream": safe_decision_text(
                    activity.get("workstream") or "",
                    max_len=120,
                ),
                "occurrence_state": str(activity.get("occurrence_state") or ""),
                "confirmation_source": str(activity.get("confirmation_source") or ""),
                "question_kind": str(activity.get("question_kind") or ""),
                "candidate_origin": str(activity.get("candidate_origin") or ""),
                "subject_scope_hint": str(
                    activity.get("confirmation_subject_scope_hint") or ""
                ),
                "candidate_type_hint": str(
                    activity.get("confirmation_candidate_type_hint") or ""
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for activity in list(view.get("activities") or [])
    ]
    revision_payload = {
        "facts": sorted(fact_tokens),
        "reportable": sorted(reportable_tokens),
        "pending_by_kind": pending_by_kind,
        "priority_counts": priority_counts,
        "funnel": {key: value for key, value in candidate_funnel.items() if key != "filter_reason_counts"},
        "orphans": orphan_count,
    }
    revision = "confirm_rev_" + _hash(
        json.dumps(revision_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )[:32]
    settlement = conn.execute(
        "SELECT revision,settled_at FROM activity_confirmation_settlements WHERE report_date=?",
        (report_date,),
    ).fetchone()
    settlement_matches = bool(settlement and str(settlement["revision"] or "") == revision)
    settled_at = str(settlement["settled_at"] or "") if settlement_matches else None
    if orphan_count:
        completeness_status = "partial"
    elif pending_groups:
        completeness_status = "pending"
    elif settlement_matches:
        completeness_status = "complete"
    else:
        # A read path never declares an unsettled or stale day complete. Only
        # the explicit settle writer can establish a matching snapshot.
        completeness_status = "partial"
    completeness_funnel = {
        key: candidate_funnel[key]
        for key in (
            "clear_activities",
            "event_signals",
            "eligible_questions",
            "hard_filtered",
            "pending",
            "confirmed",
            "reportable",
        )
    }
    completeness = {
        "status": completeness_status,
        "pending_total": len(pending_groups),
        "pending_high_priority": priority_counts["high"],
        "pending_by_kind": pending_by_kind,
        "candidate_funnel": completeness_funnel,
        "settled_at": settled_at,
        "revision": revision,
    }
    return {
        "orphan_count": orphan_count,
        "priority_counts": priority_counts,
        "pending_by_kind": pending_by_kind,
        "candidate_funnel": candidate_funnel,
        "revision": revision,
        "confirmation_completeness": completeness,
    }


def _local_date_for_range(start: str) -> str:
    try:
        return _local_datetime(start).date().isoformat()
    except ValueError:
        return str(start or "")[:10]


def answer_activity_confirmation(
    conn: sqlite3.Connection,
    *,
    item_key: str,
    answer: str,
    reason: str = "other",
    note: str = "",
    workstream: str = "",
    outcome: str = "",
    generalize_scope: str = "exact",
) -> dict[str, Any]:
    # The lease is taken before the transaction opens, so a timeout here cannot
    # leave a half-written answer behind.
    with document_authorization_lock(
        conn,
        exclusive=False,
        timeout=INTERACTIVE_WRITE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS,
    ):
        started_transaction = not conn.in_transaction
        try:
            if started_transaction:
                conn.execute("BEGIN IMMEDIATE")
            result = _answer_activity_confirmation_locked(
                conn,
                item_key=item_key,
                answer=answer,
                reason=reason,
                note=note,
                workstream=workstream,
                outcome=outcome,
                generalize_scope=generalize_scope,
            )
            if started_transaction and conn.in_transaction:
                conn.commit()
                _purge_evidence_after_commit(conn)
            return result
        except Exception:
            if started_transaction and conn.in_transaction:
                conn.rollback()
            raise


def _answer_activity_confirmation_locked(
    conn: sqlite3.Connection,
    *,
    item_key: str,
    answer: str,
    reason: str = "other",
    note: str = "",
    workstream: str = "",
    outcome: str = "",
    generalize_scope: str = "exact",
) -> dict[str, Any]:
    answer = str(answer or "").strip()
    reason = str(reason or "other").strip()
    generalize_scope = str(generalize_scope or "exact").strip()
    if answer not in ANSWERS:
        raise ValueError("unsupported confirmation answer")
    if answer == "ignored":
        reason = "user_ignored"
        generalize_scope = "exact"
        note = ""
    if reason not in REASONS:
        raise ValueError("unsupported confirmation reason")
    if generalize_scope not in GENERALIZE_SCOPES:
        raise ValueError("unsupported generalize scope")
    row = conn.execute("SELECT * FROM activity_confirmation_items WHERE item_key=?", (item_key,)).fetchone()
    if not row:
        return {"status": "blocked", "error": "item_missing", "raw_access": False}
    if not _confirmation_source_is_live(conn, row):
        if str(row["state"] or "") in UNRESOLVED_STATES:
            schedule_evidence_release(conn, [item_key], reason="source_missing")
            conn.execute("DELETE FROM activity_confirmation_items WHERE item_key=?", (item_key,))
        return {
            "status": "blocked",
            "error": "source_missing",
            **_safe_action_flags(),
        }
    previous = dict(row)
    if answer in {"occurred", "ignored", "defer"}:
        generalize_scope = "exact"
    user_workstream, user_outcome = _answer_details(answer, workstream, outcome)
    _validate_preparation_answer(
        answer,
        reason,
        user_outcome,
        candidate_origin=str(row["candidate_origin"] or ""),
        event_type=str(row["event_type"] or ""),
    )
    if previous.get("answer"):
        if _stored_answer_matches(
            previous,
            answer=answer,
            reason=reason,
            workstream=user_workstream,
            outcome=user_outcome,
            generalize_scope=generalize_scope,
            note=note,
        ):
            return {
                "status": "recorded",
                "unchanged": True,
                "item": _public_item(row, conn=conn),
                **_safe_action_flags(),
            }
        return {
            "status": "blocked",
            "error": "item_already_resolved",
            "item": _public_item(row, conn=conn),
            **_safe_action_flags(),
        }
    if not _confirmation_rows_are_answerable([previous]):
        return {
            "status": "blocked",
            "error": "information_insufficient",
            **_safe_action_flags(),
        }
    state = {
        "occurred": "confirmed",
        "not_occurred": "rejected",
        "misrecognized": "rejected",
        "ignored": "ignored",
        "defer": "deferred",
    }[answer]
    now = _utc_now()
    conn.execute(
        """
        UPDATE activity_confirmation_items
        SET state=?,answer=?,answer_reason=?,user_note='',
            user_workstream=?,user_outcome=?,notification_eligible=0,
            generalize_scope=?,answered_at=?,updated_at=?,
            clarification_note=CASE
                WHEN ?='defer' AND ?<>'' THEN ?
                WHEN ?='defer' THEN clarification_note
                ELSE ''
            END
        WHERE item_key=?
        """,
        (
            state, answer, reason, user_workstream, user_outcome, generalize_scope, now, now,
            answer, safe_decision_text(note, max_len=240), safe_decision_text(note, max_len=240),
            answer, item_key,
        ),
    )
    if answer in {"occurred", "not_occurred", "misrecognized"}:
        _update_confirmation_calibration(
            conn,
            previous,
            answer=answer,
            reason=reason,
            generalize_scope=generalize_scope,
            now=now,
        )
    if answer != "defer":
        schedule_evidence_release(conn, [item_key], reason=f"answer_{answer}")
    updated = conn.execute("SELECT * FROM activity_confirmation_items WHERE item_key=?", (item_key,)).fetchone()
    return {
        "status": "recorded",
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "item": _public_item(updated, conn=conn),
    }


def answer_activity_confirmation_group(
    conn: sqlite3.Connection,
    *,
    group_key: str,
    member_revision: str,
    answer: str,
    reason: str = "other",
    note: str = "",
    workstream: str = "",
    outcome: str = "",
    generalize_scope: str = "exact",
) -> dict[str, Any]:
    with document_authorization_lock(
        conn,
        exclusive=False,
        timeout=INTERACTIVE_WRITE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS,
    ):
        return _answer_activity_confirmation_group_locked(
            conn,
            group_key=group_key,
            member_revision=member_revision,
            answer=answer,
            reason=reason,
            note=note,
            workstream=workstream,
            outcome=outcome,
            generalize_scope=generalize_scope,
        )


def _answer_activity_confirmation_group_locked(
    conn: sqlite3.Connection,
    *,
    group_key: str,
    member_revision: str,
    answer: str,
    reason: str = "other",
    note: str = "",
    workstream: str = "",
    outcome: str = "",
    generalize_scope: str = "exact",
    allow_context_reanswer: bool = False,
) -> dict[str, Any]:
    answer = str(answer or "").strip()
    reason = str(reason or "other").strip()
    generalize_scope = str(generalize_scope or "exact").strip()
    if answer not in ANSWERS:
        raise ValueError("unsupported confirmation answer")
    if answer == "ignored":
        reason = "user_ignored"
        generalize_scope = "exact"
        note = ""
    if reason not in REASONS:
        raise ValueError("unsupported confirmation reason")
    if generalize_scope not in GENERALIZE_SCOPES:
        raise ValueError("unsupported generalize scope")
    if answer in {"occurred", "ignored", "defer"}:
        generalize_scope = "exact"
    user_workstream, user_outcome = _answer_details(answer, workstream, outcome)
    # Manual entries are already terminal and cannot be answered through the
    # group endpoint.  Consequently an occurred + preparation_only group
    # answer is always malformed here; the sole special invariant is enforced
    # explicitly inside manual_add_confirmed_activity below.
    _validate_preparation_answer(answer, reason, user_outcome)
    if answer not in {"ignored", "defer"} and not user_workstream:
        raise ValueError("v4 group answer requires workstream")
    flags = _safe_action_flags()
    started_transaction = not conn.in_transaction
    savepoint = "answer_confirmation_group"
    try:
        if started_transaction:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute(f"SAVEPOINT {savepoint}")
        initial_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM activity_confirmation_items WHERE group_key=? "
                "ORDER BY datetime(observed_start),item_key",
                (group_key,),
            ).fetchall()
        ]
        if not initial_rows:
            _finish_confirmation_transaction(conn, started_transaction, savepoint)
            return {"status": "blocked", "error": "group_missing", **flags}
        context_link = None
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='context_activity_confirmation_links'"
        ).fetchone():
            context_link = conn.execute(
                "SELECT answer,status FROM context_activity_confirmation_links "
                "WHERE confirmation_key=? AND status IN ('verified','non_verifying') "
                "ORDER BY created_at LIMIT 1",
                (group_key,),
            ).fetchone()
        if context_link and not allow_context_reanswer:
            linked_answer = str(context_link["answer"] or "")
            all_terminal = all(
                str(row.get("state") or "") not in UNRESOLVED_STATES
                for row in initial_rows
            )
            stored_answers = {
                str(row.get("answer") or "")
                for row in initial_rows
                if str(row.get("answer") or "")
            }
            if not (
                all_terminal
                and answer == linked_answer
                and stored_answers == {linked_answer}
            ):
                _finish_confirmation_transaction(
                    conn,
                    started_transaction,
                    savepoint,
                )
                return {
                    "status": "blocked",
                    "error": "context_confirmation_linked_group_immutable",
                    "group_key": group_key,
                    "member_revision": _member_revision(initial_rows),
                    **flags,
                }
        day_start, day_end = _group_day_window(initial_rows)
        _regroup_confirmation_items(
            conn,
            start=day_start,
            end=day_end,
            unresolved_only=True,
        )
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM activity_confirmation_items WHERE group_key=? "
                "ORDER BY datetime(observed_start),item_key",
                (group_key,),
            ).fetchall()
        ]
        if not rows:
            initial_keys = [str(row["item_key"]) for row in initial_rows]
            placeholders = ",".join("?" for _ in initial_keys)
            regrouped = [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM activity_confirmation_items WHERE item_key IN ({placeholders})",
                    tuple(initial_keys),
                ).fetchall()
            ]
            refreshed_group_key = str(regrouped[0].get("group_key") or "") if regrouped else ""
            refreshed_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM activity_confirmation_items WHERE group_key=? "
                    "ORDER BY datetime(observed_start),item_key",
                    (refreshed_group_key,),
                ).fetchall()
            ] if refreshed_group_key else []
            refreshed_group = (
                _public_question_group(refreshed_group_key, refreshed_rows, conn=conn)
                if refreshed_rows
                else {}
            )
            _finish_confirmation_transaction(conn, started_transaction, savepoint)
            return {
                "status": "stale",
                "error": "group_regrouped",
                "group_key": refreshed_group_key,
                "member_revision": str(refreshed_group.get("member_revision") or ""),
                "question_group": refreshed_group,
                **flags,
            }
        actual_revision = _member_revision(rows)
        if str(member_revision or "") != actual_revision:
            group = _public_question_group(group_key, rows, conn=conn)
            _finish_confirmation_transaction(conn, started_transaction, savepoint)
            return {
                "status": "stale",
                "error": "group_members_changed",
                "group_key": group_key,
                "expected_member_revision": str(member_revision or ""),
                "member_revision": actual_revision,
                "question_group": group,
                **flags,
            }
        # During the one-version compatibility window, the legacy single-item
        # command may already have answered one member of a v2 group.  Keep a
        # fully resolved group immutable, but let an explicitly submitted v2
        # group answer atomically replace a mixed legacy/pending state.
        if (
            all(str(row.get("state") or "") not in UNRESOLVED_STATES for row in rows)
            and not allow_context_reanswer
        ):
            group = _public_question_group(group_key, rows, conn=conn)
            if all(
                _stored_answer_matches(
                    row,
                    answer=answer,
                    reason=reason,
                    workstream=user_workstream,
                    outcome=user_outcome,
                    generalize_scope=generalize_scope,
                    note=note,
                )
                for row in rows
            ):
                _finish_confirmation_transaction(conn, started_transaction, savepoint)
                return {
                    "status": "recorded",
                    "unchanged": True,
                    "group_key": group_key,
                    "member_revision": actual_revision,
                    "question_group": group,
                    **flags,
                }
            _finish_confirmation_transaction(conn, started_transaction, savepoint)
            return {
                "status": "blocked",
                "error": "group_already_resolved",
                "group_key": group_key,
                "member_revision": actual_revision,
                "question_group": group,
                **flags,
            }
        answer_rows = (
            rows
            if allow_context_reanswer
            else [
                row
                for row in rows
                if str(row.get("state") or "") in UNRESOLVED_STATES
            ]
        )
        if not _confirmation_rows_are_answerable(answer_rows):
            _finish_confirmation_transaction(conn, started_transaction, savepoint)
            return {
                "status": "blocked",
                "error": "information_insufficient",
                "group_key": group_key,
                "member_revision": actual_revision,
                **flags,
            }
        state = {
            "occurred": "confirmed",
            "not_occurred": "rejected",
            "misrecognized": "rejected",
            "ignored": "ignored",
            "defer": "deferred",
        }[answer]
        now = _utc_now()
        safe_note = safe_decision_text(note, max_len=240)
        placeholders = ",".join("?" for _ in rows)
        conn.execute(
            f"""
            UPDATE activity_confirmation_items
            SET state=?,answer=?,answer_reason=?,user_note='',
                user_workstream=?,user_outcome=?,notification_eligible=0,
                generalize_scope=?,answered_at=?,updated_at=?,
                clarification_note=CASE
                    WHEN ?='defer' AND ?<>'' THEN ?
                    WHEN ?='defer' THEN clarification_note
                    ELSE ''
                END
            WHERE item_key IN ({placeholders})
            """,
            (
                state,
                answer,
                reason,
                user_workstream,
                user_outcome,
                generalize_scope,
                now,
                now,
                answer,
                safe_note,
                safe_note,
                answer,
                *(str(row["item_key"]) for row in rows),
            ),
        )
        calibration_members: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            calibration_key = str(row.get("calibration_key") or "")
            if calibration_key:
                calibration_members.setdefault(calibration_key, []).append(row)
        if (
            answer in {"occurred", "not_occurred", "misrecognized"}
            or allow_context_reanswer
        ):
            for members in calibration_members.values():
                representative = members[0]
                _update_confirmation_calibration(
                    conn,
                    representative,
                    answer=answer,
                    reason=reason,
                    generalize_scope=generalize_scope,
                    now=now,
                    previous_items=members,
                )
        if answer != "defer":
            schedule_evidence_release(
                conn,
                [str(row["item_key"]) for row in rows],
                reason=f"answer_{answer}",
            )
        updated_rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM activity_confirmation_items WHERE item_key IN ({placeholders}) "
                "ORDER BY datetime(observed_start),item_key",
                tuple(str(row["item_key"]) for row in rows),
            ).fetchall()
        ]
        result_group = _public_question_group(group_key, updated_rows, conn=conn)
        _finish_confirmation_transaction(conn, started_transaction, savepoint)
        if started_transaction:
            _purge_evidence_after_commit(conn)
        return {
            "status": "recorded",
            "group_key": group_key,
            "member_revision": result_group["member_revision"],
            "question_group": result_group,
            **flags,
        }
    except Exception:
        _rollback_confirmation_transaction(conn, started_transaction, savepoint)
        raise


def _finish_confirmation_transaction(
    conn: sqlite3.Connection,
    started_transaction: bool,
    savepoint: str,
) -> None:
    if started_transaction:
        conn.commit()
    else:
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")


def _purge_evidence_after_commit(conn: sqlite3.Connection) -> dict[str, int]:
    """Best-effort physical deletion; DB remains delete_pending on failure."""
    try:
        return purge_scheduled_evidence(conn)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        try:
            conn.execute(
                "UPDATE activity_confirmation_evidence_files "
                "SET last_error='delete_retry_pending',updated_at=? "
                "WHERE state='delete_pending'",
                (_utc_now(),),
            )
            conn.commit()
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
        return {"deleted": 0, "failed": 1, "bytes_deleted": 0}


def _rollback_confirmation_transaction(
    conn: sqlite3.Connection,
    started_transaction: bool,
    savepoint: str,
) -> None:
    if started_transaction:
        conn.rollback()
    else:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")


def clarify_activity_confirmation_group(
    conn: sqlite3.Connection,
    *,
    group_key: str,
    member_revision: str,
    note: str = "",
) -> dict[str, Any]:
    with document_authorization_lock(
        conn,
        exclusive=False,
        timeout=INTERACTIVE_WRITE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS,
    ):
        snapshot = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM activity_confirmation_items WHERE group_key=? "
                "ORDER BY datetime(observed_start),item_key",
                (group_key,),
            ).fetchall()
        ]
        supplied_note = safe_decision_text(note, max_len=360)
        previous_notes = [
            (
                str(row.get("updated_at") or ""),
                str(row.get("item_key") or ""),
                safe_decision_text(row.get("clarification_note") or "", max_len=360),
            )
            for row in snapshot
            if safe_decision_text(row.get("clarification_note") or "", max_len=360)
        ]
        # A regroup can combine notes from members observed in a different
        # order than they were clarified.  Reuse the most recently updated
        # local note, with item_key only as a deterministic tie-breaker; the
        # note remains private and is never added to the public group payload.
        effective_note = supplied_note or (
            max(previous_notes, key=lambda item: (item[0], item[1]))[2]
            if previous_notes
            else ""
        )
        item_keys = [str(row.get("item_key") or "") for row in snapshot]
        snapshot_by_key = {
            str(row.get("item_key") or ""): row
            for row in snapshot
            if str(row.get("item_key") or "")
        }
        ocr_safe_parts_by_item: dict[str, list[str]] = {}
        ocr_retry_evidence_ids: list[str] = []
        ocr_success_evidence_ids: list[str] = []
        privacy_reclassified_evidence_ids: list[str] = []
        with held_evidence_paths(conn, item_keys) as (records, hold_summary, hold_issues):
            if records:
                from .macos_probe import build_ocr_event_from_confirmation_image
                from .privacy import classify_event

                ocr_root = database_root(conn)

                for record in records:
                    path = record["path"]
                    evidence_id = str(record.get("evidence_id") or "")
                    record_item_keys = [
                        key
                        for key in record.get("item_keys", [])
                        if key in snapshot_by_key
                    ]
                    row = snapshot_by_key.get(record_item_keys[0], {}) if record_item_keys else {}
                    base_event = {
                        "captured_at": str(row.get("observed_start") or _utc_now()),
                        "display_id": "confirmation-evidence",
                        "app_name": str(row.get("app_family") or "Unknown"),
                        "bundle_id": "",
                        "window_title": "",
                        "url": "",
                        "domain": "",
                        "context_confidence": 0.65,
                        "url_source": "confirmation_evidence",
                        "context_source": "confirmation_evidence_apple_vision",
                        "screen_region_signal": {
                            "region_kind": str(row.get("region_kind") or "unknown"),
                            "actionability": str(row.get("actionability") or "unknown"),
                            "reason_codes": _json_list(row.get("reason_codes")),
                        },
                    }
                    try:
                        reviewed = classify_event(
                            build_ocr_event_from_confirmation_image(
                                path,
                                base_event,
                                root_dir=ocr_root,
                            )
                        )
                    except Exception:
                        if evidence_id:
                            ocr_retry_evidence_ids.append(evidence_id)
                        continue
                    if reviewed.status != "clear":
                        if evidence_id:
                            privacy_reclassified_evidence_ids.append(evidence_id)
                        continue
                    if evidence_id:
                        ocr_success_evidence_ids.append(evidence_id)
                    record_safe_parts: list[str] = []
                    for value in (
                        reviewed.ocr_redacted_summary,
                        reviewed.activity_summary,
                        reviewed.window_title_redacted,
                    ):
                        safe_value = safe_decision_text(value or "", max_len=900)
                        if safe_value and safe_value not in record_safe_parts:
                            record_safe_parts.append(safe_value)
                    for item_key in record_item_keys:
                        target = ocr_safe_parts_by_item.setdefault(item_key, [])
                        for safe_value in record_safe_parts:
                            if safe_value not in target:
                                target.append(safe_value)
        invalid_evidence_ids = [
            str(value or "")
            for value in hold_issues.get("invalid_evidence_ids", [])
            if str(value or "")
        ]
        excluded_ids = set(privacy_reclassified_evidence_ids) | set(invalid_evidence_ids)
        retained_records = [
            record
            for record in records
            if str(record.get("evidence_id") or "") not in excluded_ids
        ]
        retained_item_keys = {
            key
            for record in retained_records
            for key in record.get("item_keys", [])
            if key in snapshot_by_key
        }
        retained_evidence_ids = {
            str(record.get("evidence_id") or "")
            for record in retained_records
            if str(record.get("evidence_id") or "")
        }
        if item_keys and len(retained_item_keys) == len(set(item_keys)):
            retained_status = "available"
        elif retained_item_keys:
            retained_status = "partial"
        else:
            retained_status = "unavailable"
        evidence_context = {
            **dict(hold_summary),
            "evidence_status": retained_status,
            "evidence_count": len(retained_evidence_ids),
            "available_source_count": len(retained_item_keys),
        }
        return _clarify_activity_confirmation_group_locked(
            conn,
            group_key=group_key,
            member_revision=member_revision,
            note=effective_note,
            note_was_supplied=bool(supplied_note),
            ocr_safe_parts_by_item=ocr_safe_parts_by_item,
            evidence_context=evidence_context,
            ocr_retry_evidence_ids=ocr_retry_evidence_ids,
            ocr_success_evidence_ids=ocr_success_evidence_ids,
            privacy_reclassified_evidence_ids=privacy_reclassified_evidence_ids,
            invalid_evidence_ids=invalid_evidence_ids,
        )


def _clarify_activity_confirmation_group_locked(
    conn: sqlite3.Connection,
    *,
    group_key: str,
    member_revision: str,
    note: str = "",
    note_was_supplied: bool = False,
    ocr_safe_parts_by_item: dict[str, list[str]] | None = None,
    evidence_context: dict[str, Any] | None = None,
    ocr_retry_evidence_ids: list[str] | None = None,
    ocr_success_evidence_ids: list[str] | None = None,
    privacy_reclassified_evidence_ids: list[str] | None = None,
    invalid_evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Re-evaluate one question after local OCR has completed outside the write transaction."""
    flags = _safe_action_flags()
    safe_note = safe_decision_text(note, max_len=360)
    ocr_safe_parts_by_item = {
        str(item_key): _unique(
            [safe_decision_text(value, max_len=900) for value in values if value]
        )
        for item_key, values in (ocr_safe_parts_by_item or {}).items()
        if str(item_key)
    }
    evidence_context = {
        "evidence_status": str((evidence_context or {}).get("evidence_status") or "unavailable"),
        "evidence_count": max(0, int((evidence_context or {}).get("evidence_count") or 0)),
        "available_source_count": max(
            0,
            int((evidence_context or {}).get("available_source_count") or 0),
        ),
    }
    ocr_retry_evidence_ids = _private_evidence_ids(ocr_retry_evidence_ids)
    ocr_success_evidence_ids = _private_evidence_ids(ocr_success_evidence_ids)
    privacy_reclassified_evidence_ids = _private_evidence_ids(
        privacy_reclassified_evidence_ids
    )
    invalid_evidence_ids = _private_evidence_ids(invalid_evidence_ids)
    started_transaction = not conn.in_transaction
    savepoint = "clarify_confirmation_group"
    evidence_release_scheduled = False
    try:
        if started_transaction:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute(f"SAVEPOINT {savepoint}")
        release_evidence_ids = _unique(
            [*privacy_reclassified_evidence_ids, *invalid_evidence_ids]
        )
        retry_evidence_ids = [
            evidence_id
            for evidence_id in ocr_retry_evidence_ids
            if evidence_id not in release_evidence_ids
        ]
        successful_evidence_ids = [
            evidence_id
            for evidence_id in ocr_success_evidence_ids
            if evidence_id not in release_evidence_ids
            and evidence_id not in retry_evidence_ids
        ]
        if successful_evidence_ids:
            success_placeholders = ",".join("?" for _ in successful_evidence_ids)
            conn.execute(
                "UPDATE activity_confirmation_evidence_files "
                "SET last_error='',updated_at=? "
                f"WHERE evidence_id IN ({success_placeholders}) AND state='available'",
                (_utc_now(), *successful_evidence_ids),
            )
        if retry_evidence_ids:
            retry_placeholders = ",".join("?" for _ in retry_evidence_ids)
            conn.execute(
                "UPDATE activity_confirmation_evidence_files "
                "SET last_error='ocr_retry_pending',updated_at=? "
                f"WHERE evidence_id IN ({retry_placeholders}) AND state='available'",
                (_utc_now(), *retry_evidence_ids),
            )
        if privacy_reclassified_evidence_ids:
            evidence_release_scheduled = bool(
                schedule_evidence_file_release(
                    conn,
                    privacy_reclassified_evidence_ids,
                    reason="privacy_reclassified",
                )
            )
        invalid_only_ids = [
            evidence_id
            for evidence_id in invalid_evidence_ids
            if evidence_id not in privacy_reclassified_evidence_ids
        ]
        if invalid_only_ids:
            evidence_release_scheduled = bool(
                schedule_evidence_file_release(
                    conn,
                    invalid_only_ids,
                    reason="evidence_invalid",
                )
            ) or evidence_release_scheduled
        original_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM activity_confirmation_items WHERE group_key=? "
                "ORDER BY datetime(observed_start),item_key",
                (group_key,),
            ).fetchall()
        ]
        if not original_rows:
            _finish_confirmation_transaction(conn, started_transaction, savepoint)
            if started_transaction and evidence_release_scheduled:
                _purge_evidence_after_commit(conn)
            return {"status": "blocked", "error": "group_missing", **flags}
        actual_revision = _member_revision(original_rows)
        if str(member_revision or "") != actual_revision:
            group = _public_question_group(group_key, original_rows, conn=conn)
            _finish_confirmation_transaction(conn, started_transaction, savepoint)
            if started_transaction and evidence_release_scheduled:
                _purge_evidence_after_commit(conn)
            return {
                "status": "stale",
                "error": "group_members_changed",
                "group_key": group_key,
                "expected_member_revision": str(member_revision or ""),
                "member_revision": actual_revision,
                "question_group": group,
                **flags,
            }
        has_unresolved_member = any(
            str(row.get("state") or "") in UNRESOLVED_STATES for row in original_rows
        )
        if not has_unresolved_member:
            group = _public_question_group(group_key, original_rows, conn=conn)
            _finish_confirmation_transaction(conn, started_transaction, savepoint)
            if started_transaction and evidence_release_scheduled:
                _purge_evidence_after_commit(conn)
            return {
                "status": "blocked",
                "error": "group_already_resolved",
                "group_key": group_key,
                "member_revision": actual_revision,
                "question_group": group,
                **flags,
            }
        mixed_legacy_group = any(
            str(row.get("state") or "") not in UNRESOLVED_STATES for row in original_rows
        )
        original_item_keys = [str(row["item_key"]) for row in original_rows]
        unresolved_rows = [
            row
            for row in original_rows
            if str(row.get("state") or "") in UNRESOLVED_STATES
        ]
        unresolved_item_keys = [str(row["item_key"]) for row in unresolved_rows]
        event_type = str(original_rows[0].get("event_type") or "")
        event_label = _event_label(event_type)
        inputs_used = []
        if any(
            safe_decision_text(
                row.get("cleaned_content") or row.get("safe_observation") or "",
                max_len=1600,
            )
            for row in unresolved_rows
        ):
            inputs_used.append("stored_cleaned_text")
        if any(ocr_safe_parts_by_item.values()):
            inputs_used.append("retained_frame_ocr")
        if safe_note:
            inputs_used.append("user_note")

        # A local Vision/OCR exception is retryable. Do not let the older
        # cleaned text turn that failed reinspection into a terminal system
        # filter, because the user explicitly asked us to read the retained
        # source again and the source is still available.
        if retry_evidence_ids:
            generated_at = _utc_now()
            retry_count = len(set(retry_evidence_ids))
            result = {
                "outcome": "insufficient_information",
                "severity": "warning",
                "message": (
                    f"有 {retry_count} 张原截图仍保留，但本次本机 OCR 未成功；"
                    "没有据此改成发生或未发生，请稍后重新梳理。"
                ),
                "missing_fields": [],
                "subject_conflicts": [],
                "inputs_used": _unique(inputs_used),
                **evidence_context,
                "question_groups": [],
                "generated_at": generated_at,
            }
            clarification = {"status": "completed", **result}
            retry_placeholders = ",".join("?" for _ in unresolved_item_keys)
            conn.execute(
                "UPDATE activity_confirmation_items SET clarification=?,"
                "clarification_note=CASE WHEN ?=1 THEN ? ELSE clarification_note END "
                f"WHERE item_key IN ({retry_placeholders}) "
                "AND state IN ('pending','deferred')",
                (
                    json.dumps(clarification, ensure_ascii=False, sort_keys=True),
                    1 if note_was_supplied else 0,
                    safe_note,
                    *unresolved_item_keys,
                ),
            )
            refreshed_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM activity_confirmation_items WHERE group_key=? "
                    "ORDER BY datetime(observed_start),item_key",
                    (group_key,),
                ).fetchall()
            ]
            refreshed_group = _public_question_group(
                group_key,
                refreshed_rows,
                conn=conn,
            )
            result["question_groups"] = [refreshed_group]
            _finish_confirmation_transaction(conn, started_transaction, savepoint)
            if started_transaction and evidence_release_scheduled:
                _purge_evidence_after_commit(conn)
            return {
                "status": "clarified",
                "group_key": group_key,
                "member_revision": actual_revision,
                "result": result,
                **flags,
            }

        filter_reasons_by_item: dict[str, list[str]] = {}
        for row in unresolved_rows:
            stored_content = safe_decision_text(
                row.get("cleaned_content") or row.get("safe_observation") or "",
                max_len=1600,
            )
            review_parts = [
                stored_content,
                safe_decision_text(row.get("evidence") or "", max_len=280),
                *ocr_safe_parts_by_item.get(str(row["item_key"]), []),
            ]
            match = next(
                (candidate for candidate in _event_matches(review_parts) if candidate[0] == event_type),
                None,
            )
            reasons: list[str] = []
            if match is None:
                reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_trigger_missing")
                evidence = safe_decision_text(row.get("evidence") or "", max_len=280)
            else:
                evidence = safe_decision_text(match[2], max_len=280)
            review_text = " ".join(_unique([part for part in review_parts if part]))
            subject = _subject_for_event(event_type, event_label, evidence, review_text)
            reasons.extend(
                _confirmation_policy_filter_reasons(
                    event_type=event_type,
                    app_family=str(row.get("app_family") or "unknown"),
                    region_kind=str(row.get("region_kind") or "unknown"),
                    actionability=str(row.get("actionability") or "unknown"),
                    evidence=evidence,
                    full_text=review_text,
                    subject=subject,
                    context_note=safe_note,
                )
            )
            filter_reasons_by_item[str(row["item_key"])] = _unique(reasons)

        filtered_item_keys = [
            str(row["item_key"])
            for row in unresolved_rows
            if filter_reasons_by_item.get(str(row["item_key"]))
        ]
        partially_filtered_count = 0
        if filtered_item_keys:
            generated_at = _utc_now()
            filtered_used_ocr = any(
                ocr_safe_parts_by_item.get(item_key)
                for item_key in filtered_item_keys
            )
            if filtered_used_ocr:
                message = (
                    "已重新读取原截图并确认这不是可记录的已发生事项；已移出待确认，"
                    "不记录发生或未发生，也不用于系统校准。"
                )
            elif evidence_context["evidence_status"] in {"available", "partial"}:
                message = (
                    "已读取仍可用的原截图，但没有取得可用的新文字；根据已保存的清洗 OCR，"
                    "这不是可记录的已发生事项。已移出待确认，不记录发生或未发生，也不用于系统校准。"
                )
            else:
                message = (
                    "原截图按旧规则未保留；已依据保存的清洗 OCR 确认这不是可记录的已发生事项，"
                    "已移出待确认，不记录发生或未发生，也不用于系统校准。"
                )
            result = {
                "outcome": "system_filtered",
                "severity": "success",
                "message": message,
                "missing_fields": [],
                "subject_conflicts": [],
                "inputs_used": inputs_used,
                **evidence_context,
                "question_groups": [],
                "generated_at": generated_at,
            }
            clarification = {"status": "completed", **result}
            for row in unresolved_rows:
                if str(row["item_key"]) not in filtered_item_keys:
                    continue
                reasons = _unique(
                    [
                        *_json_list(row.get("reason_codes")),
                        *filter_reasons_by_item[str(row["item_key"])],
                    ]
                )
                conn.execute(
                    "UPDATE activity_confirmation_items SET state='filtered',answer=NULL,"
                    "answer_reason=NULL,user_note='',clarification_note='',clarification=?,"
                    "reason_codes=?,answered_at=NULL,updated_at=? WHERE item_key=? "
                    "AND state IN ('pending','deferred')",
                    (
                        json.dumps(clarification, ensure_ascii=False, sort_keys=True),
                        json.dumps(reasons, ensure_ascii=False),
                        generated_at,
                        str(row["item_key"]),
                    ),
                )
            evidence_release_scheduled = bool(
                schedule_evidence_release(
                    conn,
                    filtered_item_keys,
                    reason="system_filtered",
                )
            ) or evidence_release_scheduled
            if len(filtered_item_keys) == len(unresolved_rows):
                _finish_confirmation_transaction(conn, started_transaction, savepoint)
                if started_transaction:
                    _purge_evidence_after_commit(conn)
                return {
                    "status": "clarified",
                    "group_key": group_key,
                    "member_revision": actual_revision,
                    "result": result,
                    **flags,
                }
            partially_filtered_count = len(filtered_item_keys)
            filtered_set = set(filtered_item_keys)
            original_rows = [
                row for row in original_rows
                if str(row["item_key"]) not in filtered_set
            ]
            original_item_keys = [str(row["item_key"]) for row in original_rows]
            unresolved_rows = [
                row
                for row in original_rows
                if str(row.get("state") or "") in UNRESOLVED_STATES
            ]
            unresolved_item_keys = [str(row["item_key"]) for row in unresolved_rows]
            mixed_legacy_group = any(
                str(row.get("state") or "") not in UNRESOLVED_STATES
                for row in original_rows
            )

        def derive_subject(
            row: dict[str, Any],
            guidance: str = "",
        ) -> tuple[dict[str, Any], str]:
            item_key = str(row["item_key"])
            stored_content = safe_decision_text(
                row.get("cleaned_content") or row.get("safe_observation") or "",
                max_len=1600,
            )
            stored_evidence = safe_decision_text(row.get("evidence") or "", max_len=280)
            source_candidates: list[tuple[int, int, str]] = []
            for index, ocr_text in enumerate(ocr_safe_parts_by_item.get(item_key, [])):
                if ocr_text:
                    source_candidates.append((2, index, ocr_text))
            if stored_content:
                source_candidates.append((1, 0, stored_content))
            if stored_evidence and stored_evidence != stored_content:
                source_candidates.append((0, 0, stored_evidence))

            options: list[tuple[int, int, int, int, dict[str, Any], str]] = []
            for source_priority, source_index, source_text in source_candidates:
                # Each source is matched and interpreted independently. In
                # particular, old cleaned text must not leak its subject into
                # a retained-frame OCR candidate. Guidance may enrich only the
                # subject context; it never participates in occurrence matching
                # or becomes occurrence evidence.
                subject_context = " ".join(
                    part for part in (source_text, guidance) if part
                )
                for match in _event_matches([source_text]):
                    if match[0] != event_type:
                        continue
                    candidate_evidence = safe_decision_text(match[2], max_len=280)
                    subject = _subject_for_event(
                        event_type,
                        event_label,
                        candidate_evidence,
                        subject_context,
                    )
                    options.append(
                        (
                            source_priority,
                            sum(bool(part) for part in _subject_identity(subject, event_type)),
                            -source_index,
                            len(candidate_evidence),
                            subject,
                            candidate_evidence,
                        )
                    )
            if not options:
                fallback_context = " ".join(
                    part for part in (stored_evidence or stored_content, guidance) if part
                )
                return (
                    _subject_for_event(
                        event_type,
                        event_label,
                        stored_evidence,
                        fallback_context,
                    ),
                    stored_evidence,
                )
            selected = max(
                options,
                key=lambda option: (
                    option[0],
                    option[1],
                    option[2],
                    option[3],
                ),
            )
            return selected[4], selected[5]

        # First derive every member from its own safe source without the note.
        # A multi-source note may refine only one already verified shared
        # identity; generic or conflicting sources remain fail-closed.
        source_derivations = {
            str(row["item_key"]): derive_subject(row)
            for row in original_rows
        }
        source_identities = {
            _subject_identity(subject, event_type)
            for subject, _ in source_derivations.values()
        }
        verified_shared_subject = bool(
            len(original_rows) == 1
            or (
                len(source_identities) == 1
                and any(next(iter(source_identities), ()))
            )
        )
        note_group_ambiguous = bool(
            safe_note and len(original_rows) > 1 and not verified_shared_subject
        )
        subject_guidance = safe_note if verified_shared_subject else ""
        proposed_derivations = {
            str(row["item_key"]): (
                derive_subject(row, subject_guidance)
                if subject_guidance
                else source_derivations[str(row["item_key"])]
            )
            for row in original_rows
        }
        proposed_known_subjects = [
            pair
            for pair in proposed_derivations.values()
            if _subject_has_identity(pair[0], event_type)
        ]
        proposed_identities = {
            _subject_identity(subject, event_type)
            for subject, _ in proposed_known_subjects
        }
        resolved_semantics_would_change = any(
            (
                proposed_derivations[str(row["item_key"])][0]
                != _json_object(row.get("subject"))
                or proposed_derivations[str(row["item_key"])][1]
                != safe_decision_text(row.get("evidence") or "", max_len=280)
            )
            for row in original_rows
            if str(row.get("state") or "") not in UNRESOLVED_STATES
        )
        immutable_member_conflict = bool(
            mixed_legacy_group
            and (len(proposed_identities) > 1 or resolved_semantics_would_change)
        )
        block_subject_updates = immutable_member_conflict or note_group_ambiguous
        improved = False
        context_subject_conflicts: list[str] = []
        known_subjects = [] if block_subject_updates else proposed_known_subjects
        if block_subject_updates:
            context_subject_conflicts = _unique(
                [
                    safe_decision_text(subject.get("label") or "", max_len=100)
                    for subject, _ in proposed_known_subjects
                ]
            )
        else:
            for row in unresolved_rows:
                subject, evidence = proposed_derivations[str(row["item_key"])]
                old_subject = _json_object(row.get("subject"))
                old_evidence = safe_decision_text(row.get("evidence") or "", max_len=280)
                if subject == old_subject and evidence == old_evidence:
                    continue
                improved = True
                conn.execute(
                    "UPDATE activity_confirmation_items SET subject=?,evidence=?,decision_question=? "
                    "WHERE item_key=? AND state IN ('pending','deferred')",
                    (
                        json.dumps(subject, ensure_ascii=False, sort_keys=True),
                        evidence,
                        _decision_question(subject, str(row.get("observed_start") or "")),
                        row["item_key"],
                    ),
                )

        # If the card itself is still ambiguous, inspect only the post-firewall
        # safe timeline in a bounded +/-15 minute window. No raw OCR, screenshot,
        # Hermes, network, or model path is reachable here.
        used_nearby_context = False
        if not known_subjects and not block_subject_updates and not mixed_legacy_group and len(original_rows) == 1:
            from .pipeline import timeline

            used_nearby_context = True
            context_start, context_end = _clarification_context_window(original_rows)
            for activity in timeline(conn, context_start, context_end):
                if activity.get("quarantine_status") != "clear":
                    continue
                context_parts = [
                    safe_decision_text(activity.get("cleaned_text") or "", max_len=700),
                    safe_decision_text(activity.get("activity_summary") or "", max_len=420),
                    safe_decision_text(activity.get("cleaned_title") or "", max_len=180),
                ]
                context_text = " ".join(_unique([part for part in context_parts if part]))
                for match in _event_matches(context_parts):
                    if match[0] != event_type:
                        continue
                    subject = _subject_for_event(event_type, event_label, match[2], context_text)
                    if _subject_has_identity(subject, event_type):
                        pair = (subject, safe_decision_text(match[2], max_len=280))
                        if pair not in known_subjects:
                            known_subjects.append(pair)
            distinct_context_subjects = {
                _subject_identity(subject, event_type)
                for subject, _ in known_subjects
            }
            if len(distinct_context_subjects) == 1 and known_subjects:
                subject, evidence = known_subjects[0]
                improved = True
                mutable_placeholders = ",".join("?" for _ in unresolved_item_keys)
                conn.execute(
                    f"UPDATE activity_confirmation_items SET subject=?,evidence=?,decision_question=? "
                    f"WHERE item_key IN ({mutable_placeholders}) "
                    "AND state IN ('pending','deferred')",
                    (
                        json.dumps(subject, ensure_ascii=False, sort_keys=True),
                        evidence,
                        _decision_question(subject, str(original_rows[0].get("observed_start") or "")),
                        *unresolved_item_keys,
                    ),
                )
            elif len(distinct_context_subjects) > 1:
                # Nearby context can explain ambiguity, but it cannot safely
                # assign two subjects to one source-bound row. Keep one card
                # fail-closed and report the conflict instead of cloning facts.
                context_subject_conflicts = _unique(
                    [safe_decision_text(subject.get("label") or "", max_len=100) for subject, _ in known_subjects]
                )

        day_start, day_end = _group_day_window(original_rows)
        if not mixed_legacy_group and not note_group_ambiguous:
            _regroup_confirmation_items(
                conn,
                start=day_start,
                end=day_end,
                unresolved_only=True,
            )
        placeholders = ",".join("?" for _ in original_item_keys)
        regrouped_rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM activity_confirmation_items WHERE item_key IN ({placeholders}) "
                "ORDER BY datetime(observed_start),item_key",
                tuple(original_item_keys),
            ).fetchall()
        ]
        regrouped_keys = _unique([str(row.get("group_key") or "") for row in regrouped_rows])
        result_groups: list[dict[str, Any]] = []
        for regrouped_key in regrouped_keys:
            members = [row for row in regrouped_rows if str(row.get("group_key") or "") == regrouped_key]
            result_groups.append(_public_question_group(regrouped_key, members, conn=conn))
        if len(result_groups) > 1:
            outcome = "split"
            severity = "success"
            message = f"已识别出 {len(result_groups)} 个不同活动主体，问题已拆开。"
            missing_fields: list[str] = []
        else:
            final_subject = result_groups[0]["subject"] if result_groups else {}
            missing_fields = [
                field
                for field in ("company", "round")
                if event_type == "interview" and not final_subject.get(field)
            ]
            if immutable_member_conflict:
                outcome = "insufficient_information"
                severity = "warning"
                message = "这组线索包含已回答成员，本次补充说明无法在不改写历史题意的情况下安全应用；未修改原回答。"
            elif note_group_ambiguous:
                outcome = "insufficient_information"
                severity = "warning"
                message = "已使用你的补充说明重新梳理，但仍没有足够信息；可以继续补充，或忽略该问题。"
            elif improved and _subject_has_identity(final_subject, event_type):
                outcome = "rewritten"
                severity = "success"
                if evidence_context["evidence_status"] == "unavailable":
                    message = (
                        "原截图不可用；已根据仍可用的已清洗信息和你的补充说明补全主体并重写问题。"
                        if subject_guidance
                        else "原截图不可用；已根据仍可用的已清洗信息补全主体并重写问题。"
                    )
                else:
                    message = (
                        "已使用你的补充说明和可用本机证据补全主体并重写问题。"
                        if subject_guidance
                        else "已根据可用本机证据补全主体并重写问题。"
                    )
            elif evidence_context["evidence_status"] == "unavailable":
                outcome = "evidence_unavailable"
                severity = "warning"
                message = "原截图不可用；本次仅依据仍可用的已清洗信息复核，未发现可安全更新的信息。"
            elif _subject_has_identity(final_subject, event_type) and not missing_fields:
                outcome = "unchanged"
                severity = "neutral"
                message = "已重新读取可用证据，当前问题保持不变。"
            else:
                outcome = "insufficient_information"
                severity = "warning"
                if context_subject_conflicts:
                    message = f"附近已清洗信息出现多个可能主体（{'、'.join(context_subject_conflicts)}），无法安全对应到这条线索。"
                elif safe_note:
                    message = "已使用你的补充说明重新梳理，但仍没有足够信息；可以继续补充，或忽略该问题。"
                else:
                    message = "已重新读取可用证据，但仍没有足够信息。"
        if partially_filtered_count:
            prefix = f"已先过滤 {partially_filtered_count} 条不应作为真实发生事项的来源；"
            if outcome == "unchanged":
                outcome = "rewritten"
                severity = "success"
                message = prefix + "其余问题已重新整理并继续等待确认。"
            else:
                message = prefix + message
        if used_nearby_context:
            inputs_used.append("nearby_safe_context")
        generated_at = _utc_now()
        result = {
            "outcome": outcome,
            "severity": severity,
            "message": message,
            "missing_fields": missing_fields,
            "subject_conflicts": context_subject_conflicts,
            "inputs_used": _unique(inputs_used),
            **evidence_context,
            "question_groups": result_groups,
        }
        clarification = {
            "status": "completed",
            "generated_at": generated_at,
            **result,
        }
        result["generated_at"] = generated_at
        mutable_placeholders = ",".join("?" for _ in unresolved_item_keys)
        conn.execute(
            f"UPDATE activity_confirmation_items SET clarification=?,"
            "clarification_note=CASE WHEN ?=1 THEN ? ELSE clarification_note END "
            f"WHERE item_key IN ({mutable_placeholders}) "
            "AND state IN ('pending','deferred')",
            (
                json.dumps(clarification, ensure_ascii=False, sort_keys=True),
                1 if note_was_supplied else 0,
                safe_note,
                *unresolved_item_keys,
            ),
        )
        # Refresh the public groups so the persisted clarification is visible,
        # while member_revision remains tied only to membership/provenance.
        final_rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM activity_confirmation_items WHERE item_key IN ({placeholders}) "
                "ORDER BY datetime(observed_start),item_key",
                tuple(original_item_keys),
            ).fetchall()
        ]
        final_groups = []
        for final_key in _unique([str(row.get("group_key") or "") for row in final_rows]):
            final_groups.append(
                _public_question_group(
                    final_key,
                    [row for row in final_rows if str(row.get("group_key") or "") == final_key],
                    conn=conn,
                )
            )
        result["question_groups"] = final_groups
        current_group = final_groups[0] if final_groups else {}
        _finish_confirmation_transaction(conn, started_transaction, savepoint)
        if started_transaction and evidence_release_scheduled:
            _purge_evidence_after_commit(conn)
        return {
            "status": "clarified",
            "group_key": str(current_group.get("group_key") or group_key),
            "member_revision": str(current_group.get("member_revision") or actual_revision),
            "result": result,
            **flags,
        }
    except Exception:
        _rollback_confirmation_transaction(conn, started_transaction, savepoint)
        raise


def manual_add_confirmed_activity(
    conn: sqlite3.Connection,
    *,
    confirmation_action: str,
    safe_label: str,
    workstream: str = "",
    outcome: str = "unknown",
    started_at: str,
    ended_at: str | None = None,
    _authorization_lock_held: bool = False,
) -> dict[str, Any]:
    if str(confirmation_action or "") != MANUAL_CONFIRMATION_ACTION:
        return {
            "status": "blocked",
            "error": "explicit_manual_confirmation_required",
            **_safe_action_flags(),
        }
    label = _safe_manual_text(safe_label, field="safe_label", max_len=180, required=True)
    safe_workstream = _safe_manual_text(
        workstream,
        field="workstream",
        max_len=120,
        required=True,
    )
    if str(outcome or "unknown") not in OUTCOME_VALUES:
        raise ValueError("unsupported manual outcome")
    safe_outcome = _normalized_outcome(outcome)
    manual_event_type = "preparation" if safe_outcome == "preparation_only" else "manual"
    # This is the one intentional occurred + preparation_only representation:
    # the fact being confirmed is the preparation activity itself, not the
    # original interview/meeting/application candidate.  Keep it on the same
    # validator path as every other answer so it cannot become an implicit SQL
    # exception.
    _validate_preparation_answer(
        "occurred",
        "correct",
        safe_outcome,
        candidate_origin="manual",
        event_type=manual_event_type,
    )
    start_dt = _manual_datetime(started_at, field="started_at")
    end_dt = _manual_datetime(ended_at or started_at, field="ended_at")
    if end_dt < start_dt or end_dt - start_dt > timedelta(days=1):
        raise ValueError("invalid manual activity time range")
    start_text = start_dt.isoformat(timespec="seconds")
    end_text = end_dt.isoformat(timespec="seconds")
    fingerprint = _hash(
        "|".join(("manual", start_text, end_text, label, safe_workstream, safe_outcome))
    )
    item_key = "manual_" + fingerprint[:24]
    group_key = "grp_" + _hash(f"manual_group|{item_key}")[:24]
    subject = {
        "event_label": "手工确认活动",
        "company": "",
        "round": "",
        "object": label,
        "label": label,
        "company_identified": False,
        "round_identified": False,
        "object_identified": True,
    }
    candidate = {
        "item_key": item_key,
        "event_type": manual_event_type,
        "safe_observation": (
            f"已确认仅进行了准备：{label}"
            if safe_outcome == "preparation_only"
            else label
        ),
        "decision_question": "",
        "why_uncertain": "",
        "app_family": "Manual confirmation",
        "observed_start": start_text,
        "observed_end": end_text,
        "confidence": 1.0,
        "reason_codes": ["manual_confirmed_action", CONFIRMATION_POLICY_EVALUATED_REASON],
        "region_kind": "manual",
        "actionability": "user_confirmed",
        "source_activity_ids": [],
        "deletion_group_ids": [],
        "fingerprint": fingerprint,
        "calibration_key": "",
        "group_key": group_key,
        "group_signature": "sig_" + _hash(f"manual_signature|{item_key}")[:24],
        "subject": subject,
        "evidence": "",
        "cleaned_content": label,
        "question_kind": "occurrence",
        "candidate_origin": "manual",
        "priority": "high",
        "episode_id": "",
        "safe_signal_types": ["manual_user_entry"],
        "suggested_workstream": safe_workstream,
        "suggested_outcome": safe_outcome,
        "notification_eligible": False,
        "state": "confirmed",
    }
    authorization = (
        nullcontext()
        if _authorization_lock_held
        else document_authorization_lock(
            conn,
            exclusive=False,
            timeout=INTERACTIVE_WRITE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS,
        )
    )
    with authorization:
        started_transaction = not conn.in_transaction
        try:
            if started_transaction:
                conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM activity_confirmation_items WHERE item_key=?",
                (item_key,),
            ).fetchone()
            if existing and _stored_answer_matches(
                dict(existing),
                answer="occurred",
                reason="correct",
                workstream=safe_workstream,
                outcome=safe_outcome,
                generalize_scope="exact",
                note="",
            ):
                if started_transaction:
                    conn.commit()
                return {
                    "status": "recorded",
                    "unchanged": True,
                    "interface": INTERFACE,
                    "contract_version": CONTRACT_VERSION,
                    "item": _public_item(existing, conn=conn),
                    **_safe_action_flags(),
                }
            if existing and existing["answer"]:
                if started_transaction:
                    conn.commit()
                return {
                    "status": "blocked",
                    "error": "manual_confirmation_conflict",
                    "interface": INTERFACE,
                    "contract_version": CONTRACT_VERSION,
                    "item": _public_item(existing, conn=conn),
                    **_safe_action_flags(),
                }
            inserted = _upsert_confirmation_candidate(conn, candidate)
            if inserted is None:
                raise RuntimeError("manual_confirmation_insert_failed")
            now = _utc_now()
            conn.execute(
                "UPDATE activity_confirmation_items SET state='confirmed',answer='occurred',"
                "answer_reason='correct',user_workstream=?,user_outcome=?,"
                "notification_eligible=0,generalize_scope='exact',answered_at=?,updated_at=? "
                "WHERE item_key=? AND answer IS NULL",
                (safe_workstream, safe_outcome, now, now, item_key),
            )
            refreshed = conn.execute(
                "SELECT * FROM activity_confirmation_items WHERE item_key=?",
                (item_key,),
            ).fetchone()
            if started_transaction:
                conn.commit()
            return {
                "status": "recorded",
                "interface": INTERFACE,
                "contract_version": CONTRACT_VERSION,
                "item": _public_item(refreshed, conn=conn) if refreshed else {},
                **_safe_action_flags(),
            }
        except Exception:
            if started_transaction and conn.in_transaction:
                conn.rollback()
            raise


def settle_activity_confirmations(conn: sqlite3.Connection, *, report_date: str) -> dict[str, Any]:
    day = date.fromisoformat(str(report_date or ""))
    tz = datetime.now().astimezone().tzinfo
    start_dt = datetime.combine(day, time.min, tzinfo=tz)
    end_dt = start_dt + timedelta(days=1)
    start = start_dt.isoformat(timespec="seconds")
    end = end_dt.isoformat(timespec="seconds")
    with document_authorization_lock(conn, exclusive=False):
        started_transaction = not conn.in_transaction
        try:
            if started_transaction:
                conn.execute("BEGIN IMMEDIATE")
            materialize_activity_confirmations(conn, start, end, commit=False)
            view = confirmed_activity_view(conn, start, end, materialize=False)
            diagnostics = _confirmation_diagnostics_locked(conn, start, end, view=view)
            completeness = diagnostics["confirmation_completeness"]
            revision = str(completeness["revision"])
            existing = conn.execute(
                "SELECT revision,settled_at,created_at FROM activity_confirmation_settlements "
                "WHERE report_date=?",
                (day.isoformat(),),
            ).fetchone()
            unchanged = bool(existing and str(existing["revision"] or "") == revision)
            now = _utc_now()
            settled_at = str(existing["settled_at"] or "") if unchanged else now
            created_at = str(existing["created_at"] or "") if existing else now
            pending_by_kind = completeness["pending_by_kind"]
            candidate_funnel = completeness["candidate_funnel"]
            if int(diagnostics.get("orphan_count") or 0):
                status = "partial"
            elif int(completeness["pending_total"] or 0):
                status = "pending"
            else:
                status = "complete"
            conn.execute(
                """
                INSERT INTO activity_confirmation_settlements(
                  report_date,revision,settled_at,completeness_status,pending_total,
                  pending_high_priority,pending_by_kind,candidate_funnel,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(report_date) DO UPDATE SET
                  revision=excluded.revision,
                  settled_at=CASE
                    WHEN activity_confirmation_settlements.revision=excluded.revision
                    THEN activity_confirmation_settlements.settled_at
                    ELSE excluded.settled_at
                  END,
                  completeness_status=excluded.completeness_status,
                  pending_total=excluded.pending_total,
                  pending_high_priority=excluded.pending_high_priority,
                  pending_by_kind=excluded.pending_by_kind,
                  candidate_funnel=excluded.candidate_funnel,
                  updated_at=excluded.updated_at
                """,
                (
                    day.isoformat(), revision, settled_at, status,
                    int(completeness["pending_total"] or 0),
                    int(completeness["pending_high_priority"] or 0),
                    json.dumps(pending_by_kind, sort_keys=True),
                    json.dumps(candidate_funnel, sort_keys=True),
                    created_at, now,
                ),
            )
            if started_transaction:
                conn.commit()
            settled_completeness = dict(completeness)
            settled_completeness["status"] = status
            settled_completeness["settled_at"] = settled_at
            return {
                "status": "settled",
                "interface": INTERFACE,
                "contract_version": CONTRACT_VERSION,
                "report_date": day.isoformat(),
                "settled_at": settled_at,
                "revision": revision,
                "confirmation_completeness": settled_completeness,
                **_safe_action_flags(),
            }
        except Exception:
            if started_transaction and conn.in_transaction:
                conn.rollback()
            raise


def _safe_manual_text(value: Any, *, field: str, max_len: int, required: bool = False) -> str:
    raw = str(value or "")
    if len(raw) > max_len * 4:
        raise ValueError(f"{field} exceeds safe input limit")
    if unsafe_decision_text(raw) or re.search(
        r"(?:验证码|校验码|动态码|一次性验证码|\bOTP\b).{0,16}\b\d{4,8}\b|"
        r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret)\b\s*[:=]",
        raw,
        re.I,
    ):
        raise ValueError(f"{field} contains sensitive content")
    safe = safe_decision_text(raw, max_len=max_len)
    if any(marker in safe for marker in ("[path_redacted]", "[secret_redacted]", "[email_redacted]", "[phone_redacted]")):
        raise ValueError(f"{field} contains sensitive content")
    if required and not safe:
        raise ValueError(f"{field} is required")
    return safe


def _manual_datetime(value: Any, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {field}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone()


def _answer_details(answer: str, workstream: Any, outcome: Any) -> tuple[str, str]:
    if answer in {"ignored", "defer"}:
        return "", ""
    safe_workstream = _safe_manual_text(workstream, field="workstream", max_len=120)
    raw_outcome = str(outcome or "").strip()
    if raw_outcome and raw_outcome not in OUTCOME_VALUES:
        raise ValueError("unsupported activity outcome")
    if answer in {"not_occurred", "misrecognized"} and raw_outcome not in {
        "",
        "unknown",
        "preparation_only",
    }:
        raise ValueError("negative occurrence can only be classified as preparation_only or unknown")
    return safe_workstream, _normalized_outcome(raw_outcome or "unknown")


def _stored_answer_matches(
    row: dict[str, Any],
    *,
    answer: str,
    reason: str,
    workstream: str,
    outcome: str,
    generalize_scope: str,
    note: str,
) -> bool:
    if str(row.get("answer") or "") != answer:
        return False
    if str(row.get("answer_reason") or "") != reason:
        return False
    if str(row.get("user_workstream") or "") != workstream:
        return False
    if _normalized_outcome(row.get("user_outcome") or "unknown") != _normalized_outcome(outcome):
        return False
    if str(row.get("generalize_scope") or "exact") != generalize_scope:
        return False
    if answer == "defer":
        safe_note = safe_decision_text(note, max_len=240)
        if safe_note and safe_note != str(row.get("clarification_note") or ""):
            return False
    return True


def _validate_preparation_answer(
    answer: str,
    reason: str,
    outcome: str,
    *,
    candidate_origin: str = "",
    event_type: str = "",
) -> None:
    preparation_reason = reason == "preparation_only"
    preparation_outcome = outcome == "preparation_only"
    manual_preparation_fact = (
        candidate_origin == "manual"
        and event_type == "preparation"
        and answer == "occurred"
        and reason == "correct"
        and preparation_outcome
    )
    if manual_preparation_fact:
        return
    if answer == "not_occurred":
        if preparation_reason != preparation_outcome:
            raise ValueError(
                "not_occurred preparation_only reason and outcome must be submitted together"
            )
        return
    if preparation_reason or preparation_outcome:
        raise ValueError("preparation_only classification requires not_occurred")


def revoke_confirmation_calibration(
    conn: sqlite3.Connection,
    *,
    calibration_key: str,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT calibration_key FROM activity_confirmation_calibrations WHERE calibration_key=?",
        (calibration_key,),
    ).fetchone()
    if not row:
        return {"status": "blocked", "error": "calibration_missing", "raw_access": False}
    now = _utc_now()
    conn.execute(
        "UPDATE activity_confirmation_calibrations SET revoked_at=?,updated_at=? WHERE calibration_key=?",
        (now, now, calibration_key),
    )
    conn.commit()
    return {
        "status": "revoked",
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "calibration_key": calibration_key,
        "message": "已撤销这条校准规则；历史回答保持不变。",
    }


def confirmed_activity_view(
    conn: sqlite3.Connection,
    start: str | None,
    end: str | None,
    *,
    materialize: bool = False,
) -> dict[str, Any]:
    """Return two explicit, versioned trust projections.

    ``activities`` is the backwards-compatible alias for the governed
    trusted projection.  Keeping this legacy field trusted prevents old
    callers from accidentally exporting local observations after upgrade.

    ``owner_activities`` is the owner-facing local observation projection. It
    may contain Privacy-Firewall-cleaned screen/OCR observations, but every
    such row is explicitly marked ``verification_state=unverified`` and is
    never eligible for completion claims, trusted export, HA facts, or memory.

    ``trusted_activities`` is the governed export projection.  Only an
    explicit user answer can release an activity into this list.  Controlled
    non-screen evidence enters the separate Context Console graph and is not
    inferred from this legacy screen timeline.
    """
    from .pipeline import timeline

    if materialize:
        materialize_activity_confirmations(conn, start, end)
    clear_activities = [
        item for item in timeline(conn, start, end)
        if item.get("quarantine_status") == "clear"
    ]
    activity_by_id = {
        str(item.get("id") or ""): item
        for item in clear_activities
        if item.get("id")
    }
    supplemental_rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM activity_confirmation_items "
            "WHERE candidate_origin IN ('business_episode','manual') "
            "AND datetime(observed_start)<datetime(?) AND datetime(observed_end)>datetime(?) "
            "ORDER BY datetime(observed_start),item_key",
            (end, start),
        ).fetchall()
        if _confirmation_source_is_live(conn, row)
    ]

    owner_by_key: dict[str, dict[str, Any]] = {}
    trusted: list[dict[str, Any]] = []
    unverified_activity_clues: list[dict[str, Any]] = []
    unverified_clues: list[dict[str, Any]] = []
    candidate_entries: list[dict[str, Any]] = []

    def add_owner(activity: dict[str, Any], reason_code: str, *, source: str = "screen_observation") -> None:
        clue = _unverified_screen_activity_clue(
            activity,
            reason_code,
            confirmation_source=source,
        )
        key = str(clue.get("id") or clue.get("source_ref") or _public_activity_key(clue))
        owner_by_key[key] = clue
        unverified_activity_clues.append(clue)
        unverified_clues.append(_content_free_screen_clue(clue, reason_code))

    # Every clear screen row remains visible to the owner as a cleaned local
    # observation.  Candidate settlement controls only the trusted projection.
    for activity in clear_activities:
        candidate = _candidate_for_activity(conn, activity)
        if candidate is None:
            add_owner(activity, "screen_observation_only")
            continue
        persisted = conn.execute(
            "SELECT * FROM activity_confirmation_items WHERE item_key=?",
            (candidate["item_key"],),
        ).fetchone()
        if not persisted:
            add_owner(activity, "confirmation_candidate_not_materialized")
            continue
        metadata = dict(persisted)
        metadata.setdefault("answer", None)
        metadata.setdefault("user_note", "")
        metadata.setdefault("group_key", candidate["group_key"])
        metadata.setdefault("group_signature", candidate["group_signature"])
        metadata.setdefault("subject", candidate["subject"])
        metadata.setdefault("evidence", candidate["evidence"])
        metadata["activity"] = activity
        candidate_entries.append(metadata)
        add_owner(activity, "confirmation_pending_or_untrusted")

    for row in supplemental_rows:
        if str(row.get("candidate_origin") or "") == "manual":
            metadata = dict(row)
            metadata["activity"] = _manual_activity(row)
            candidate_entries.append(metadata)
            continue
        sources = [
            activity_by_id[source_id]
            for source_id in _json_list(row.get("source_activity_ids"))
            if source_id in activity_by_id
        ]
        for source in sources:
            metadata = dict(row)
            metadata["activity"] = source
            candidate_entries.append(metadata)

    pending_groups = 0
    user_confirmed_groups = 0
    user_rejected_groups = 0
    auto_suppressed_groups = 0
    for cluster in _confirmation_view_clusters(candidate_entries):
        states = {str(entry.get("state") or "pending") for entry in cluster}
        answers = {str(entry.get("answer") or "") for entry in cluster if entry.get("answer")}
        if states.intersection(UNRESOLVED_STATES):
            pending_groups += 1
            continue
        if "ignored" in states:
            continue
        if "filtered" in states:
            auto_suppressed_groups += 1
            continue
        if "rejected" in states:
            if answers.intersection({"not_occurred", "misrecognized"}):
                user_rejected_groups += 1
                outcomes = {
                    _normalized_outcome(entry.get("user_outcome") or "unknown")
                    for entry in cluster
                }
                if answers == {"not_occurred"} and outcomes == {"preparation_only"}:
                    representative_entry = min(
                        cluster,
                        key=lambda entry: (
                            str(entry["activity"].get("started_at") or ""),
                            str(entry["activity"].get("id") or ""),
                        ),
                    )
                    prepared = _canonical_group_activity(cluster, representative_entry)
                    prepared["event_type"] = "preparation"
                    prepared["activity_summary"] = _confirmed_preparation_summary(representative_entry)
                    prepared["cleaned_text"] = prepared["activity_summary"]
                    prepared["cleaned_title"] = ""
                    prepared["outcome"] = "preparation_only"
                    prepared["occurrence_state"] = "user_confirmed_preparation"
                    prepared["confirmation_source"] = "user"
                    prepared["verification_state"] = "verified"
                    prepared["trust_partition"] = "trusted_user_confirmation"
                    prepared["verified_context_eligible"] = True
                    trusted.append(prepared)
                    owner_by_key[_public_activity_key(prepared)] = prepared
            else:
                auto_suppressed_groups += 1
            continue
        if states != {"confirmed"}:
            pending_groups += 1
            continue
        if answers and answers != {"occurred"}:
            pending_groups += 1
            continue
        representative_entry = min(
            cluster,
            key=lambda entry: (
                str(entry["activity"].get("started_at") or ""),
                str(entry["activity"].get("id") or ""),
            ),
        )
        enriched = _canonical_group_activity(cluster, representative_entry)
        if answers == {"occurred"}:
            if (
                str(enriched.get("candidate_origin") or "") == "manual"
                and str(enriched.get("event_type") or "") == "preparation"
                and _normalized_outcome(enriched.get("outcome") or "unknown")
                == "preparation_only"
            ):
                enriched["occurrence_state"] = "user_confirmed_preparation"
            else:
                enriched["occurrence_state"] = "user_confirmed"
            enriched["confirmation_source"] = "user"
            enriched["verification_state"] = "verified"
            enriched["trust_partition"] = "trusted_user_confirmation"
            enriched["verified_context_eligible"] = True
            # Explicit user confirmation is the only legacy screen-timeline
            # path that may enter the governed HA/Memory review projection.
            # The actual writer remains disabled and downstream subject/privacy
            # gates still fail closed.
            enriched["memory_candidate_eligible"] = True
            enriched["completion_claim_eligible"] = True
            user_confirmed_groups += 1
            trusted.append(enriched)
            # Replace the unverified owner copy with the confirmed canonical
            # group, preserving one owner-facing row per logical source.
            source_ids = _json_list(enriched.get("confirmation_source_activity_ids"))
            for source_id in source_ids:
                owner_by_key.pop(source_id, None)
            owner_by_key[_public_activity_key(enriched)] = enriched
        else:
            # A legacy deterministic ``confirmed`` state without a user answer
            # is deliberately not trusted.  Its owner copy already exists.
            continue

    owner_activities = sorted(
        owner_by_key.values(),
        key=lambda item: (str(item.get("started_at") or ""), _public_activity_key(item)),
    )
    trusted.sort(key=lambda item: (str(item.get("started_at") or ""), _public_activity_key(item)))
    unverified_activity_clues = [
        item for item in owner_activities
        if str(item.get("verification_state") or "") == "unverified"
    ]
    # Deduplicate content-free clues after grouped confirmations replaced local
    # owner rows.
    live_unverified_ids = {
        str(item.get("id") or "") for item in unverified_activity_clues if item.get("id")
    }
    unverified_clues = [
        item for item in unverified_clues
        if not item.get("source_activity_id")
        or str(item.get("source_activity_id")) in live_unverified_ids
    ]
    clue_by_ref = {str(item.get("source_ref") or ""): item for item in unverified_clues}
    return {
        "projection_version": 2,
        "owner_observation_projection": "local_observation_v2",
        "trusted_projection": "trusted_activity_v2",
        "activities": trusted,
        "owner_activities": owner_activities,
        "trusted_activities": trusted,
        "unverified_activity_clues": unverified_activity_clues,
        "clear_activity_count": len(clear_activities),
        "owner_observation_count": len(owner_activities),
        "reportable_activity_count": len(trusted),
        "pending_confirmation_count": pending_groups,
        "user_confirmed_count": user_confirmed_groups,
        "user_rejected_count": user_rejected_groups,
        "auto_suppressed_count": auto_suppressed_groups,
        "unverified_clue_count": len(clue_by_ref),
        "unverified_clues": sorted(
            clue_by_ref.values(),
            key=lambda item: (str(item.get("started_at") or ""), str(item.get("source_ref") or "")),
        ),
        "raw_access": False,
    }


def _public_activity_key(activity: Mapping[str, Any]) -> str:
    explicit = str(activity.get("id") or "")
    if explicit:
        return explicit
    material = "|".join(
        (
            str(activity.get("started_at") or ""),
            str(activity.get("ended_at") or ""),
            str(activity.get("event_type") or ""),
            str(activity.get("activity_summary") or ""),
        )
    )
    return "activity_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

def _unverified_screen_activity_clue(
    activity: dict[str, Any],
    reason_code: str,
    *,
    confirmation_source: str,
) -> dict[str, Any]:
    """Return a privacy-cleaned local clue that cannot cross trust gates.

    ``timeline()`` has already removed raw screenshots/OCR and privacy-blocked
    content.  Keeping this projection allows local search, quality diagnostics,
    and owner-facing navigation to continue without restoring the old
    ``evidence_confirmed`` mistake.  Every state-bearing field is fail-closed,
    so callers must opt into the clue partition instead of accidentally
    treating it as a verified fact.
    """

    clue = dict(activity)
    clue["event_type"] = str(clue.get("event_type") or "observed_activity")
    clue["occurrence_state"] = "unverified_clue"
    clue["confirmation_source"] = confirmation_source
    clue["verification_state"] = "unverified"
    clue["trust_partition"] = "unverified_clue"
    clue["reason_code"] = reason_code
    clue["verified_context_eligible"] = False
    clue["memory_candidate_eligible"] = False
    clue["completion_claim_eligible"] = False
    clue["raw_access"] = False
    return clue


def _content_free_screen_clue(activity: dict[str, Any], reason_code: str) -> dict[str, Any]:
    source_material = "|".join(
        (
            str(activity.get("id") or ""),
            str(activity.get("started_at") or ""),
            str(activity.get("ended_at") or ""),
            reason_code,
        )
    )
    observed_at = str(activity.get("started_at") or "")
    ended_at = str(activity.get("ended_at") or observed_at)
    apps = _json_list(activity.get("apps"))
    return {
        "source_ref": "source_" + hashlib.sha256(source_material.encode("utf-8")).hexdigest()[:32],
        "source_activity_id": str(activity.get("id") or ""),
        "clue_type": "screen_or_ocr",
        "observed_at": observed_at,
        "started_at": observed_at,
        "ended_at": ended_at,
        "verification_state": "unverified",
        "reason_code": reason_code,
        # Content-free quality metadata is permitted for diagnostics, but no
        # title, OCR-derived text, document body, path, domain, or internal ID
        # is copied into this projection.
        "privacy_level": str(activity.get("privacy_level") or "normal"),
        "dedupe_status": str(activity.get("dedupe_status") or "unique"),
        "dedupe_note": safe_decision_text(activity.get("dedupe_note") or "", max_len=120),
        "screen_region_signal": activity.get("screen_region_signal") or {},
        "app_name": safe_decision_text(apps[0] if apps else "", max_len=80),
        "app_class": _screen_clue_app_class(activity),
        "raw_access": False,
    }


def _screen_clue_app_class(activity: dict[str, Any]) -> str:
    apps = _json_list(activity.get("apps"))
    app = str(apps[0] if apps else "").strip().lower()
    if not app:
        return "unknown"
    if any(token in app for token in ("finder", "qspace", "files")):
        return "file_manager"
    if any(token in app for token in ("iina", "vlc", "quicktime", "photos", "preview")):
        return "media_or_preview"
    if any(token in app for token in ("codex", "cursor", "xcode", "visual studio", "terminal", "warp")):
        return "developer_tool"
    if any(token in app for token in ("obsidian", "typora", "notes", "notion")):
        return "notes_or_editor"
    return "other"


def _confirmation_view_clusters(entries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    for entry in sorted(
        entries,
        key=lambda item: (
            str(item.get("observed_start") or ""),
            str(item.get("item_key") or ""),
        ),
    ):
        entry_group_key = str(entry.get("group_key") or "")
        matching = next(
            (
                cluster
                for cluster in clusters
                if entry_group_key
                and any(str(member.get("group_key") or "") == entry_group_key for member in cluster)
            ),
            None,
        )
        if matching is not None:
            matching.append(entry)
            continue
        for cluster in reversed(clusters):
            if any(member.get("answer") for member in cluster) or entry.get("answer"):
                continue
            if all(_rows_can_group(member, entry) for member in cluster):
                cluster.append(entry)
                break
        else:
            clusters.append([entry])
    return clusters


def _canonical_group_activity(
    cluster: list[dict[str, Any]],
    representative_entry: dict[str, Any],
) -> dict[str, Any]:
    enriched = dict(representative_entry["activity"])
    activities = [entry["activity"] for entry in cluster]
    starts = [str(activity.get("started_at") or "") for activity in activities]
    ends = [str(activity.get("ended_at") or activity.get("started_at") or "") for activity in activities]
    enriched["started_at"] = min(starts) if starts else enriched.get("started_at")
    enriched["ended_at"] = max(ends) if ends else enriched.get("ended_at")
    enriched["apps"] = _unique(
        [app for activity in activities for app in _json_list(activity.get("apps"))]
    )
    enriched["domains"] = _unique(
        [domain for activity in activities for domain in _json_list(activity.get("domains"))]
    )
    enriched["evidence_refs"] = _unique(
        [ref for activity in activities for ref in _json_list(activity.get("evidence_refs"))]
    )
    notes = _unique(
        [safe_decision_text(entry.get("user_note") or "", max_len=240) for entry in cluster]
    )
    if notes:
        enriched["activity_summary"] = notes[0]
        enriched["cleaned_text"] = notes[0]
        enriched["cleaned_title"] = ""
        enriched["document_contexts"] = "[]"
    origins = _unique([str(entry.get("candidate_origin") or "explicit_claim") for entry in cluster])
    if "manual" in origins:
        enriched["activity_summary"] = safe_decision_text(
            representative_entry.get("safe_observation") or enriched.get("activity_summary") or "",
            max_len=420,
        )
        enriched["cleaned_text"] = enriched["activity_summary"]
        enriched["cleaned_title"] = ""
        enriched["document_contexts"] = "[]"
    elif "business_episode" in origins:
        enriched["activity_summary"] = _confirmed_episode_summary(representative_entry)
        enriched["cleaned_text"] = enriched["activity_summary"]
        enriched["cleaned_title"] = ""
    enriched["event_type"] = str(representative_entry.get("event_type") or "")
    group_key = str(representative_entry.get("group_key") or "") or (
        "grp_" + _hash("|".join(sorted(str(entry.get("item_key") or "") for entry in cluster)))[:24]
    )
    enriched["confirmation_group_key"] = group_key
    source_activity_ids = _unique(
        [
            source_id
            for entry in cluster
            for source_id in _json_list(entry.get("source_activity_ids"))
        ]
        + [str(activity.get("id") or "") for activity in activities if activity.get("id")]
    )
    enriched["confirmation_source_count"] = len(source_activity_ids) or len(cluster)
    enriched["confirmation_source_activity_ids"] = source_activity_ids
    enriched["confirmation_deletion_group_ids"] = _unique(
        [
            deletion_group_id
            for entry in cluster
            for deletion_group_id in _json_list(entry.get("deletion_group_ids"))
        ]
        + [str(activity.get("deletion_group_id") or "") for activity in activities]
    )
    enriched["question_kind"] = str(representative_entry.get("question_kind") or "occurrence")
    enriched["candidate_origin"] = str(representative_entry.get("candidate_origin") or "explicit_claim")
    enriched["episode_id"] = str(representative_entry.get("episode_id") or "")
    enriched["workstream"] = safe_decision_text(
        representative_entry.get("user_workstream")
        or representative_entry.get("suggested_workstream")
        or "",
        max_len=120,
    )
    enriched["outcome"] = _normalized_outcome(
        representative_entry.get("user_outcome")
        or representative_entry.get("suggested_outcome")
        or "unknown"
    )
    enriched["confirmation_subject_scope_hint"] = _confirmed_subject_scope_hint(
        activities
    )
    enriched["confirmation_candidate_type_hint"] = _confirmed_candidate_type_hint(
        activities
    )
    return enriched


def _confirmed_subject_scope_hint(activities: list[dict[str, Any]]) -> str:
    """Derive only a content-free self-ownership hint from confirmed sources."""
    for activity in activities:
        text = " ".join(
            safe_decision_text(activity.get(field) or "", max_len=700)
            for field in ("activity_summary", "cleaned_text", "cleaned_title")
        )
        if CONFIRMED_SELF_SUBJECT_RE.search(text):
            return "self_or_user_owned"
    return ""


def _confirmed_candidate_type_hint(activities: list[dict[str, Any]]) -> str:
    """Retain a safe durable-type enum when canonicalizing confirmed text."""
    text = " ".join(
        safe_decision_text(activity.get(field) or "", max_len=700)
        for activity in activities
        for field in ("activity_summary", "cleaned_text", "cleaned_title")
    )
    if re.search(r"(?:决定|决策|\bdecision\b|\bdecided\b|不再|默认|以后|规则|要求)", text, re.I):
        return "decision"
    if re.search(r"(?:偏好|喜欢|不喜欢|\bprefer(?:ence)?\b)", text, re.I):
        return "preference"
    if re.search(r"(?:阻塞|失败|报错|卡住|\bmissing\b|\berror\b)", text, re.I):
        return "blocker"
    if re.search(r"(?:简历|面试|岗位|文档|\bresume\b|\bcv\b|\bmarkdown\b)", text, re.I):
        return "document_work"
    if re.search(r"(?:待办|跟进|下一步|\btodo\b)", text, re.I):
        return "follow_up"
    return ""


def _confirmed_episode_summary(entry: dict[str, Any]) -> str:
    subject = _json_object(entry.get("subject"))
    label = safe_decision_text(
        subject.get("label") or _event_label(str(entry.get("event_type") or "")),
        max_len=120,
    )
    workstream = safe_decision_text(
        entry.get("user_workstream") or entry.get("suggested_workstream") or "",
        max_len=120,
    )
    outcome = _normalized_outcome(entry.get("user_outcome") or entry.get("suggested_outcome") or "unknown")
    outcome_label = {
        "completed": "完成",
        "progressing": "推进中",
        "blocked": "受阻",
        "unknown": "结果未知",
    }.get(outcome, "结果未知")
    details = f"业务线：{workstream}；" if workstream else ""
    return safe_decision_text(f"已确认：{label}已发生（{details}结果：{outcome_label}）。", max_len=420)


def _confirmed_preparation_summary(entry: dict[str, Any]) -> str:
    subject = _json_object(entry.get("subject"))
    label = safe_decision_text(subject.get("label") or "该业务事项", max_len=120)
    workstream = safe_decision_text(
        entry.get("user_workstream") or entry.get("suggested_workstream") or "",
        max_len=120,
    )
    suffix = f"，业务线：{workstream}" if workstream else ""
    return safe_decision_text(f"已确认：仅进行了{label}的准备{suffix}；原候选事件未作为已发生事实记录。", max_len=420)


def _manual_activity(row: dict[str, Any]) -> dict[str, Any]:
    label = safe_decision_text(row.get("safe_observation") or "手工确认活动", max_len=420)
    return {
        "id": str(row.get("item_key") or ""),
        "started_at": str(row.get("observed_start") or ""),
        "ended_at": str(row.get("observed_end") or row.get("observed_start") or ""),
        "activity_summary": label,
        "cleaned_text": label,
        "cleaned_title": "",
        "apps": ["Manual confirmation"],
        "domains": [],
        "evidence_refs": [],
        "document_contexts": [],
        "privacy_level": "normal",
        "redaction_confidence": 1.0,
        "quarantine_status": "clear",
        "deletion_group_id": "",
        "dedupe_status": "unique",
        "dedupe_note": "",
        "dedupe_duplicate_lines": 0,
        "dedupe_retained_lines": 1,
        "dedupe_content_hash": _hash(label),
    }


def _update_confirmation_calibration(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    *,
    answer: str,
    reason: str,
    generalize_scope: str,
    now: str,
    previous_items: list[dict[str, Any]] | None = None,
) -> None:
    calibration_key = str(item.get("calibration_key") or "")
    if not calibration_key:
        return
    existing_row = conn.execute(
        "SELECT * FROM activity_confirmation_calibrations WHERE calibration_key=?",
        (calibration_key,),
    ).fetchone()
    existing = dict(existing_row) if existing_row else {}
    positive = int(existing.get("positive_count") or 0)
    negative = int(existing.get("negative_count") or 0)
    misrecognized = int(existing.get("misrecognized_count") or 0)
    try:
        reason_counts = json.loads(existing.get("reason_counts") or "{}")
    except (TypeError, json.JSONDecodeError):
        reason_counts = {}
    if not isinstance(reason_counts, dict):
        reason_counts = {}

    def apply_feedback(current_answer: str, current_reason: str, scope: str, delta: int) -> None:
        nonlocal positive, negative, misrecognized
        if current_answer == "occurred":
            positive = max(0, positive + delta)
        if scope != "similar":
            return
        if current_answer == "not_occurred":
            negative = max(0, negative + delta)
        elif current_answer == "misrecognized":
            misrecognized = max(0, misrecognized + delta)
        else:
            return
        if current_reason in NEGATIVE_GENERALIZATION_REASONS:
            updated = max(0, int(reason_counts.get(current_reason) or 0) + delta)
            if updated:
                reason_counts[current_reason] = updated
            else:
                reason_counts.pop(current_reason, None)

    prior_feedback = previous_items if previous_items is not None else [item]
    for previous_item in prior_feedback:
        if str(previous_item.get("calibration_key") or "") != calibration_key:
            continue
        previous_answer = str(previous_item.get("answer") or "")
        previous_reason = str(previous_item.get("answer_reason") or "")
        previous_scope = str(previous_item.get("generalize_scope") or "exact")
        if previous_answer:
            apply_feedback(previous_answer, previous_reason, previous_scope, -1)
    apply_feedback(answer, reason, generalize_scope, 1)

    if not existing and answer == "defer":
        return
    created_at = str(existing.get("created_at") or now)
    reactivates_rule = (
        answer in {"not_occurred", "misrecognized"}
        and generalize_scope == "similar"
        and reason in NEGATIVE_GENERALIZATION_REASONS
    )
    revoked_at = None if reactivates_rule else existing.get("revoked_at")
    conn.execute(
        """
        INSERT INTO activity_confirmation_calibrations(
          calibration_key,event_type,app_family,region_kind,actionability,
          positive_count,negative_count,misrecognized_count,reason_counts,
          last_answer,last_reason,revoked_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(calibration_key) DO UPDATE SET
          event_type=excluded.event_type,
          app_family=excluded.app_family,
          region_kind=excluded.region_kind,
          actionability=excluded.actionability,
          positive_count=excluded.positive_count,
          negative_count=excluded.negative_count,
          misrecognized_count=excluded.misrecognized_count,
          reason_counts=excluded.reason_counts,
          last_answer=excluded.last_answer,
          last_reason=excluded.last_reason,
          revoked_at=excluded.revoked_at,
          updated_at=excluded.updated_at
        """,
        (
            calibration_key,
            str(item.get("event_type") or ""),
            str(item.get("app_family") or ""),
            str(item.get("region_kind") or "unknown"),
            str(item.get("actionability") or "unknown"),
            positive,
            negative,
            misrecognized,
            json.dumps(reason_counts, sort_keys=True),
            answer,
            reason,
            revoked_at,
            created_at,
            now,
        ),
    )


def rebuild_confirmation_calibration(
    conn: sqlite3.Connection,
    calibration_keys: list[str] | None = None,
    *,
    preserve_revoked: bool = True,
) -> int:
    # Calibrations are durable, content-free settings.  They cannot be rebuilt
    # from source-bound items because retention may already have removed those
    # rows.  Keep this compatibility entry point read-only so no caller can
    # accidentally erase durable reason counters.
    keys = sorted({str(key) for key in (calibration_keys or []) if str(key)})
    if not keys:
        row = conn.execute("SELECT COUNT(*) AS count FROM activity_confirmation_calibrations").fetchone()
        return int(row["count"] or 0) if row else 0
    placeholders = ",".join("?" for _ in keys)
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM activity_confirmation_calibrations WHERE calibration_key IN ({placeholders})",
        keys,
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def resolve_confirmation_range(
    range_name: str,
    *,
    start: str | None = None,
    end: str | None = None,
) -> tuple[str, str]:
    if start and end:
        return start, end
    now = datetime.now().astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_name == "yesterday":
        resolved_start = today - timedelta(days=1)
        resolved_end = today
    elif range_name == "7d":
        resolved_start = today - timedelta(days=6)
        resolved_end = today + timedelta(days=1)
    else:
        resolved_start = today
        resolved_end = today + timedelta(days=1)
    return resolved_start.isoformat(timespec="seconds"), resolved_end.isoformat(timespec="seconds")


def _local_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone()


def _candidate_for_activity(conn: sqlite3.Connection, activity: dict[str, Any]) -> dict[str, Any] | None:
    summary = safe_decision_text(activity.get("activity_summary") or "", max_len=420)
    cleaned = safe_decision_text(activity.get("cleaned_text") or "", max_len=700)
    title = safe_decision_text(activity.get("cleaned_title") or "", max_len=180)
    cleaned_parts = _unique([part for part in (cleaned, summary, title) if part])
    match = _event_match(cleaned_parts)
    if not match:
        return None
    event_type, event_label, trigger_sentence = match
    text = " ".join(cleaned_parts)
    apps = _json_list(activity.get("apps"))
    app_family = safe_decision_text(apps[0] if apps else "unknown", max_len=80)
    if _is_personal_ai_monitor_app(app_family):
        return None
    observed_start = str(activity.get("started_at") or "")
    observed_end = str(activity.get("ended_at") or observed_start)
    region = _region_signal(activity.get("screen_region_signal"))
    region_kind = region["region_kind"]
    actionability = region["actionability"]
    region_reasons = region["reason_codes"]
    independent_artifact_signals = _activity_independent_artifact_signals(
        activity,
        region_reasons,
    )
    subject = _subject_for_event(event_type, event_label, trigger_sentence, text)
    evidence = safe_decision_text(trigger_sentence, max_len=280)
    identity = _normalized_identity(evidence or event_label, event_type)
    day = observed_start[:10]
    hour_bucket = observed_start[:13]
    source_activity_id = safe_decision_text(activity.get("id") or "", max_len=120)
    fingerprint = _hash(
        "|".join(
            (
                day,
                hour_bucket,
                event_type,
                app_family.lower(),
                region_kind,
                actionability,
                identity,
                source_activity_id,
            )
        )
    )
    item_key = "occ_" + fingerprint[:24]
    calibration_key = "cal_" + _hash("|".join((event_type, app_family.lower(), region_kind, actionability)))[:24]
    group_signature = _semantic_group_signature(
        event_type=event_type,
        app_family=app_family,
        subject=subject,
        evidence=evidence,
        observed_start=observed_start,
    )
    group_key = "grp_" + _hash(f"{group_signature}|{item_key}")[:24]
    state = "pending"
    reason_codes = [
        f"possible_{event_type}_occurrence",
        CONFIRMATION_POLICY_EVALUATED_REASON,
        *region_reasons,
    ]
    confidence = 0.55
    direct_submit_evidence = event_type in {
        "application",
        "submission",
        "send",
        "purchase",
        "publish",
    } and (region_kind == "submit_area" or bool(DIRECT_REASON_CODES.intersection(region_reasons)))
    realtime_event_evidence = (
        event_type in {"meeting", "call"}
        and any(term in app_family.lower() for term in REALTIME_APP_TERMS)
        and bool(REALTIME_EVENT_STATE_RE.search(text))
    )
    policy_filter_reasons = _confirmation_policy_filter_reasons(
        event_type=event_type,
        app_family=app_family,
        region_kind=region_kind,
        actionability=actionability,
        evidence=evidence,
        full_text=text,
        subject=subject,
        # A missing company/object is an uncertainty to ask about, not a reason
        # to silently drop a high-impact real-world event candidate.
        require_subject=False,
        independent_execution_evidence=(
            direct_submit_evidence
            or realtime_event_evidence
            or bool(independent_artifact_signals)
        ),
    )
    if policy_filter_reasons:
        state = "filtered"
        confidence = 0.05
        reason_codes.extend(policy_filter_reasons)
    elif direct_submit_evidence:
        # Submit-area/app/window/screen heuristics may nominate a high-priority
        # question, but cannot prove the real-world action.  Keep the candidate
        # pending until an explicit user answer or governed controlled evidence
        # arrives.
        state = "pending"
        confidence = min(0.75, max(confidence, 0.65))
        reason_codes.append("direct_submit_screen_signal_requires_confirmation")
    elif realtime_event_evidence:
        state = "pending"
        confidence = min(0.75, max(confidence, 0.65))
        reason_codes.append("realtime_screen_signal_requires_confirmation")
    calibration = conn.execute(
        "SELECT * FROM activity_confirmation_calibrations WHERE calibration_key=?",
        (calibration_key,),
    ).fetchone()
    if calibration and not calibration["revoked_at"]:
        generalized_negative_count = int(calibration["negative_count"] or 0) + int(
            calibration["misrecognized_count"] or 0
        )
        consistent_reason, consistent_count = _consistent_negative_feedback(conn, calibration_key)
        if state == "pending" and generalized_negative_count:
            confidence = max(0.05, confidence - min(0.15, generalized_negative_count * 0.08))
            reason_codes.append("calibration_negative_feedback_caution")
            if consistent_reason and consistent_count >= 2:
                if event_type in HIGH_IMPACT_EVENT_TYPES:
                    reason_codes.extend(
                        [
                            "calibration_high_impact_review_required",
                            f"calibration_reason_{consistent_reason}",
                        ]
                    )
                else:
                    state = "rejected"
                    reason_codes.extend(["calibration_similar_suppressed", f"calibration_reason_{consistent_reason}"])
    deletion_group_id = safe_decision_text(activity.get("deletion_group_id") or "", max_len=120)
    observation = safe_decision_text(summary or cleaned or f"{app_family} 中出现可能的{event_label}信息。", max_len=420)
    # This is still post-firewall cleaned text only. The larger bound avoids
    # turning the folded "complete cleaned content" UI into another summary.
    cleaned_content = safe_decision_text("\n".join(cleaned_parts), max_len=1600)
    priority = "high" if event_type in HIGH_IMPACT_EVENT_TYPES else "normal"
    signal_types = [
        "post_firewall_activity",
        "explicit_occurrence_language",
        *independent_artifact_signals,
    ]
    if actionability == "context_only" and state == "pending":
        priority = "low"
        confidence = min(confidence, 0.35)
        reason_codes.append("context_only_high_recall_review")
    if direct_submit_evidence:
        signal_types.append("direct_submit_evidence")
    if realtime_event_evidence:
        signal_types.append("direct_realtime_event_evidence")
    if not _subject_is_identified(subject):
        signal_types.append("anonymous_subject")
    mode = get_activity_confirmation_mode(conn)
    return _finalize_confirmation_candidate({
        "item_key": item_key,
        "event_type": event_type,
        "safe_observation": observation,
        "decision_question": _decision_question(subject, observed_start),
        "why_uncertain": "这项判断只来自本机已清洗文字，需要你确认现实中是否真的发生。",
        "app_family": app_family,
        "observed_start": observed_start,
        "observed_end": observed_end,
        "confidence": round(confidence, 3),
        "reason_codes": _unique(reason_codes)[:12],
        "region_kind": region_kind,
        "actionability": actionability,
        "source_activity_ids": [source_activity_id] if source_activity_id else [],
        "deletion_group_ids": [deletion_group_id] if deletion_group_id else [],
        "fingerprint": fingerprint,
        "calibration_key": calibration_key,
        "group_key": group_key,
        "group_signature": group_signature,
        "subject": subject,
        "evidence": evidence,
        "cleaned_content": cleaned_content,
        "question_kind": "occurrence",
        "candidate_origin": "explicit_claim",
        "priority": priority,
        "episode_id": "",
        "safe_signal_types": _unique(signal_types),
        "suggested_workstream": _suggested_workstream(subject, text),
        "suggested_outcome": _suggested_outcome(event_type),
        "notification_eligible": _notification_eligible(mode, priority),
        "state": state,
    })


def _confirmation_policy_filter_reasons(
    *,
    event_type: str,
    app_family: str,
    region_kind: str,
    actionability: str,
    evidence: str,
    full_text: str,
    subject: dict[str, Any],
    require_subject: bool = True,
    independent_execution_evidence: bool = False,
    context_note: str = "",
) -> list[str]:
    reasons: list[str] = []
    combined = safe_decision_text(" ".join(_unique([evidence, full_text])), max_len=1800)
    conditional_scope = safe_decision_text(evidence, max_len=600)
    for fragment in re.split(r"[\r\n。！？!?；;]+", str(full_text or "")):
        safe_fragment = safe_decision_text(fragment, max_len=900)
        if conditional_scope and conditional_scope in safe_fragment:
            conditional_scope = safe_fragment
            break
    if CONFIRMATION_UI_RE.search(combined):
        reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_self_ui")
    if META_DISCUSSION_RE.search(combined):
        reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_meta_discussion")
    reasons.extend(
        _confirmation_semantic_filter_reasons(
            subject=subject,
            evidence=evidence,
            full_text=full_text,
        )
    )
    # Evaluate the sentence that actually triggered this candidate. A separate
    # nearby product-description sentence must not suppress a concrete, explicit
    # completion statement from the same cleaned activity.
    if event_type == "completion" and CONDITIONAL_COMPLETION_RE.search(conditional_scope):
        reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_conditional_completion")
    if (
        NEGATED_OCCURRENCE_RE.search(conditional_scope or combined)
        or _only_negated_high_value(conditional_scope or combined)
    ):
        reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_negated_occurrence")
    if (
        GAME_CONTEXT_NOTE_RE.search(context_note or "")
        and (
            (_game_app_hint(app_family) and _game_ui_signal_count(combined) >= 1)
            or _looks_like_game_ui(app_family, combined)
        )
    ):
        # The note is context guidance only: it can help classify an already
        # game-looking surface as non-real-world UI, but it never participates
        # in occurrence matching or chooses occurred/not_occurred.
        reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_user_context_only")
    elif _looks_like_game_ui(app_family, combined):
        reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_game_ui")
    if (
        not independent_execution_evidence
        and _is_ai_tool_app(app_family)
        and AI_PLAN_OR_SUMMARY_RE.search(combined)
        and not (
            event_type == "completion"
            and _looks_like_generated_document_claim(
                app_family=app_family,
                object_label=safe_decision_text(subject.get("object") or "", max_len=180),
                evidence=evidence,
                full_text=full_text,
            )
        )
    ):
        reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_ai_plan_or_summary")
    if _looks_like_tool_status(app_family, combined):
        reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_tool_status")
    if region_kind in NON_ACTIONABLE_REGION_KINDS:
        reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_non_actionable_region")
    if actionability in NON_ACTIONABLE_ACTIONABILITY and not (
        actionability == "context_only" and event_type in HIGH_IMPACT_EVENT_TYPES
    ):
        reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_non_actionable_actionability")
    if (
        require_subject
        and event_type not in HIGH_IMPACT_EVENT_TYPES
        and not _subject_is_identified(subject)
    ):
        reasons.append(f"{CONFIRMATION_POLICY_REASON_PREFIX}_missing_subject")
    return _unique(reasons)


def _only_negated_high_value(value: str) -> bool:
    relevant: list[str] = []
    for fragment in re.split(r"[\r\n。！？!?；;]+", str(value or "")):
        safe_fragment = safe_decision_text(fragment, max_len=700)
        if not safe_fragment:
            continue
        if any(
            pattern.search(safe_fragment)
            for pattern in (
                WEAK_INTERVIEW_RE,
                WEAK_MEETING_RE,
                WEAK_CALL_RE,
                APPLICATION_ACTION_RE,
                SUBMISSION_ACTION_RE,
                SEND_ACTION_RE,
                PUBLISH_ACTION_RE,
            )
        ):
            relevant.append(safe_fragment)
    return bool(relevant) and all(
        HIGH_VALUE_NEGATION_RE.search(fragment)
        or NEGATED_OCCURRENCE_RE.search(fragment)
        for fragment in relevant
    )


def _looks_like_game_ui(app_family: str, value: str) -> bool:
    signals = [bool(pattern.search(value or "")) for pattern in GAME_UI_SIGNAL_RES]
    signal_count = sum(signals)
    distinctive_signal = any(signals[:3])
    if not distinctive_signal:
        return False
    if _game_app_hint(app_family):
        return signal_count >= 2
    # Unknown app attribution is common around full-screen games.  Without an
    # app hint, demand a substantially stronger combination and at least XP or
    # an equipment/blueprint term to avoid filtering ordinary work tasks.
    return signal_count >= 3 and bool(signals[0] or signals[2])


def _game_ui_signal_count(value: str) -> int:
    return sum(bool(pattern.search(value or "")) for pattern in GAME_UI_SIGNAL_RES)


def _game_app_hint(app_family: str) -> bool:
    compact = re.sub(r"[^0-9a-z]+", "", str(app_family or "").casefold())
    return any(
        token in compact
        for token in (
            "codblkop",
            "callofduty",
            "blackops",
            "steam",
            "battlenet",
            "epicgames",
        )
    )


def _looks_like_tool_status(app_family: str, value: str) -> bool:
    compact_app = re.sub(r"[^0-9a-z]+", "", str(app_family or "").casefold())
    known_tool = any(
        token in compact_app
        for token in (
            "ticktick", "chatgpt", "openai", "hermes", "workbuddy", "codex", "claude", "finder",
        )
    )
    if STRONG_TOOL_STATUS_RE.search(value or ""):
        return True
    if known_tool and TOOL_STATUS_RE.search(value or ""):
        return True
    # A leading status label in a task/file/tool surface has no actor or
    # occurrence context and is treated as UI state, not a real-world fact.
    return bool(
        known_tool
        and re.match(r"^\s*(?:已完成|已做完|completed\b|finished\b)", value or "", re.I)
        and not re.search(r"(?:今天|今日|刚刚|刚才|我|本人|Alex|Local Owner)", value or "", re.I)
    )


def _is_ai_tool_app(app_family: str) -> bool:
    compact = re.sub(r"[^0-9a-z]+", "", str(app_family or "").casefold())
    return any(
        token in compact
        for token in ("chatgpt", "openai", "hermes", "workbuddy", "codex", "claude")
    )


def _is_ai_agent_terminal_app(app_family: str) -> bool:
    # Terminal emulators are where CLI AI agents (Codex, Claude Code, …) run;
    # their on-screen claims describe agent work, not the owner's own actions.
    # AI chat apps are identified separately by _is_ai_tool_app; both surfaces
    # use the same evidence requirement in the shared question gate.
    compact = re.sub(r"[^0-9a-z]+", "", str(app_family or "").casefold())
    return any(
        token in compact
        for token in (
            "terminal",
            "iterm",
            "warp",
            "ghostty",
            "kitty",
            "alacritty",
            "wezterm",
        )
    )


def _event_match(text: str | list[str]) -> tuple[str, str, str] | None:
    matches = _event_matches(text)
    return matches[0] if matches else None


def _event_matches(text: str | list[str]) -> list[tuple[str, str, str]]:
    parts = [text] if isinstance(text, str) else text
    sentences: list[str] = []
    for part in parts:
        for fragment in SENTENCE_SPLIT_RE.split(str(part or "")):
            # Match before truncating.  A long post-firewall prefix must not
            # push the actual occurrence verb out of the stored evidence.
            sentence = str(fragment or "").strip()
            if sentence and sentence not in sentences:
                sentences.append(sentence)
    matches: list[tuple[str, str, str]] = []
    for sentence in sentences:
        interview_context = bool(re.search(r"(?:面试|初面|[一二三四五六123456]面|复试|终面|\binterview\b)", sentence, re.I))
        interview_excluded = bool(
            interview_context
            and (
                INTERVIEW_PREPARATION_RE.search(sentence)
                or INTERVIEW_FUTURE_RE.search(sentence)
                or INTERVIEW_PAST_REFERENCE_RE.search(sentence)
                or INTERVIEW_META_INFERENCE_RE.search(sentence)
                or INTERVIEW_META_LANGUAGE_RE.search(sentence)
            )
        )
        for event_type, label, patterns in EVENT_RULES:
            if event_type == "interview" and interview_excluded:
                continue
            if event_type == "completion" and interview_excluded:
                # "完成面试准备" is a preparation statement, not evidence
                # that either the interview or another real-world event ended.
                continue
            pattern_match = next(
                (candidate for pattern in patterns if (candidate := pattern.search(sentence))),
                None,
            )
            if pattern_match:
                evidence = _matched_evidence_excerpt(sentence, pattern_match)
                match = (event_type, label, evidence)
                if match not in matches:
                    matches.append(match)
                break
    return matches


def _matched_evidence_excerpt(value: str, match: re.Match[str], *, max_len: int = 280) -> str:
    """Return bounded post-firewall context centered on the real regex match."""
    context = max(24, (max_len - (match.end() - match.start())) // 2)
    start = max(0, match.start() - context)
    end = min(len(value), match.end() + context)
    excerpt = value[start:end]
    cleaned = safe_decision_text(excerpt, max_len=max_len)
    matched_text = safe_decision_text(match.group(0), max_len=max_len)
    if matched_text and matched_text not in cleaned:
        # Defensive fallback for an unexpectedly aggressive sanitizer.
        return matched_text
    return cleaned


def _region_signal(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    if not isinstance(value, dict):
        value = {}
    return {
        "region_kind": safe_decision_text(value.get("region_kind") or "unknown", max_len=60),
        "actionability": safe_decision_text(value.get("actionability") or "unknown", max_len=60),
        "reason_codes": _safe_tokens(value.get("reason_codes")),
    }


def _public_item(
    row: sqlite3.Row | None,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if row is None:
        return {}
    item = dict(row)
    low_value_question = (
        str(item.get("state") or "pending") in UNRESOLVED_STATES
        and _confirmation_group_question_assessment([item])["reason"]
        in LOW_VALUE_QUESTION_REASONS
    )
    evidence = (
        evidence_summary(conn, [str(item.get("item_key") or "")])
        if conn is not None
        else {"evidence_status": "unavailable", "evidence_count": 0, "available_source_count": 0}
    )
    return {
        "item_key": str(item.get("item_key") or ""),
        "event_type": str(item.get("event_type") or ""),
        "question_kind": str(item.get("question_kind") or "occurrence"),
        "candidate_origin": str(item.get("candidate_origin") or "explicit_claim"),
        "priority": "low" if low_value_question else str(item.get("priority") or "normal"),
        "episode_id": str(item.get("episode_id") or ""),
        "safe_signal_types": _safe_tokens(item.get("safe_signal_types")),
        "suggested_workstream": safe_decision_text(item.get("suggested_workstream") or "", max_len=120),
        "suggested_outcome": _normalized_outcome(item.get("suggested_outcome") or "unknown"),
        "user_workstream": safe_decision_text(item.get("user_workstream") or "", max_len=120),
        "user_outcome": (
            _normalized_outcome(item.get("user_outcome"))
            if str(item.get("user_outcome") or "")
            else ""
        ),
        "notification_eligible": not low_value_question and bool(int(item.get("notification_eligible") or 0)),
        "group_key": str(item.get("group_key") or ""),
        "group_signature": str(item.get("group_signature") or ""),
        "subject": _json_object(item.get("subject")),
        "subject_label": safe_decision_text(
            _json_object(item.get("subject")).get("label") or "",
            max_len=100,
        ),
        "trigger_sentence": safe_decision_text(item.get("evidence") or "", max_len=280),
        "safe_observation": safe_decision_text(item.get("safe_observation") or "", max_len=420),
        "decision_question": "" if low_value_question else safe_decision_text(item.get("decision_question") or "", max_len=180),
        "why_uncertain": safe_decision_text(item.get("why_uncertain") or "", max_len=260),
        "app": _public_confirmation_app([item.get("app_family") or ""]),
        "time_label": _time_label(str(item.get("observed_start") or ""), str(item.get("observed_end") or "")),
        "observed_start": str(item.get("observed_start") or ""),
        "observed_end": str(item.get("observed_end") or ""),
        "confidence": float(item.get("confidence") or 0.0),
        "reason_codes": _json_list(item.get("reason_codes")),
        "state": str(item.get("state") or "pending"),
        "answer_options": [] if low_value_question else ANSWER_OPTIONS,
        "user_answer": str(item.get("answer") or ""),
        "answer_reason": str(item.get("answer_reason") or ""),
        "generalize_scope": str(item.get("generalize_scope") or "exact"),
        "answered_at": str(item.get("answered_at") or ""),
        "clarification": _public_clarification(item.get("clarification")),
        **evidence,
    }


def _public_calibration(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    negative = int(item.get("negative_count") or 0) + int(item.get("misrecognized_count") or 0)
    positive = int(item.get("positive_count") or 0)
    consistent_reason, consistent_count = _consistent_negative_feedback(
        conn,
        str(item.get("calibration_key") or ""),
    )
    revoked = bool(item.get("revoked_at"))
    auto_suppressed = bool(
        not revoked
        and bool(consistent_reason)
        and consistent_count >= 2
        and str(item.get("event_type") or "") not in HIGH_IMPACT_EVENT_TYPES
    )
    event_label = {
        "interview": "面试类信号",
        "meeting": "会议类信号",
        "application": "投递类信号",
        "submission": "提交类信号",
        "send": "发送类信号",
        "call": "通话类信号",
        "completion": "完成类信号",
        "purchase": "购买类信号",
        "publish": "发布类信号",
    }.get(str(item.get("event_type") or ""), "活动信号")
    status = "已撤销" if revoked else "自动降级中" if auto_suppressed else "仅用于校准"
    recent = conn.execute(
        """
        SELECT answer,answer_reason,generalize_scope
        FROM activity_confirmation_items
        WHERE calibration_key=? AND answer IN ('occurred','not_occurred','misrecognized')
        ORDER BY datetime(answered_at) DESC LIMIT 1
        """,
        (str(item.get("calibration_key") or ""),),
    ).fetchone()
    return {
        "calibration_key": str(item.get("calibration_key") or ""),
        "label": event_label,
        "summary": f"负反馈 {negative} 次，正反馈 {positive} 次；{status}。",
        "negative_count": negative,
        "positive_count": positive,
        "auto_suppressed": auto_suppressed,
        "revoked": revoked,
        "answer": str((recent["answer"] if recent else item.get("last_answer")) or ""),
        "reason": str((recent["answer_reason"] if recent else item.get("last_reason")) or ""),
        "generalize": str((recent["generalize_scope"] if recent else "similar") or "similar"),
        "created_at": str(item.get("created_at") or ""),
        "can_revoke": not revoked,
        "updated_at": str(item.get("updated_at") or ""),
    }


def _event_label(event_type: str) -> str:
    return {
        "interview": "面试",
        "meeting": "会议",
        "application": "投递或申请",
        "submission": "提交",
        "send": "发送",
        "call": "通话",
        "completion": "完成事项",
        "purchase": "购买",
        "publish": "发布",
        "business_context": "业务工作片段",
        "manual": "手工确认活动",
        "preparation": "准备活动",
    }.get(str(event_type or ""), "活动")


def _suggested_outcome(event_type: str) -> str:
    if event_type in {"completion", "application", "submission", "send", "publish", "purchase"}:
        return "completed"
    if event_type in {"interview", "meeting", "call"}:
        return "progressing"
    return "unknown"


def _normalized_outcome(value: Any) -> str:
    outcome = str(value or "unknown").strip()
    outcome = {
        "progressed": "progressing",
        "preparation": "preparation_only",
    }.get(outcome, outcome)
    return outcome if outcome in OUTCOME_VALUES else "unknown"


def _subject_for_event(
    event_type: str,
    event_label: str,
    evidence: str,
    full_text: str,
) -> dict[str, Any]:
    if event_type != "interview":
        object_label = _extract_event_object(event_type, evidence)
        if not object_label and event_type == "completion":
            object_label = _generated_document_object(full_text)
        label = f"{object_label}的{event_label}" if object_label else event_label
        return _organized_confirmation_subject(
            event_type,
            {
                "event_label": event_label or _event_label(event_type),
                "company": "",
                "round": "",
                "object": object_label,
                "label": label or _event_label(event_type),
                "company_identified": False,
                "round_identified": False,
                "object_identified": bool(object_label),
                "object_ambiguous": False,
            },
            evidence=evidence,
        )
    searchable = " ".join(_unique([evidence, full_text]))
    round_label = _extract_interview_round(searchable)
    company = _extract_interview_company(evidence or searchable)
    if not company and evidence != searchable:
        company = _extract_interview_company(searchable)
    if company and round_label:
        label = f"{company}{round_label}面试"
    elif company:
        label = f"{company}面试（轮次未识别）"
    elif round_label:
        label = f"公司未识别的{round_label}面试"
    else:
        label = "具体公司和轮次未识别的面试"
    return {
        "event_label": "面试",
        "company": company,
        "round": round_label,
        "object": label if company or round_label else "",
        "label": label,
        "company_identified": bool(company),
        "round_identified": bool(round_label),
        "object_identified": bool(company or round_label),
    }


def _extract_event_object(event_type: str, evidence: str) -> str:
    """Extract a conservative concrete object adjacent to the matched verb."""
    named_object_patterns: tuple[str, ...] = ()
    if event_type == "meeting":
        meeting_head = (
            r"(?:会议|周会|例会|会谈|沟通会|复盘会|评审会|同步会|"
            r"启动会|讨论会|交流会|\bmeeting\b|\bstandup\b|\bsync\b)"
        )
        named_object_patterns = (
            rf"(?:参加|加入|进行|召开|完成|结束)(?:了|过)?\s*"
            rf"(?:一场|一次|一个)?\s*"
            rf"([A-Za-z0-9&.+\-\u4e00-\u9fff ]{{2,40}}?{meeting_head})",
            rf"([A-Za-z0-9&.+\-\u4e00-\u9fff ]{{2,40}}?{meeting_head})\s*"
            r"(?:已|已经)?(?:进行|完成|结束|started|ended)",
        )
    elif event_type == "call":
        call_head = r"(?:通话|电话|连线|电话会|\bcall\b)"
        named_object_patterns = (
            rf"(?:参加|进行|拨打|完成|结束)(?:了|过)?\s*"
            rf"(?:一次|一个)?\s*"
            rf"([A-Za-z0-9&.+\-\u4e00-\u9fff ]{{2,40}}?{call_head})",
            rf"([A-Za-z0-9&.+\-\u4e00-\u9fff ]{{2,40}}?{call_head})\s*"
            r"(?:已|已经)?(?:进行|完成|结束|started|ended)",
        )
    for pattern in named_object_patterns:
        named_match = re.search(pattern, evidence or "", re.I)
        if named_match:
            return safe_decision_text(
                named_match.group(1).strip(" ·•:：-_—&|()[]（）"),
                max_len=72,
            )
    patterns = next((rules for kind, _, rules in EVENT_RULES if kind == event_type), ())
    match = next(
        (candidate for pattern in patterns if (candidate := pattern.search(evidence or ""))),
        None,
    )
    if not match:
        return ""
    before = (evidence or "")[: match.start()]
    after = (evidence or "")[match.end() :]
    candidates = (after, before)
    for raw in candidates:
        candidate = safe_decision_text(raw, max_len=100)
        candidate = re.split(
            r"(?:然后|接下来|随后|并且|因此|真的|请确认|可以|需要|"
            r"\b(?:and then|then|please|therefore)\b)",
            candidate,
            maxsplit=1,
            flags=re.I,
        )[0]
        candidate = re.sub(
            r"^(?:(?:今天|今日|刚刚|刚才|已经|我|本人|Alex|Local Owner|给|将|把|"
            r"the|an?|to)\s*)+",
            "",
            candidate,
            flags=re.I,
        )
        candidate = re.sub(r"(?:\b\d{1,2}:\d{2}\b|\b\d+\s*/\s*\d+\b)", "", candidate)
        candidate = safe_decision_text(candidate.strip(" ··•:：-_—&|()[]（）"), max_len=72)
        normalized = _normalized_phrase(candidate)
        generic = {
            "", "事项", "完成事项", "任务", "项目", "工作", "操作", "处理", "报告", "邮件", "消息",
            "申请", "提交", "发送", "购买", "发布", "测试", "构建", "打包", "安装", "部署",
            "task", "tasks", "project", "work", "report", "email", "message", "test", "tests", "build",
        }
        if normalized and normalized not in {_normalized_phrase(value) for value in generic}:
            if len(normalized) >= 3 and not normalized.isdigit():
                return candidate
    return ""


def _subject_is_identified(subject: dict[str, Any]) -> bool:
    return bool(
        subject.get("object_identified")
        or subject.get("company")
        or subject.get("round")
        or subject.get("object")
    )


def _subject_identity(
    subject: dict[str, Any],
    event_type: str,
    evidence: str = "",
) -> tuple[str, ...]:
    if event_type == "interview":
        return (
            _normalized_phrase(subject.get("company")),
            _normalized_phrase(subject.get("round")),
        )
    return (
        _normalized_phrase(
            _canonical_event_object(
                event_type,
                subject,
                evidence,
            )
        ),
    )


def _subject_has_identity(subject: dict[str, Any], event_type: str) -> bool:
    return any(_subject_identity(subject, event_type))


def _extract_interview_round(value: str) -> str:
    patterns = (
        (
            r"(?:初面|初次(?:面试)?|首轮(?:面试)?|一面|第一(?:轮|次)|第1(?:轮|次)|"
            r"\bfirst\s+(?:round\s+)?interview\b)",
            "第一轮",
        ),
        (r"(?:二面|第二(?:轮|次)|第2(?:轮|次)|\bsecond\s+(?:round\s+)?interview\b)", "第二轮"),
        (r"(?:三面|第三(?:轮|次)|第3(?:轮|次)|\bthird\s+(?:round\s+)?interview\b)", "第三轮"),
        (r"(?:四面|第四(?:轮|次)|第4(?:轮|次)|\bfourth\s+(?:round\s+)?interview\b)", "第四轮"),
        (r"(?:五面|第五(?:轮|次)|第5(?:轮|次)|\bfifth\s+(?:round\s+)?interview\b)", "第五轮"),
        (r"(?:六面|第六(?:轮|次)|第6(?:轮|次)|\bsixth\s+(?:round\s+)?interview\b)", "第六轮"),
        (r"(?:复试|\bfollow[- ]?up\s+interview\b)", "复试"),
        (r"(?:终面|最终面试|\bfinal\s+(?:round\s+)?interview\b)", "终面"),
    )
    for pattern, label in patterns:
        if re.search(pattern, value or "", re.I):
            return label
    return ""


def _extract_interview_company(value: str) -> str:
    text = safe_decision_text(value, max_len=360)
    term_matches = list(
        re.finditer(r"(?:初面|[一二三四五六123456]面|复试|终面|面试|\binterview\b)", text, re.I)
    )
    if not term_matches:
        return ""
    prefix = text[max(0, term_matches[0].start() - 60) : term_matches[0].start()].strip()
    prefix = re.sub(
        r"(?:初次|首轮|第?[一二三四五六123456](?:轮|次)?|初|复|终|"
        r"first|second|third|fourth|fifth|sixth|final)"
        r"(?:轮)?(?:业务|技术|HR|hr|人事|招聘|round)?$",
        "",
        prefix,
        flags=re.I,
    ).strip()
    action_matches = list(
        re.finditer(
            r"(?:参加了?|进行了?|完成了?|结束了?|面完了?|已面试|已通过|通过了?|"
            r"attended|completed|finished|had)\s*",
            prefix,
            re.I,
        )
    )
    if action_matches:
        prefix = prefix[action_matches[-1].end() :].strip()
    prefix = re.sub(r"正在\s*$", "", prefix).strip()
    prefix = re.sub(
        r"^(?:(?:今天|今日|刚刚|已经|实际|本人|我的?|Alex|Local Owner|于|在|"
        r"一场|这场|一(?:次|个)|某(?:次|个)|an?|the|线上|线下|远程|现场|电话|视频|"
        r"技术|业务|招聘|人事|HR|hr|产品)\s*)+",
        "",
        prefix,
        flags=re.I,
    ).strip()
    prefix = re.sub(
        r"(?:线上|线下|远程|现场|电话|视频|技术|业务|招聘|人事|HR|hr|产品)+$",
        "",
        prefix,
        flags=re.I,
    ).strip()
    prefix = re.sub(
        r"(?:海外|国际|全球|本地化|区域)?\s*"
        r"(?:销售|渠道|商务|市场|运营|产品|BD|GTM|sales|channel)\s*$",
        "",
        prefix,
        flags=re.I,
    ).strip()
    prefix = re.sub(r"(?:的|公司|集团)\s*$", "", prefix).strip()
    candidates = re.findall(
        r"(?:[A-Za-z][A-Za-z0-9&.+-]*(?:\s+[A-Za-z0-9&.+-]+){0,3}|[\u4e00-\u9fff·]{2,14})",
        prefix,
    )
    if not candidates:
        return ""
    company = safe_decision_text(candidates[-1], max_len=40).strip(" ·.-")
    lowered = company.casefold()
    blocked = {
        "今天",
        "今日",
        "刚刚",
        "已经",
        "实际",
        "一场",
        "这场",
        "面试",
        "公司",
        "具体公司",
        "interview",
        "技术",
        "技术面",
        "线上",
        "线下",
        "远程",
        "现场",
        "电话",
        "视频",
        "业务",
        "招聘",
        "人事",
        "hr",
        "产品",
        "一个",
        "某个",
        "technical",
        "online",
        "remote",
        "phone",
        "video",
        "business",
        "recruiting",
    }
    if lowered in {value.casefold() for value in blocked}:
        return ""
    return company


def _decision_question(subject: dict[str, Any], observed_start: str) -> str:
    event_label = safe_decision_text(subject.get("event_label") or "", max_len=40)
    event_type = {
        "面试": "interview",
        "会议": "meeting",
        "投递或申请": "application",
        "提交": "submission",
        "发送": "send",
        "通话": "call",
        "完成事项": "completion",
        "购买": "purchase",
        "发布": "publish",
    }.get(event_label, "")
    object_label = _canonical_event_object(event_type, subject)
    if not object_label:
        return ""
    action = {
        "interview": "参加了",
        "meeting": "参加了",
        "application": "投递了",
        "submission": "提交了",
        "send": "发送了",
        "call": "进行了",
        "completion": "完成了",
        "purchase": "购买了",
        "publish": "发布了",
    }.get(event_type, "完成了")
    separator = " " if re.match(r"[A-Za-z0-9]", object_label) else ""
    return f"{_date_label(observed_start)}，你是否{action}{separator}{object_label}？"


def _semantic_group_signature(
    *,
    event_type: str,
    app_family: str,
    subject: dict[str, Any],
    evidence: str,
    observed_start: str,
) -> str:
    raw = "|".join(
        (
            _local_day(observed_start),
            event_type,
            _group_time_bucket(event_type, observed_start),
            *(_subject_identity(subject, event_type, evidence) or ("unknown",)),
        )
    )
    return "sig_" + _hash(raw)[:24]


def _group_time_bucket(event_type: str, observed_start: str) -> str:
    if event_type not in REPEATABLE_EVENT_TYPES:
        return "all_day"
    try:
        return _local_datetime(observed_start).replace(minute=0, second=0, microsecond=0).isoformat(
            timespec="hours"
        )
    except ValueError:
        return "unknown_hour"


def _evidence_semantics(value: str) -> str:
    codes: list[str] = []
    checks = (
        (r"(?:参加|进行|attended|had\s+(?:an?\s+)?interview)", "attended"),
        (r"(?:完成|结束|面完|completed|finished|ended)", "completed"),
        (r"(?:已通过|通过了|passed)", "passed"),
        (r"(?:已发生|took\s+place)", "occurred"),
    )
    for pattern, code in checks:
        if re.search(pattern, value or "", re.I):
            codes.append(code)
    return "+".join(codes) if codes else _normalized_evidence(value)


def _normalized_evidence(value: str) -> str:
    normalized = str(value or "").casefold()
    normalized = re.sub(r"\d{1,4}[-/:.年]\d{1,2}(?:[-/:.月]\d{1,4})?", "#date", normalized)
    normalized = re.sub(r"\b\d{1,2}:\d{2}\b", "#time", normalized)
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff#]+", "", normalized)
    return safe_decision_text(normalized, max_len=260)


def _normalized_phrase(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def _normalized_app_family(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().casefold())
    aliases = {
        "google chrome": "chrome",
        "com.google.chrome": "chrome",
        "chatgpt desktop": "chatgpt",
        "com.openai.chat": "chatgpt",
    }
    return aliases.get(normalized, normalized or "unknown")


def _unknown_app(value: str) -> bool:
    return value in {"", "unknown", "未知", "未识别"}


def _is_generic_accessibility_services_app(value: str) -> bool:
    compact = re.sub(r"[^0-9a-z]+", "", str(value or "").casefold())
    return compact in {
        "accessibilityservices",
        "comappleaccessibilityaxvisualsupportagent",
    }


def _public_confirmation_app(values: list[Any]) -> str:
    """Prefer a real app and never expose generic AX attribution as identity."""
    apps = _unique(
        [safe_decision_text(value or "", max_len=80) for value in values]
    )
    known_apps = [
        app
        for app in apps
        if not _unknown_app(_normalized_app_family(app))
        and not _is_generic_accessibility_services_app(app)
    ]
    if known_apps:
        return known_apps[0]
    if any(_is_generic_accessibility_services_app(app) for app in apps):
        return "系统辅助识别"
    return apps[0] if apps else "unknown"


def _is_personal_ai_monitor_app(value: str) -> bool:
    compact = re.sub(r"[^0-9a-z]+", "", str(value or "").casefold())
    return compact in {
        "personalaimonitor",
        "personalaimonitormenubar",
        "apppersonalaimonitor",
        "comexampleuserpersonalaimonitor",
        "comexampleuserpersonalaimonitormenubar",
    }


def _local_day(value: str) -> str:
    try:
        return _local_datetime(value).date().isoformat()
    except ValueError:
        return str(value or "")[:10]


def _affected_local_day_windows(start: str, end: str) -> list[tuple[str, str]]:
    """Return full local-day windows touched by an incremental interval."""
    try:
        first = _local_datetime(start)
        exclusive_end = _local_datetime(end)
    except (TypeError, ValueError):
        return [(start, end)]
    if exclusive_end <= first:
        exclusive_end = first
    # ``end`` is exclusive throughout the timeline API.  Keep an exact local
    # midnight from spuriously pulling the following day into a regroup.
    last = exclusive_end - timedelta(microseconds=1) if exclusive_end > first else first
    cursor = first.replace(hour=0, minute=0, second=0, microsecond=0)
    last_day = last.replace(hour=0, minute=0, second=0, microsecond=0)
    windows: list[tuple[str, str]] = []
    while cursor <= last_day:
        next_day = cursor + timedelta(days=1)
        windows.append(
            (
                cursor.isoformat(timespec="seconds"),
                next_day.isoformat(timespec="seconds"),
            )
        )
        cursor = next_day
    return windows


def _within_group_window(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        left_start = _local_datetime(str(left.get("observed_start") or ""))
        left_end = _local_datetime(str(left.get("observed_end") or left.get("observed_start") or ""))
        right_start = _local_datetime(str(right.get("observed_start") or ""))
        right_end = _local_datetime(str(right.get("observed_end") or right.get("observed_start") or ""))
    except ValueError:
        return False
    if left_start > right_start:
        left_start, right_start = right_start, left_start
        left_end, right_end = right_end, left_end
    return right_start <= max(left_end, left_start) + timedelta(minutes=30)


def _within_completion_ocr_window(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        left_start = _local_datetime(str(left.get("observed_start") or ""))
        right_start = _local_datetime(str(right.get("observed_start") or ""))
    except ValueError:
        return False
    return abs(right_start - left_start) <= timedelta(minutes=15)


def _intervals_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        left_start = _local_datetime(str(left.get("observed_start") or ""))
        left_end = _local_datetime(str(left.get("observed_end") or left.get("observed_start") or ""))
        right_start = _local_datetime(str(right.get("observed_start") or ""))
        right_end = _local_datetime(str(right.get("observed_end") or right.get("observed_start") or ""))
    except ValueError:
        return False
    return left_start <= right_end and right_start <= left_end


def _group_day_window(rows: list[dict[str, Any]]) -> tuple[str, str]:
    observed = min(_local_datetime(str(row.get("observed_start") or "")) for row in rows)
    start = observed.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def _clarification_context_window(rows: list[dict[str, Any]]) -> tuple[str, str]:
    start = min(_local_datetime(str(row.get("observed_start") or "")) for row in rows)
    end = max(
        _local_datetime(str(row.get("observed_end") or row.get("observed_start") or ""))
        for row in rows
    )
    return (
        (start - timedelta(minutes=15)).isoformat(timespec="seconds"),
        (end + timedelta(minutes=15)).isoformat(timespec="seconds"),
    )


def _confirmation_source_is_live(conn: sqlite3.Connection, row: Any) -> bool:
    """Fail closed for source-bound rows left behind by older privacy code.

    Legacy rows with no provenance identifiers keep their historical behavior;
    source-bound rows must still resolve to at least one current activity.
    """
    source_ids = _json_list(row["source_activity_ids"])
    deletion_group_ids = _json_list(row["deletion_group_ids"])
    if not source_ids and not deletion_group_ids:
        return True
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        if conn.execute(
            f"SELECT 1 FROM extracted_activities WHERE id IN ({placeholders}) LIMIT 1",
            tuple(source_ids),
        ).fetchone():
            return True
    if deletion_group_ids:
        placeholders = ",".join("?" for _ in deletion_group_ids)
        if conn.execute(
            f"SELECT 1 FROM extracted_activities "
            f"WHERE deletion_group_id IN ({placeholders}) LIMIT 1",
            tuple(deletion_group_ids),
        ).fetchone():
            return True
    return False


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            parsed = {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _public_clarification(value: Any) -> dict[str, Any]:
    item = _json_object(value)
    if item.get("status") != "completed":
        return {"status": "not_requested"}
    outcome = str(item.get("outcome") or "")
    allowed = {
        "rewritten": "success",
        "split": "success",
        "unchanged": "neutral",
        "insufficient_information": "warning",
        "system_filtered": "success",
        "evidence_unavailable": "warning",
    }
    if outcome not in allowed:
        return {"status": "not_requested"}
    severity = str(item.get("severity") or allowed[outcome])
    if severity != allowed[outcome]:
        return {"status": "not_requested"}
    evidence_status = str(item.get("evidence_status") or "unavailable")
    if evidence_status not in {"available", "partial", "unavailable"}:
        evidence_status = "unavailable"
    allowed_inputs = {
        "retained_frame_ocr",
        "stored_cleaned_text",
        "nearby_safe_context",
        "user_note",
    }
    return {
        "status": "completed",
        "outcome": outcome,
        "severity": severity,
        "message": safe_decision_text(item.get("message") or "", max_len=180),
        "missing_fields": _safe_tokens(item.get("missing_fields")),
        "inputs_used": [
            token for token in _safe_tokens(item.get("inputs_used")) if token in allowed_inputs
        ],
        "evidence_status": evidence_status,
        "evidence_count": max(0, int(item.get("evidence_count") or 0)),
        "available_source_count": max(0, int(item.get("available_source_count") or 0)),
        "generated_at": str(item.get("generated_at") or ""),
    }


def _safe_action_flags() -> dict[str, bool]:
    return {
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
    }


def _merged_ids(left: Any, right: Any, limit: int) -> list[str]:
    return _unique([*_json_list(left), *_json_list(right)])[:limit]


def select_confirmed_activities(activities: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Keep high-impact confirmations, then sample ordinary facts across the range."""
    if limit <= 0:
        return []
    priority = [
        item
        for item in activities
        if item.get("occurrence_state") == "user_confirmed"
        or item.get("event_type") in HIGH_IMPACT_EVENT_TYPES
    ]
    priority.sort(
        key=lambda item: (
            0 if item.get("occurrence_state") == "user_confirmed" else 1,
            str(item.get("started_at") or ""),
        )
    )
    selected = priority[:limit]
    selected_ids = {str(item.get("id") or "") for item in selected if str(item.get("id") or "")}
    remaining = [
        item
        for item in activities
        if (not str(item.get("id") or "") or str(item.get("id") or "") not in selected_ids)
        and item not in selected
    ]
    slots = limit - len(selected)
    if slots <= 0 or not remaining:
        return selected
    if len(remaining) <= slots:
        return [*selected, *remaining]
    if slots == 1:
        sampled = [remaining[-1]]
    else:
        sampled = [
            remaining[round(index * (len(remaining) - 1) / (slots - 1))]
            for index in range(slots)
        ]
    return [*selected, *sampled]


def _json_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [value]
    elif isinstance(value, (list, tuple, set)):
        parsed = list(value)
    else:
        parsed = []
    output: list[str] = []
    for item in parsed:
        safe_item = safe_decision_text(item, max_len=120)
        if safe_item:
            output.append(safe_item)
    return output


def _safe_tokens(value: Any) -> list[str]:
    tokens = _json_list(value)
    return [re.sub(r"[^a-zA-Z0-9_.+-]", "_", token)[:80].strip("_") for token in tokens if token][:12]


def _private_evidence_ids(value: Any) -> list[str]:
    """Validate internal evidence ids without imposing a public-response limit."""
    values = value if isinstance(value, (list, tuple, set)) else []
    output: list[str] = []
    for candidate in values:
        token = str(candidate or "")
        if not re.fullmatch(r"confirm_ev_(?:unavailable_)?[0-9a-f]{32}", token):
            continue
        if token not in output:
            output.append(token)
    return output


def _unique(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
    return output


def _normalized_identity(text: str, event_type: str) -> str:
    normalized = re.sub(r"\s+", "", text.lower())
    normalized = re.sub(r"\d{1,4}[-/:.]\d{1,2}(?:[-/:.]\d{1,4})?", "#date", normalized)
    return safe_decision_text(f"{event_type}:{normalized}", max_len=180)


def _time_label(start: str, end: str) -> str:
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        start_label = f"{start_dt.month}月{start_dt.day}日 {start_dt.strftime('%H:%M')}"
        if start_dt.date() == end_dt.date():
            return f"{start_label}–{end_dt.strftime('%H:%M')}"
        return f"{start_label}–{end_dt.month}月{end_dt.day}日 {end_dt.strftime('%H:%M')}"
    except ValueError:
        return ""


def _date_label(value: str) -> str:
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return f"{observed.month}月{observed.day}日"
    except ValueError:
        return "该时段"


def _consistent_negative_feedback(conn: sqlite3.Connection, calibration_key: str) -> tuple[str, int]:
    if not calibration_key:
        return "", 0
    row = conn.execute(
        "SELECT reason_counts FROM activity_confirmation_calibrations WHERE calibration_key=?",
        (calibration_key,),
    ).fetchone()
    if not row:
        return "", 0
    try:
        reason_counts = json.loads(row["reason_counts"] or "{}")
    except (TypeError, json.JSONDecodeError):
        reason_counts = {}
    for reason, raw_count in sorted(reason_counts.items(), key=lambda item: (-int(item[1] or 0), str(item[0]))):
        count = int(raw_count or 0)
        if reason in NEGATIVE_GENERALIZATION_REASONS and count >= 2:
            return reason, count
    return "", 0


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
