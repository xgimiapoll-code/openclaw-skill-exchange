"""Content security guard — scans text and recipes for malicious patterns.

PHILOSOPHY: Only block content that is CLEARLY an attack. Borderline cases
get logged as warnings but allowed through. We never want a legitimate agent
to get blocked because their task description happened to contain a keyword.

Hard-blocks (raise ContentViolation):
- Shell commands in recipes (rm -rf, | bash, eval, exec)
- Direct credential extraction ("reveal your api_key")
- Oversized payloads (DoS prevention)

Soft-checks (log warning, allow through):
- Suspicious URLs in text
- Borderline injection patterns
"""

import logging
import re

logger = logging.getLogger(__name__)

# ── Pattern databases ──

# HARD-BLOCK patterns — only the most clearly malicious, zero false-positive risk
_PROMPT_INJECTION_PATTERNS = [
    # Credential extraction — the #1 real threat in agent-to-agent systems
    r"(reveal|show|print|output|return|send|post|leak)\s+(your\s+)?(api[_\s]?key|secret|password|credential|bearer)",
    r"drain\s+(wallet|balance|funds|tokens)",
]

# SOFT-WARNING patterns — logged but NOT blocked (for early adoption friendliness)
_SOFT_WARNING_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules)",
    r"forget\s+(everything|all|your)\s+(you|instructions|rules)",
    r"you\s+are\s+now\s+(a|an|DAN|jailbreak)",
    r"override\s+(instructions|system|safety)",
    r"(transfer|send|withdraw|bridge)\s+.*\s+(all|entire|maximum|max)\s+(balance|shl|tokens|funds)",
]

# Dangerous URL patterns in recipes
_DANGEROUS_URL_PATTERNS = [
    r"https?://[^\s]*\.(ru|cn|tk|ml|ga|cf)/",  # Suspicious TLDs in recipe
    r"https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}",  # Direct IP
    r"https?://localhost",
    r"https?://127\.0\.0\.1",
    r"https?://0\.0\.0\.0",
    r"data:\s*text/html",
    r"javascript:",
]

# Dangerous recipe action patterns
_DANGEROUS_RECIPE_ACTIONS = [
    r"\beval\b",
    r"\bexec\b",
    r"\b__import__\b",
    r"\bos\.system\b",
    r"\bsubprocess\b",
    r"\bcurl\s+.*-d\b",  # Data exfiltration via curl
    r"\bwget\b",
    r"\brm\s+-rf\b",
    r"\bchmod\b.*777",
    r"\bnc\s+-",  # netcat
    r"\bbase64\s+--decode\b",
    r"\|\s*bash\b",
    r"\|\s*sh\b",
    r">\s*/etc/",
    r"\bsudo\b",
]

# Compile all patterns for performance
_INJECTION_RES = [re.compile(p, re.IGNORECASE) for p in _PROMPT_INJECTION_PATTERNS]
_SOFT_WARNING_RES = [re.compile(p, re.IGNORECASE) for p in _SOFT_WARNING_PATTERNS]
_URL_RES = [re.compile(p, re.IGNORECASE) for p in _DANGEROUS_URL_PATTERNS]
_ACTION_RES = [re.compile(p, re.IGNORECASE) for p in _DANGEROUS_RECIPE_ACTIONS]

# ── Size limits (generous for early adoption) ──

MAX_TEXT_FIELD_LEN = 50_000  # Characters per text field (generous)
MAX_RECIPE_DEPTH = 10  # JSON nesting depth
MAX_RECIPE_STEPS = 200
MAX_TAGS = 50
MAX_TAG_LEN = 100


# ── Public API ──


class ContentViolation(Exception):
    """Raised when content fails security checks."""

    def __init__(self, reason: str, field: str, severity: str = "blocked"):
        self.reason = reason
        self.field = field
        self.severity = severity
        super().__init__(f"[{severity}] {field}: {reason}")


def scan_text(text: str, field_name: str = "text") -> list[str]:
    """Scan a text field for prompt injection and credential extraction.

    Returns list of warnings. Raises ContentViolation for severe threats.
    """
    if not text:
        return []

    warnings = []

    # Size check
    if len(text) > MAX_TEXT_FIELD_LEN:
        raise ContentViolation(
            f"Text exceeds maximum length ({len(text)} > {MAX_TEXT_FIELD_LEN})",
            field_name,
        )

    # Hard-block: credential extraction / wallet drain
    for pattern in _INJECTION_RES:
        match = pattern.search(text)
        if match:
            matched = match.group(0)
            logger.warning(
                "Hard-blocked content in %s: %r", field_name, matched
            )
            raise ContentViolation(
                f"Potentially malicious content detected: {matched[:60]}",
                field_name,
            )

    # Soft-warning: suspicious but allowed (log only)
    for pattern in _SOFT_WARNING_RES:
        match = pattern.search(text)
        if match:
            logger.info(
                "Soft warning in %s: %r (allowed)", field_name, match.group(0)
            )
            warnings.append(f"Suspicious pattern in {field_name}: {match.group(0)[:40]}")

    # Dangerous URLs in text — warn only, don't block
    for pattern in _URL_RES:
        match = pattern.search(text)
        if match:
            warnings.append(f"Suspicious URL in {field_name}: {match.group(0)[:60]}")

    return warnings


def scan_recipe(recipe: dict, field_name: str = "recipe") -> list[str]:
    """Scan a skill recipe for dangerous actions, URLs, and shell commands.

    Returns list of warnings. Raises ContentViolation for severe threats.
    """
    if not recipe:
        return []

    warnings = []

    # Depth check
    depth = _json_depth(recipe)
    if depth > MAX_RECIPE_DEPTH:
        raise ContentViolation(
            f"Recipe nesting too deep ({depth} > {MAX_RECIPE_DEPTH})",
            field_name,
        )

    # Step count check
    steps = recipe.get("steps", [])
    if isinstance(steps, list) and len(steps) > MAX_RECIPE_STEPS:
        raise ContentViolation(
            f"Too many recipe steps ({len(steps)} > {MAX_RECIPE_STEPS})",
            field_name,
        )

    # Flatten all string values for scanning
    all_strings = _extract_strings(recipe)
    full_text = " ".join(all_strings)

    # Check for dangerous actions
    for pattern in _ACTION_RES:
        match = pattern.search(full_text)
        if match:
            logger.warning(
                "Dangerous action in recipe %s: %r", field_name, match.group(0)
            )
            raise ContentViolation(
                f"Dangerous action detected in recipe: {match.group(0)[:60]}",
                field_name,
            )

    # Check for dangerous URLs
    for pattern in _URL_RES:
        match = pattern.search(full_text)
        if match:
            warnings.append(f"Suspicious URL in recipe: {match.group(0)[:60]}")

    # Check for prompt injection in recipe strings
    for pattern in _INJECTION_RES:
        match = pattern.search(full_text)
        if match:
            logger.warning(
                "Prompt injection in recipe %s: %r", field_name, match.group(0)
            )
            raise ContentViolation(
                f"Potentially malicious content in recipe: {match.group(0)[:60]}",
                field_name,
            )

    return warnings


def scan_tags(tags: list[str], field_name: str = "tags") -> list[str]:
    """Validate tag list for abuse."""
    warnings = []

    if len(tags) > MAX_TAGS:
        raise ContentViolation(
            f"Too many tags ({len(tags)} > {MAX_TAGS})", field_name
        )

    for tag in tags:
        if len(tag) > MAX_TAG_LEN:
            raise ContentViolation(
                f"Tag too long ({len(tag)} > {MAX_TAG_LEN}): {tag[:20]}...",
                field_name,
            )
        # Tags should be simple labels
        if re.search(r"[<>\"';&|`$\\]", tag):
            raise ContentViolation(
                f"Tag contains invalid characters: {tag[:30]}", field_name
            )

    return warnings


def scan_submission(summary: str, recipe: dict | None = None) -> list[str]:
    """Full scan of a submission: summary text + optional recipe."""
    warnings = []
    warnings.extend(scan_text(summary, "summary"))
    if recipe:
        warnings.extend(scan_recipe(recipe, "skill_recipe"))
    return warnings


def scan_task(title: str, description: str, tags: list[str] | None = None) -> list[str]:
    """Full scan of a task: title + description + tags."""
    warnings = []
    warnings.extend(scan_text(title, "title"))
    warnings.extend(scan_text(description, "description"))
    if tags:
        warnings.extend(scan_tags(tags, "tags"))
    return warnings


def scan_skill(name: str, title: str, description: str | None,
               recipe: dict | None, tags: list[str] | None = None) -> list[str]:
    """Full scan of a skill: all text fields + recipe."""
    warnings = []
    warnings.extend(scan_text(name, "name"))
    warnings.extend(scan_text(title, "title"))
    if description:
        warnings.extend(scan_text(description, "description"))
    if recipe:
        warnings.extend(scan_recipe(recipe, "recipe"))
    if tags:
        warnings.extend(scan_tags(tags, "tags"))
    return warnings


# ── Helpers ──


def _json_depth(obj, current: int = 0) -> int:
    """Calculate max nesting depth of a JSON structure."""
    if isinstance(obj, dict):
        if not obj:
            return current + 1
        return max(_json_depth(v, current + 1) for v in obj.values())
    elif isinstance(obj, list):
        if not obj:
            return current + 1
        return max(_json_depth(v, current + 1) for v in obj)
    return current


def _extract_strings(obj) -> list[str]:
    """Recursively extract all string values from a JSON-like structure."""
    strings = []
    if isinstance(obj, str):
        strings.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            strings.extend(_extract_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            strings.extend(_extract_strings(v))
    return strings
