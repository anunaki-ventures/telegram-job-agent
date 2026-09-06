import re

# Telegram usernames are 5-32 chars and may contain letters, digits and underscores.
_AT_RE = re.compile(r"(?<![\w])@([A-Za-z][A-Za-z0-9_]{4,31})")
_TME_RE = re.compile(r"(?:https?://)?t\.me/(?:s/)?([A-Za-z][A-Za-z0-9_]{4,31})", re.I)

# t.me links are only trusted automatically when the surrounding line clearly presents
# them as an application/contact route. This avoids treating unrelated channel links as recruiters.
_CONTACT_HINT_RE = re.compile(
    r"(?:telegram|\btg\b|contact|apply|application|send\s+(?:your\s+)?(?:cv|resume)|"
    r"cv\s*(?:to|:)|resume\s*(?:to|:)|dm\b|direct\s+message|"
    r"телеграм|telegram|контакт|писать|напишите|резюме|отклик)",
    re.I,
)

_RESERVED = {"joinchat", "share", "addstickers", "proxy", "socks"}


def _clean(username, source_username=None):
    if not username:
        return None
    u = username.strip().lstrip("@").lower()
    source = (source_username or "").strip().lstrip("@").lower()
    if not u or u in _RESERVED or u == source:
        return None
    return "@" + username.strip().lstrip("@")


def extract_telegram_contact(text, source_username=None):
    """Return a conservative Telegram application contact or None.

    Preference order:
    1. @username on a line with explicit contact/apply language;
    2. t.me/username on a line with explicit contact/apply language;
    3. any standalone @username (legacy Telegram vacancy convention).

    Unlabelled t.me links are intentionally not used because job posts frequently contain
    links to other channels that are not recruiter contacts.
    """
    text = text or ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    for line in lines:
        if not _CONTACT_HINT_RE.search(line):
            continue
        for match in _AT_RE.finditer(line):
            contact = _clean(match.group(1), source_username)
            if contact:
                return contact
        for match in _TME_RE.finditer(line):
            contact = _clean(match.group(1), source_username)
            if contact:
                return contact

    # Preserve the existing behaviour for explicit @handles, but exclude the source itself.
    for match in _AT_RE.finditer(text):
        contact = _clean(match.group(1), source_username)
        if contact:
            return contact

    return None
