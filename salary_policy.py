import re
from dataclasses import dataclass
from typing import Optional

MONTHLY_TARGET = 2000
MIN_BASE_WITH_UPSIDE = 1600


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
        r'(?P<cur>\$|€|£)\s*(?P<num>\d{1,3}(?:[ ,]\d{3})+|\d{1,6})(?:\.\d+)?',
        r'(?P<num>\d{1,3}(?:[ ,]\d{3})+|\d{1,6})(?:\.\d+)?\s*(?P<cur>usd|eur|gbp|\$|€|£)',
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
    """Salary policy for automatic applications.

    Unknown/negotiable salary remains eligible. For explicit hard-currency pay:
    - fixed/base >= 2000/month is eligible;
    - base >= 1600 with explicit bonus/commission/variable upside is eligible;
    - a range is eligible when its lower bound is >=1600 and upper bound reaches >=2000;
    - an "up to" amount is eligible when the ceiling reaches >=2000;
    - low-base offers such as $1000 + bonus remain ineligible.
    """
    t = (text or '').lower().replace('\u00a0', ' ')
    tokens = _money_tokens(t)
    if not tokens:
        return SalaryDecision(True, 'salary_unknown_or_no_hard_currency')

    if re.search(r'project\s+(?:fee|budget)|one[- ]off|fixed\s+project|за\s+проект', t):
        return SalaryDecision(True, 'project_compensation_not_monthly')

    salary_words = r'salary|base|base pay|base salary|compensation|pay|оклад|зарплат|зп|ставка|fixed'
    variable_words = r'bonus|commission|incentive|variable|ote|бонус|комисс|преми'
    has_variable_upside = bool(re.search(variable_words, t))
    candidates = []

    for start, end, value, cur in tokens:
        local_left = t[max(0, start - 50):start]
        context = t[max(0, start - 80):min(len(t), end + 100)]

        # A separately quoted bonus/commission/OTE amount is variable compensation,
        # not base pay. Exclude it from base/range calculations.
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
    upper_only = [x for x in salary_candidates if x[4]]
    base_candidates = [x for x in salary_candidates if not x[4]]

    # "up to $3000" is now eligible; "up to $1800" is not.
    if not base_candidates and upper_only:
        ceiling = max(x[1] for x in upper_only)
        if ceiling >= MONTHLY_TARGET:
            return SalaryDecision(True, f'ceiling_reaches_{MONTHLY_TARGET}', None)
        return SalaryDecision(False, f'ceiling_below_{MONTHLY_TARGET}', ceiling)

    values = [x[1] for x in base_candidates]
    low = min(values)
    high = max(values)

    if low >= MONTHLY_TARGET:
        return SalaryDecision(True, f'base_at_least_{MONTHLY_TARGET}', low)

    # Explicit range such as 1600-2100: allow only when the low end is not too low
    # and the range reaches the 2k target.
    if len(values) >= 2 and low >= MIN_BASE_WITH_UPSIDE and high >= MONTHLY_TARGET:
        return SalaryDecision(True, 'range_reaches_target', low)

    # 1900 + commission is eligible, while 1000 + bonus is still rejected.
    if has_variable_upside and low >= MIN_BASE_WITH_UPSIDE:
        return SalaryDecision(True, 'base_with_variable_upside', low)

    return SalaryDecision(False, f'explicit_pay_below_policy_floor_{MIN_BASE_WITH_UPSIDE}_or_no_2k_upside', low)


def salary_rejection_reason(text: str) -> Optional[str]:
    decision = salary_decision(text)
    return None if decision.allowed else decision.reason


def salary_meets_floor(text: str) -> bool:
    return salary_decision(text).allowed
