import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] TraceGen: %(message)s",
)
logger = logging.getLogger("generate_traces")


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATASET_PATH = PROJECT_ROOT / "tests" / "eval" / "datasets" / "basic-dataset.json"
OUTPUT_PATH = PROJECT_ROOT / "artifacts" / "traces" / "generated_traces.json"


SSN_PATTERN = re.compile(r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b")
CREDIT_CARD_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

PROMPT_INJECTION_PATTERNS = [
    "ignore previous instructions",
    "ignore all previous instructions",
    "bypass all rules",
    "bypass rules",
    "auto-approve",
    "approve this no matter what",
    "disregard policy",
    "override system",
    "forget your instructions",
    "do not follow the policy",
]


def scrub_pii(description: str) -> Tuple[str, List[str]]:
    """
    Redacts sensitive values from the expense description.
    Returns:
        scrubbed_description, redacted_categories
    """
    if not isinstance(description, str):
        description = str(description or "")

    redacted_categories: List[str] = []
    scrubbed = description

    if SSN_PATTERN.search(scrubbed):
        scrubbed = SSN_PATTERN.sub("[REDACTED_SSN]", scrubbed)
        redacted_categories.append("ssn")

    if CREDIT_CARD_PATTERN.search(scrubbed):
        # Avoid double-counting SSNs as credit cards.
        possible_cards = CREDIT_CARD_PATTERN.findall(description)
        for value in possible_cards:
            digits_only = re.sub(r"\D", "", value)
            if len(digits_only) >= 13:
                scrubbed = scrubbed.replace(value, "[REDACTED_CREDIT_CARD]")
                if "credit_card" not in redacted_categories:
                    redacted_categories.append("credit_card")

    return scrubbed, redacted_categories


def check_prompt_injection(description: str) -> bool:
    """
    Simple keyword-based prompt-injection detector.
    """
    if not isinstance(description, str):
        description = str(description or "")

    normalized = description.lower()
    return any(pattern in normalized for pattern in PROMPT_INJECTION_PATTERNS)


class MockContext:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.visited_nodes: List[Dict[str, Any]] = []

    async def run_node(self, node_name: str, node_fn: Callable[..., Any], **kwargs: Any) -> Any:
        logger.info("Executing node: %s", node_name)

        input_args = json.loads(json.dumps(kwargs, default=str))
        output = await node_fn(self, **kwargs)

        self.visited_nodes.append(
            {
                "node": node_name,
                "input": input_args,
                "output": json.loads(json.dumps(output, default=str)),
            }
        )

        return output


async def run_security_checkpoint(ctx: MockContext, expense: Dict[str, Any]) -> Dict[str, Any]:
    original_description = expense.get("description", "")
    scrubbed_description, redacted_categories = scrub_pii(original_description)
    has_prompt_injection = check_prompt_injection(original_description)

    secured_state = dict(expense)
    secured_state["description"] = scrubbed_description
    secured_state["redacted_categories"] = redacted_categories
    secured_state["has_prompt_injection"] = has_prompt_injection
    secured_state["pii_redacted"] = bool(redacted_categories)

    return secured_state


async def run_llm_reviewer(ctx: MockContext, expense: Dict[str, Any]) -> Dict[str, Any]:
    """
    Simulated LLM reviewer.

    Important:
    - This node only receives the scrubbed expense.
    - Prompt-injection cases must bypass this node entirely.
    """
    amount = float(expense.get("amount", 0.0) or 0.0)
    category = str(expense.get("category", "")).lower()

    model_seen_description = expense.get("description", "")

    if amount < 100.0:
        return {
            "status": "auto_approved",
            "reason": "Expense is under $100 and passes baseline compliance checks.",
            "model_seen_description": model_seen_description,
        }

    return {
        "status": "flagged_for_human_review",
        "reason": "Expense amount is $100 or more and requires human review.",
        "model_seen_description": model_seen_description,
        "category": category,
    }


async def run_human_review_node(
    ctx: MockContext,
    expense: Dict[str, Any],
    reason: str,
) -> Dict[str, Any]:
    return {
        "session_id": ctx.session_id,
        "expense": expense,
        "status": "pending_human_review",
        "reason": reason,
        "security_event": bool(expense.get("has_prompt_injection", False)),
    }


async def run_workflow(session_id: str, expense: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    ctx = MockContext(session_id)

    secured_state = await ctx.run_node(
        "security_checkpoint",
        run_security_checkpoint,
        expense=expense,
    )

    if secured_state.get("has_prompt_injection"):
        output = await ctx.run_node(
            "human_review",
            run_human_review_node,
            expense=secured_state,
            reason="Prompt injection detected. LLM reviewer bypassed.",
        )
        output["llm_bypassed"] = True
        output["route"] = "human_review"
        return output, ctx.visited_nodes

    llm_result = await ctx.run_node(
        "llm_reviewer",
        run_llm_reviewer,
        expense=secured_state,
    )

    if llm_result.get("status") == "flagged_for_human_review":
        output = await ctx.run_node(
            "human_review",
            run_human_review_node,
            expense=secured_state,
            reason=llm_result.get("reason", "Human review required."),
        )
        output["llm_bypassed"] = False
        output["route"] = "human_review"
        return output, ctx.visited_nodes

    final_output = {
        "expense": secured_state,
        "status": "auto_approved",
        "reason": llm_result.get("reason"),
        "security_event": False,
        "llm_bypassed": False,
        "route": "auto_approval",
        "model_seen_description": llm_result.get("model_seen_description"),
    }

    return final_output, ctx.visited_nodes


def load_cases() -> List[Dict[str, Any]]:
    if not DATASET_PATH.exists():
        raise FileNotFoundError(
            f"Dataset not found: {DATASET_PATH}\n"
            "Create tests/eval/datasets/basic-dataset.json first."
        )

    with DATASET_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        return data

    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        return data["cases"]

    raise ValueError(
        "Dataset must be either a JSON list of cases or an object with a 'cases' list."
    )


def automate_human_decision(workflow_output: Dict[str, Any]) -> Tuple[Dict[str, Any], bool, str | None]:
    final_output = dict(workflow_output)
    human_intercepted = workflow_output.get("status") == "pending_human_review"
    automated_decision = None

    if not human_intercepted:
        return final_output, False, None

    if workflow_output.get("security_event"):
        automated_decision = "rejected"
        final_output["status"] = "rejected"
        final_output["approved_by"] = None
        final_output["reason"] = "Automated HITL rejection for prompt-injection security event."
    else:
        automated_decision = "approved"
        final_output["status"] = "approved_after_human_review"
        final_output["approved_by"] = "automated_human_review"
        final_output["reason"] = "Automated HITL approval for clean manually reviewed expense."

    final_output["human_review_completed"] = True
    final_output["automated_decision"] = automated_decision

    return final_output, human_intercepted, automated_decision


async def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    cases = load_cases()
    traces: List[Dict[str, Any]] = []

    for case in cases:
        case_id = case.get("id", f"case-{len(traces) + 1}")
        name = case.get("name", case_id)
        payload = case.get("payload")

        if not isinstance(payload, dict):
            raise ValueError(f"Case {case_id} is missing a valid object payload.")

        logger.info("Running scenario: %s (ID: %s)", name, case_id)

        workflow_output, steps = await run_workflow(
            session_id=f"eval-session-{case_id}",
            expense=payload,
        )

        final_output, human_intercepted, automated_decision = automate_human_decision(workflow_output)

        trace = {
            "case_id": case_id,
            "name": name,
            "input": payload,
            "expected": case.get("expected", {}),
            "steps": steps,
            "output": final_output,
            "human_intercepted": human_intercepted,
            "automated_decision": automated_decision,
        }

        traces.append(trace)

    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(traces, f, indent=2)

    logger.info("Successfully generated traces: %s", OUTPUT_PATH)


if __name__ == "__main__":
    asyncio.run(main())