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
    """Return explicit USD/EUR/GBP money values with offsets."""
    patterns = [
        r'(?P<cur>\$|€|£)\s*(?P<num>\d{1,3}(?:[ ,]\d{3})+|\d{3,6})(?:\.\d+)?',
        r'(?P<num>\d{1,3}(?:[ ,]\d{3})+|\d{3,6})(?:\.\d+)?\s*(?P<cur>usd|eur|gbp|\$|€|£)',
    ]
    out = []
    for p in patterns:
        for m in re.finditer(p, text, re.I):
            try:
                out.append((m.start(), m.end(), _num(m.group('num')), m.group('cur').lower()))
            except Exception:
                pass
    dedup = []
    for item in sorted(out):
        if any(not (item[1] <= x[0] or item[0] >= x[1]) for x in dedup):
            continue
        dedup.append(item)
    return dedup


def _monthly_equivalent(value: float, context: str) -> float:
    c = context.lower()
    if re.search(r'/(?:h|hr|hour)\b|per\s+hour|hourly|в\s+час|час(?:овой|овая)?', c):
        return value * 160.0
    if re.search(r'/(?:year|yr|annum)\b|per\s+year|annual|annually|год(?:овой|овая)?|в\s+год', c):
        return value / 12.0
    if re.search(r'/(?:week|wk)\b|per\s+week|weekly|в\s+недел', c):
        return value * 4.33
    return value


def salary_decision(text: str) -> SalaryDecision:
    t = (text or '').lower().replace('\u00a0', ' ')
    tokens = _money_tokens(t)
    if not tokens:
        return SalaryDecision(True, 'salary_unknown_or_no_hard_currency')

    if re.search(r'project\s+(?:fee|budget)|one[- ]off|fixed\s+project|за\s+проект', t):
        return SalaryDecision(True, 'project_compensation_not_monthly')

    salary_words = r'salary|base|base pay|base salary|compensation|pay|оклад|зарплат|зп|ставка|fixed'
    variable_words = r'bonus|commission|incentive|variable|ote|бонус|комисс|преми'
    candidates = []

    for start, end, value, cur in tokens:
        local_left = t[max(0, start - 50):start]
        local_right = t[end:min(len(t), end + 50)]
        context = t[max(0, start - 80):min(len(t), end + 100)]

        # A separately quoted bonus/commission/OTE amount is variable compensation,
        # not guaranteed base. It must neither rescue a low base nor invalidate a
        # valid >=2k base.
        variable_amount = bool(re.search(rf'(?:{variable_words})[^\n]{{0,24}}$', local_left))
        if variable_amount:
            continue

        monthly = _monthly_equivalent(value, context)
        near_salary = bool(re.search(salary_words, context))
        upper_bound_only = bool(re.search(r'(?:up\s+to|max(?:imum)?(?:\s+of)?|до)\s*[:\-]?\s*$', local_left))
        candidates.append((start, monthly, context, near_salary, upper_bound_only))

    if not candidates:
        return SalaryDecision(True, 'only_variable_compensation_amounts_visible')

    salary_candidates = [x for x in candidates if x[3]] or candidates
    guaranteed_candidates = [x for x in salary_candidates if not x[4]]

    # "Salary up to $3000" does not guarantee a 2k base.
    if not guaranteed_candidates and salary_candidates:
        maximum = max(x[1] for x in salary_candidates)
        return SalaryDecision(False, 'maximum_only_not_guaranteed_2000', maximum)

    # In a range, or with "from/start", the lower non-variable figure is the
    # guaranteed side. Bonus/commission is deliberately excluded above.
    guaranteed = min(x[1] for x in guaranteed_candidates)
    if guaranteed < MONTHLY_FLOOR:
        return SalaryDecision(False, f'guaranteed_base_below_{MONTHLY_FLOOR}', guaranteed)

    return SalaryDecision(True, f'guaranteed_base_at_least_{MONTHLY_FLOOR}', guaranteed)


def salary_rejection_reason(text: str) -> Optional[str]:
    decision = salary_decision(text)
    return None if decision.allowed else decision.reason


def salary_meets_floor(text: str) -> bool:
    return salary_decision(text).allowed
