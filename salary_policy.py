import re
from dataclasses import dataclass
from typing import Optional

MONTHLY_FLOOR = 2000


@dataclass(frozen=True)
class SalaryDecision:
    allowed: bool
    reason: str
    guaranteed_monthly: Optional[float] = None


def _num(raw: str) -> float:
    return float(raw.replace(',', '').replace(' ', ''))


def _money_tokens(text: str):
    """Return explicit USD/EUR/GBP salary numbers with offsets.

    Local-currency numbers without one of these currencies are deliberately ignored;
    the 2k policy is a hard-currency monthly floor and must not confuse phone numbers,
    years, local salaries or bonuses with guaranteed base pay.
    """
    patterns = [
        r'(?P<cur>\$|€|£)\s*(?P<num>\d{1,3}(?:[ ,]\d{3})+|\d{3,6})(?:\.\d+)?',
        r'(?P<num>\d{1,3}(?:[ ,]\d{3})+|\d{3,6})(?:\.\d+)?\s*(?P<cur>usd|eur|gbp|\$|€|£)\b?',
    ]
    out = []
    for p in patterns:
        for m in re.finditer(p, text, re.I):
            try:
                out.append((m.start(), m.end(), _num(m.group('num')), m.group('cur').lower()))
            except Exception:
                pass
    # de-duplicate overlapping symbol/code matches
    dedup = []
    for item in sorted(out):
        if any(not (item[1] <= x[0] or item[0] >= x[1]) for x in dedup):
            continue
        dedup.append(item)
    return dedup


def _monthly_equivalent(value: float, context: str) -> Optional[float]:
    c = context.lower()
    if re.search(r'/(?:h|hr|hour)\b|per\s+hour|hourly|в\s+час|час(?:овой|овая)?', c):
        return value * 160.0
    if re.search(r'/(?:year|yr|annum)\b|per\s+year|annual|annually|год(?:овой|овая)?|в\s+год', c):
        return value / 12.0
    if re.search(r'/(?:week|wk)\b|per\s+week|weekly|в\s+недел', c):
        return value * 4.33
    # Explicit month markers or no explicit period: Telegram vacancy salaries are
    # treated as monthly by default, matching the project's established $2k/mo rule.
    return value


def salary_decision(text: str) -> SalaryDecision:
    t = (text or '').lower().replace('\u00a0', ' ')
    tokens = _money_tokens(t)
    if not tokens:
        return SalaryDecision(True, 'salary_unknown_or_no_hard_currency')

    # Ignore obvious one-off/project budgets when the text explicitly says project fee.
    if re.search(r'project\s+(?:fee|budget)|one[- ]off|fixed\s+project|за\s+проект', t):
        return SalaryDecision(True, 'project_compensation_not_monthly')

    # Prefer values appearing near salary/base/pay/compensation markers. If none do,
    # still evaluate explicit hard-currency amounts because job ads commonly use
    # terse titles such as "Sales Manager — $1000 + bonus".
    salary_words = r'salary|base|base pay|base salary|compensation|pay|оклад|зарплат|зп|ставка|fixed'
    candidates = []
    for start, end, value, cur in tokens:
        left = max(0, start - 80)
        right = min(len(t), end + 100)
        context = t[left:right]
        monthly = _monthly_equivalent(value, context)
        if monthly is None:
            continue
        near_salary = bool(re.search(salary_words, context))
        candidates.append((start, value, monthly, context, near_salary))

    if not candidates:
        return SalaryDecision(True, 'salary_not_comparable')

    salary_candidates = [x for x in candidates if x[4]] or candidates

    # "up to / maximum" never establishes a guaranteed >=2k base.
    for _, value, monthly, context, _ in salary_candidates:
        if re.search(r'up\s+to|max(?:imum)?|до\s*[\$€£]?\s*\d', context) and monthly >= MONTHLY_FLOOR:
            return SalaryDecision(False, 'maximum_only_not_guaranteed_2000', monthly)

    # For salary ranges or "from/start" offers, the lowest explicit hard-currency
    # amount is the guaranteed side of the offer. Bonuses/commission never raise it.
    guaranteed = min(x[2] for x in salary_candidates)
    if guaranteed < MONTHLY_FLOOR:
        return SalaryDecision(False, f'guaranteed_base_below_{MONTHLY_FLOOR}', guaranteed)

    return SalaryDecision(True, f'guaranteed_base_at_least_{MONTHLY_FLOOR}', guaranteed)


def salary_rejection_reason(text: str) -> Optional[str]:
    decision = salary_decision(text)
    return None if decision.allowed else decision.reason


def salary_meets_floor(text: str) -> bool:
    return salary_decision(text).allowed
