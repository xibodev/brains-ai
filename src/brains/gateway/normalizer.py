from brains.config import settings


def redact_payload(value):
    """Recursively redact secret-bearing dictionary keys in payload-like data.

    Any dictionary key that contains one of
    `settings.redaction_sensitive_key_parts` (case-insensitive) is replaced with
    `[REDACTED]`. Lists and nested dicts are traversed recursively.
    """
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if any(part in key.lower() for part in settings.redaction_sensitive_key_parts):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value
