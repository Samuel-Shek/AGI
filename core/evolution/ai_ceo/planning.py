"""Evidence-bounded candidate triage; this is deterministic, not an LLM.

Missing effort/savings remain unknown. New-version and usage count signals
alone cannot authorize or justify an upgrade.
"""
from __future__ import annotations

ALTERNATIVES = ["no_change", "repair", "simplify", "feature", "refactor", "dependency_upgrade"]
LABELS = {
    "no_change": "不改动，保留现状", "repair": "修复已证实的问题",
    "simplify": "减少重复步骤", "feature": "增加功能",
    "refactor": "重构", "dependency_upgrade": "升级依赖",
}


def decision_card(observation: dict) -> dict:
    status = observation.get("status", "unknown")
    decision = observation.get("decision", "observe")
    if status in {"healthy", "recovered"}:
        choice, priority = "no_change", 3
        rationale = "当前证据支持保留现状；历史失败不能直接作为再次修改的理由。"
    elif status == "fault":
        choice, priority = "repair", 1
        rationale = "先补足当前故障与工作影响的证据；此卡只建议调查，不授权修复。"
    else:
        choice, priority = "no_change", 2
        rationale = "等待或证据不足时先观察，不把未知状态转成改动需求。"
    return {
        "goal_ref": "reduce_repeated_manual_intervention",
        "engine": "deterministic_rules_v1",
        "model_used": False,
        "status": status,
        "decision": decision,
        "priority": priority,
        "recommended_option": choice,
        "rationale": rationale,
        "alternatives": [
            {"id": option, "label": LABELS[option], "assessment": (
                "当前推荐；仍须遵守授权和验收条件。" if option == choice else
                "尚无充分的收益和实施证据，不进入执行。"
            )} for option in ALTERNATIVES
        ],
        "expected_saved_minutes": None,
        "implementation_cost_minutes": None,
        "maintenance_cost_minutes": None,
        "user_impact_confirmed": False,
        "uncertainties": ["未建立自然使用基线", "未测量人工介入与实际节省时间"],
        "acceptance": ["新旧回执与同一次请求正确关联", "陈旧或缺失证据不得声称恢复", "无生产写入"],
        "stop_conditions": ["来源权限撤销", "证据结构不受支持", "请求需要模型外传或生产修改"],
        "rollback": "当前仅新增本地观察记录；没有改变业务应用，无需业务回滚。",
    }


def rank_candidates(cards: list[dict]) -> list[dict]:
    """Urgency then measured benefit/cost, unknown estimates sort last."""
    def score(card):
        benefit = card.get("expected_saved_minutes")
        cost = card.get("implementation_cost_minutes")
        confidence = card.get("evidence_confidence")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (benefit, cost, confidence)):
            return (card.get("priority", 3), 1, 0)
        if benefit < 0 or cost <= 0 or not 0 <= confidence <= 1:
            return (card.get("priority", 3), 1, 0)
        return (card.get("priority", 3), 0, -(benefit * confidence / cost))
    return sorted(cards, key=score)
