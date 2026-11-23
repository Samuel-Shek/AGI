"""Personal responsibility rules copied from PSILocalWeb triage.py.

Negation, conditional and technical-context guards retain the accepted behavior.
Only classification reuses closure_question; UI questions keep source wording.
"""

import hashlib
import re

from .presentation import clauses, recommendation_excerpt, short_title


TECHNICAL = re.compile(r"(?:[/\\]|[A-Z][A-Z0-9]+[-_][A-Z0-9_-]+|sqlite|traceback|launchagent|chmod|token|权限|磁盘|服务|数据库|进程|清理|删除|故障|报错|修复|配置|重启)", re.I)

CAREER = re.compile(r"求职|岗位|职位|应聘|面试|投递|招聘")

MATERIAL = re.compile(r"原始(?:录屏|录音|视频|音频|媒体|文件|材料)|录屏|录音|素材|原片")

RETENTION = re.compile(r"(?:是否|要不要|还要|需不需要|还需|留不留).*(?:保留|留存|留着)|(?:保留|留存).*(?:取舍|选择|确认|决定|[？?])")

PURPOSE = re.compile(r"用途|用来做什么|做什么用|用于什么")

PERSONAL = re.compile(r"(?:你|本人|个人).*(?:希望|愿意|偏好|选择|取舍|打算)")

CLOSURE_TECHNICAL = re.compile(r"[/\\]|[A-Z][A-Z0-9]+[-_][A-Z0-9_-]+|sqlite|traceback|launchagent|chmod|token|权限|数据库|进程|服务|故障|失败|修复|配置|重启|报错|磁盘|空间不足|容量不足|清理策略|自动清理|阈值|调试|部署|升级|安装|校验|端口|凭据|密钥", re.I)

GENERIC_ADVICE = re.compile(r"核查|核对|查证|查看|看依据|确认你确知|仅确认|请确认|先确认|确认是否|检查|分析|排查|调查|诊断|评估|交给|交由|运行|编译|调试|启动|部署|安装|升级|执行|写入|写回|如果|若|除非|当.*时|必要时|(?:不|别|无需|不要)选择")

NO_RECOMMENDATION = re.compile(r"(?:没有|暂无|尚无|无明确|不提供|未提供|不能|无法|尚未|未能|未知|待定|不确定|未确定|未决定|未形成|未给出).{0,8}(?:推荐|结论|建议|判断|决定|确定|意见)|(?:推荐|建议|结论).{0,8}(?:未知|待定|不明|尚无|暂无|没有)|^(?:无|暂无|未知|待定|不确定|尚未确定|未定)$")

CONCRETE_CHOICE = re.compile(
    r"^(?:是$|没有$|不$|(?:先|暂时|继续|只|仅|优先)?"
    r"(?:保留|保存|留存|留到|留作|留给|用作|用于|归档|暂缓|延期|推迟|采用|选择|维持|沿用)|"
    r"暂不决定|不再需要|不保留|不要保留|无需保留|同意|不同意|愿意|不愿意|"
    r"需要|不需要|参加|不参加|公开|私下|个人(?:回顾|纪念|保存)|方向不对|30 天内)")


FRAGMENT = re.compile(r"…|\.\.\.|(?:将|把|从|向|往|运往|送往|用于|关于|针对|为了|作为|以及|但是|但|并|或|和|的|到|与|由)[？?。！!]*$")

def safe_text(value, fallback="待核对事项", limit=48):
    if not isinstance(value, str) or not value.strip() or TECHNICAL.search(value):
        return fallback
    value = " ".join(value.split())
    return fallback if len(value) > limit or FRAGMENT.search(value) else value

def local_option(label):
    """This opaque value is a web answer, never a Core action ID or command."""
    return {"value": "local:" + hashlib.sha256(label.encode()).hexdigest()[:24], "label": label}

def recommendation_choice(raw):
    """A complete excerpt is not necessarily a choice; inspect only that clause."""
    label = recommendation_excerpt(raw)
    if not label or NO_RECOMMENDATION.search(raw) or re.search(r'[？?]', label):
        return ''
    human = human_requirement({'recommendation': label})
    explicit_action = re.search(r'(?:由|请|需要|需|须)(?:本人|你|用户)(?:亲自|自行)?(?:完成)?(?:签名|签字|签署|出售|卖出|付款|转账)', label)
    if re.search(r'无需|不必|禁止|不允许|不用', label):
        explicit_action = None
    personal_action = human or explicit_action
    if re.match(r'^(?:先|优先|继续)?(?:完成|进行)?(?:只读)?(?:核验|核对|核查|准备|读取|检查|同步)', label) and not personal_action:
        return ''
    return label if personal_action or CONCRETE_CHOICE.search(label) else ''

def option_label(value, *, explicit_option=False):
    if not isinstance(value, str) or GENERIC_ADVICE.search(value) or NO_RECOMMENDATION.search(value):
        return ""
    value = re.sub(r"^(?:(?:建议|推荐)(?:选项|选择)?\s*[:：]?\s*)+", "", value.strip())
    # Never remove a clause: it can contain a condition, scope or time limit.
    value = value.strip().removesuffix("。").strip()
    for opening, closing in (("「", "」"), ("“", "”"), ('"', '"')):
        if len(value) >= 2 and value.startswith(opening) and value.endswith(closing):
            value = value[1:-1].strip()
            break
    aliases = {"occurred": "是", "not_occurred": "没有", "defer": "暂缓"}
    value = aliases.get(value, value)
    if not value or len(value) > 24 or not re.search(r"[\u4e00-\u9fff]", value) or TECHNICAL.search(value) or FRAGMENT.search(value) or re.search(r"[<>?？\x00-\x1f]", value):
        return ""
    if not explicit_option and value not in {"记不清"} and not CONCRETE_CHOICE.search(value) and not re.fullmatch(r"由(?:本人|你)(?:亲自)?(?:完成(?:签名|最终资金动作|最终资金操作)|签名|签署|办理|确认授权(?:范围)?)", value):
        return ""
    return value

def source_options(item):
    rows = [*item.get("actions", []), *item.get("card", {}).get("answer_options", [])]
    options = []
    for row in rows:
        raw = row if isinstance(row, str) else row.get("title") or row.get("label", "")
        label = recommendation_choice(raw) if item.get("kind") == "closure" else option_label(raw, explicit_option=True)
        if label:
            marked = isinstance(row, dict) and (row.get("recommended") is True or row.get("is_recommended") is True)
            options.append((label, marked, raw))
    return options

def with_recommendation(item, q):
    """Only source-backed concrete choices; no invented fallback recommendation."""
    candidates = source_options(item)
    marked = {label for label, recommended, _ in candidates if recommended}
    label = ""
    full = ""
    if len(marked) == 1:
        originals = {raw for _, recommended, raw in candidates if recommended}
        if len(originals) == 1:
            label, full = next(iter(marked)), next(iter(originals))
    elif not marked:
        raw = item.get("recommendation", "")
        known = option_label(raw, explicit_option=True)
        if item.get("kind") == "closure":
            label = recommendation_choice(raw)
        else:
            label = known if known in {candidate for candidate, _, _ in candidates} else option_label(raw)
        full = raw
    if q["mode"] == "intent":
        # Never import a role name or add a fourth intention answer.
        label = label if label in {o["label"] for o in q["options"]} else ""
    for candidate, _, _ in candidates:
        if q["mode"] == "choice" and candidate not in {o["label"] for o in q["options"]}:
            q["options"].append(local_option(candidate))
    if label and label not in {o["label"] for o in q["options"]}:
        q["options"].append(local_option(label))
    q["recommendation"] = label
    q["recommendation_full"] = full if label and len(clauses(full)) > 1 else ""
    q["recommended_value"] = next((o["value"] for o in q["options"] if label and o["label"] == label), "")
    if item.get("kind") == "closure" and q["mode"] != "intent":
        q["headline"] = short_title(item.get("title", ""))
        q["prompt"] = q["headline"]
        q["decision_prompt"] = grounded_prompt(item, q["headline"])
        return q
    suffix = "建议：" + label if label else ""
    if len(q["prompt"]) + len(suffix) > 48:
        # Keep a complete question; the full recommendation remains on its button.
        suffix = ""
    q["headline"] = q["prompt"] + suffix
    return q

def grounded_prompt(item, headline):
    """Generate only from explicit words in this title or recommendation."""
    if headline.endswith(('？', '?')):
        return ''
    evidence = []
    for field in ('title', 'recommendation'):
        for part in clauses(item.get(field, '')):
            if not re.search(r'不(?:要|能|得|应|允许)|禁止|无需|不必|暂不', part):
                evidence.append(part)
    if any(re.search(r'出售|卖出|卖掉|卖给|转卖|是否卖|要不要卖', part) for part in evidence):
        return '要不要出售？'
    if any(re.search(r'签名|签字|签署', part) and not re.search(r'(?:签名|签字|签署)(?:样例|格式|记录|模板)', part) for part in evidence):
        return '签名如何安排？'
    if any(re.search(r'资金|付款|转账', part) for part in evidence):
        return '资金动作如何安排？'
    if any(re.search(r'保留|留存|留着', part) for part in evidence):
        return '要不要保留？'
    return ''

def human_requirement(item):
    """No actor field exists. Only explicit, unnegated human responsibilities qualify.

    Do not use boilerplate summary/reason or assume requires_decision identifies
    the actor. Technical context does not erase an explicit human authorization.
    """
    for field in ("title", "recommendation", "background"):
        text = item.get(field, "")
        if re.search(r"如果|若|除非|仅当|届时|以后", text):
            continue
        for clause in re.split(r"[，,。；;\n]", text):
            if re.search(r"无需|不需要|不用|不必|不能|不得|不由|禁止|不允许|不应|不要|无需再", clause):
                continue
            responsible = re.search(r"(?:由|请|待|需要|需|须|交给|交由)(?:本人|你|用户)|(?:本人|你|用户)(?:亲自|自行|独立|必须|需要)", clause)
            action = r"(?:本人|你|用户)(?:亲自|自行|独立)?(?:完成|进行|执行)?"
            if re.search(r"(?:签名|签署)(?:需要|需|须)?由?(?:本人|你|用户)(?:亲自)?完成", clause):
                return "signing"
            if responsible and re.search(action + r"(?:签名|签署)[/／、](?:最终资金动作|最终资金操作)$", clause):
                return "personal_action"
            if responsible and re.search(action + r"(?:签名|签署)(?=$|才能|后)", clause):
                return "signing"
            if responsible and re.search(action + r"(?:最终资金(?:动作|操作)|最终付款|最终转账)(?=$|才能|后)", clause):
                return "funds"
            if responsible and re.search(r"(?:本人|你|用户).{0,8}(?:授权|确认)", clause):
                return "authorization" if "授权" in clause else "confirmation"
            if responsible and re.search(r"(?:本人|你|用户).{0,6}(?:亲自|独立).{0,5}(?:完成|办理|操作|决定)|(?:本人|你|用户).{0,5}(?:选择|取舍|决定)", clause):
                return "personal_action"
    return ""

def closure_question(item):
    title = item.get("title", "")
    if not title.strip():
        return None
    if item.get("state", "") not in {"", "decision"}:
        return None
    human = human_requirement(item)
    technical = bool(CLOSURE_TECHNICAL.search(title) or TECHNICAL.search(title))
    if technical and not human:
        return None
    prompt = title.rstrip("。")
    if not prompt.endswith(("？", "?")):
        prompt += "，你怎么选？"
    if not technical and MATERIAL.search(title) and RETENTION.search(title):
        q = {"mode": "choice", "category": "material_retention",
             "prompt": safe_text(prompt, "这批原始素材要保留吗？"),
             "options": [local_option("先保留"), local_option("不再需要"), local_option("暂不决定")]}
    elif not technical and MATERIAL.search(title) and PURPOSE.search(title):
        q = {"mode": "choice", "category": "material_purpose",
             "prompt": safe_text(prompt, "这批原始素材准备用来做什么？"),
             "options": [{"value": "wrong", "label": "填写用途"}, {"value": "unsure", "label": "暂不决定"}]}
    elif human:
        if CAREER.search(title):
            return intention_question(item, career=True)
        q = {"mode": "choice", "category": "personal_" + human, "prompt": title,
             "options": [{"value": "wrong", "label": "填写我的选择"}, {"value": "unsure", "label": "暂不决定"}]}
    elif PERSONAL.search(title) and ("是否" in title or "要不要" in title):
        if CAREER.search(title):
            return intention_question(item, career=True)
        q = {"mode": "choice", "category": "personal_choice",
             "prompt": safe_text(prompt, "这项个人选择你怎么选？"),
             "options": [{"value": "yes", "label": "是"}, {"value": "no", "label": "不"},
                         {"value": "unsure", "label": "暂不决定"}]}
    else:
        return None
    return with_recommendation(item, q)

def intention_question(item, *, career):
    topic = "求职" if career else "这类活动"
    return with_recommendation(item, {"mode": "intent", "category": "career" if career else "activity_intent",
            "prompt": f"这些线索代表你的{topic}意向吗？",
            "options": [{"value": "yes", "label": "是"}, {"value": "wrong", "label": "方向不对"},
                        {"value": "snooze", "label": "30 天内不再问这类"}]})
