import re
from typing import List, Tuple

# PII Patterns
SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# Credit card patterns (13 to 19 digits, possibly separated by spaces or hyphens)
CC_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

# Prompt injection signatures
PROMPT_INJECTION_KEYWORDS = [
    "ignore all rules",
    "ignore previous instructions",
    "system override",
    "force approve",
    "auto-approve",
    "bypass the rules",
    "override rules",
    "always return approved"
]

def scrub_pii(text: str) -> Tuple[str, List[str]]:
    redacted_categories = []
    scrubbed_text = text
    
    if SSN_PATTERN.search(text):
        scrubbed_text = SSN_PATTERN.sub("[REDACTED_SSN]", scrubbed_text)
        redacted_categories.append("SSN")
        
    if CC_PATTERN.search(text):
        scrubbed_text = CC_PATTERN.sub("[REDACTED_CREDIT_CARD]", scrubbed_text)
        redacted_categories.append("CREDIT_CARD")
        
    return scrubbed_text, redacted_categories

def check_prompt_injection(text: str) -> bool:
    text_lower = text.lower()
    for kw in PROMPT_INJECTION_KEYWORDS:
        if kw in text_lower:
            return True
    return False
