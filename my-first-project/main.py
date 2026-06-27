import os
import re
import json
import base64
import logging
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import uvicorn

# Configure standard python logging for console logs as per checklist
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("expense_agent")

# Checklist: Telemetry: Set otel_to_cloud=False
# To ensure telemetry doesn't export to cloud, we set the environment variable
os.environ["OTEL_TO_CLOUD"] = "false"
os.environ["OTEL_SDK_DISABLED"] = "true"  # Double safety check to avoid OTel overhead

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

# In-memory human review database
human_reviews_db: List[Dict[str, Any]] = []

def scrub_pii(text: str) -> tuple[str, List[str]]:
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

# Initialize ADK components dynamically to handle cases where google-adk is being installed
# or fallback gracefully if there are environment issues
try:
    from google.adk.agents import Agent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.workflow import node
    
    # Declare the session service
    session_service = InMemorySessionService()
    
    # Define Nodes using ADK 2.0 @node
    @node
    async def security_checkpoint(ctx, expense: Dict[str, Any]) -> Dict[str, Any]:
        description = expense.get("description", "")
        scrubbed_desc, redacted_cats = scrub_pii(description)
        has_injection = check_prompt_injection(description)
        
        # Keep track of updated fields
        state = expense.copy()
        state["description"] = scrubbed_desc
        state["redacted_categories"] = redacted_cats
        state["has_prompt_injection"] = has_injection
        
        logger.info(f"Security Checkpoint: Scrubbed PII. Redacted: {redacted_cats}. Prompt Injection Detected: {has_injection}")
        return state

    @node
    async def llm_reviewer(ctx, expense: Dict[str, Any]) -> Dict[str, Any]:
        # Fallback compliance logic if API key isn't configured, otherwise use Gemini LLM
        api_key = os.environ.get("GEMINI_API_KEY")
        
        if api_key:
            logger.info("Calling Gemini LLM Reviewer via ADK Agent...")
            try:
                # Construct an ADK agent
                agent = Agent(
                    name="gemini_compliance_reviewer",
                    instruction=(
                        "You are an expense compliance reviewer. Review the expense details and determine "
                        "if they are compliant. Respond ONLY with a valid JSON: "
                        '{"status": "approved" | "rejected" | "flagged_for_review", "reason": "explanation"}'
                    )
                )
                workflow_runner = Runner(
    agent=root_expense_workflow,
    session_service=session_service,
    app_name="expense_agent",
)
                )
                result = json.loads(response.text)
                return {
                    "status": result.get("status", "flagged_for_review"),
                    "reason": result.get("reason", "LLM determined status"),
                    "by": "gemini_llm"
                }
            except Exception as e:
                logger.error(f"Gemini LLM Call failed: {e}. Falling back to rules.")
        
        # Rule-based fallback
        amount = expense.get("amount", 0.0)
        category = expense.get("category", "").lower()
        
        if category == "software" and amount < 100.0:
            return {
                "status": "approved",
                "reason": "Software purchase under $100 is automatically compliant.",
                "by": "static_rules"
            }
        elif amount >= 100.0:
            return {
                "status": "flagged_for_review",
                "reason": "Expense amount exceeds $100 threshold.",
                "by": "static_rules"
            }
        else:
            return {
                "status": "approved",
                "reason": "Expense matches baseline compliance rules.",
                "by": "static_rules"
            }

    @node
    async def human_review(ctx, expense: Dict[str, Any], reason: str) -> Dict[str, Any]:
        logger.warning(f"Routing to human review. Reason: {reason}")
        
        review_item = {
            "session_id": ctx.session_id if hasattr(ctx, "session_id") else "local-session",
            "expense": expense,
            "reason": reason,
            "status": "pending_human_review",
            "security_event": expense.get("has_prompt_injection", False)
        }
        human_reviews_db.append(review_item)
        return review_item

    # Orchestrator Node (Root Workflow Agent)
    @node
    async def root_expense_workflow(ctx, expense: Dict[str, Any]) -> Dict[str, Any]:
        # 1. Run security checkpoint
        secured_state = await ctx.run_node(security_checkpoint, expense=expense)
        
        # 2. Check route branch
        if secured_state.get("has_prompt_injection"):
            # Prompt injection -> bypass LLM, route straight to human review, flag as security event
            result = await ctx.run_node(
                human_review,
                expense=secured_state,
                reason="Security Checkpoint Flagged: Prompt Injection Attempted"
            )
            return result
            
        # 3. Clean -> LLM Reviewer
        llm_result = await ctx.run_node(llm_reviewer, expense=secured_state)
        
        # 4. Handle LLM decision
        if llm_result.get("status") == "flagged_for_review":
            result = await ctx.run_node(
                human_review,
                expense=secured_state,
                reason=llm_result.get("reason", "Flagged by LLM compliance review")
            )
            return result
            
        # Approved or Rejected directly
        return {
            "expense": secured_state,
            "status": llm_result.get("status", "approved"),
            "reason": llm_result.get("reason", "Approved by automated rules"),
            "security_event": False
        }

    # Initialize workflow runner
    workflow_runner = Runner(agent=root_expense_workflow, session_service=session_service, app_name="expense_agent")

except ImportError as ie:
    logger.warning(f"Google ADK 2.0 could not be imported: {ie}. Using local workflow emulation.")
    # Implement clean emulation so it runs seamlessly
    class MockContext:
        def __init__(self, session_id: str):
            self.session_id = session_id
        async def run_node(self, node_fn, **kwargs):
            return await node_fn(self, **kwargs)

    async def security_checkpoint(ctx, expense: Dict[str, Any]):
        description = expense.get("description", "")
        scrubbed_desc, redacted_cats = scrub_pii(description)
        has_injection = check_prompt_injection(description)
        state = expense.copy()
        state["description"] = scrubbed_desc
        state["redacted_categories"] = redacted_cats
        state["has_prompt_injection"] = has_injection
        logger.info(f"[Emu] Security Checkpoint: Scrubbed: {redacted_cats}. Injection: {has_injection}")
        return state

    async def llm_reviewer(ctx, expense: Dict[str, Any]):
        amount = expense.get("amount", 0.0)
        category = expense.get("category", "").lower()
        if category == "software" and amount < 200.0:
            return {"status": "approved", "reason": "Software purchase under $200 automatically compliant."}
        elif amount >= 1000.0:
            return {"status": "flagged_for_review", "reason": "Expense amount exceeds $1000 threshold."}
        else:
            return {"status": "approved", "reason": "Expense matches baseline compliance rules."}

    async def human_review(ctx, expense: Dict[str, Any], reason: str):
        logger.warning(f"[Emu] Routing to human review. Reason: {reason}")
        review_item = {
            "session_id": ctx.session_id,
            "expense": expense,
            "reason": reason,
            "status": "pending_human_review",
            "security_event": expense.get("has_prompt_injection", False)
        }
        human_reviews_db.append(review_item)
        return review_item

    async def root_expense_workflow_emu(session_id: str, expense: Dict[str, Any]) -> Dict[str, Any]:
        ctx = MockContext(session_id)
        secured_state = await security_checkpoint(ctx, expense)
        if secured_state.get("has_prompt_injection"):
            return await human_review(ctx, secured_state, "Security Checkpoint Flagged: Prompt Injection Attempted")
        llm_result = await llm_reviewer(ctx, secured_state)
        if llm_result.get("status") == "flagged_for_review":
            return await human_review(ctx, secured_state, llm_result.get("reason"))
        return {
            "expense": secured_state,
            "status": llm_result.get("status"),
            "reason": llm_result.get("reason"),
            "security_event": False
        }

# FastAPI Web Service
app = FastAPI(title="Ambient Expense Approval Service")

@app.post("/apps/expense_agent/trigger/pubsub")
async def trigger_pubsub(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
        
    subscription_path = body.get("subscription", "default-sub")
    
    # Gotcha: normalize fully-qualified subscription path down to short name
    # e.g., projects/my-project/subscriptions/test-sub -> test-sub
    sub_name = subscription_path.split("/")[-1]
    logger.info(f"Normalized subscription name: {sub_name}")
    
    # Extract message data
    message = body.get("message", {})
    expense_payload = {}
    if isinstance(message, dict) and "data" in message:
        try:
            data_b64 = message["data"]
            # Correct padding if necessary
            missing_padding = len(data_b64) % 4
            if missing_padding:
                data_b64 += '=' * (4 - missing_padding)
            data_bytes = base64.b64decode(data_b64)
            data_str = data_bytes.decode("utf-8")
            expense_payload = json.loads(data_str)
        except Exception as e:
            logger.error(f"Failed to decode base64 data: {e}")
            raise HTTPException(status_code=400, detail="Failed to decode Pub/Sub base64 message data")
    else:
        # Fallback if raw JSON is sent directly without pubsub envelope
        expense_payload = body
        
    logger.info(f"Feeding expense payload into workflow. Session: {sub_name}")
    logger.info(f"Expense payload: {json.dumps(expense_payload)}")
    
    # Run the workflow
    try:
        if 'workflow_runner' in globals():
            session = await session_service.create_session(app_name="expense_agent", user_id=sub_name)
            result = await workflow_runner.run_async(session_id=session.id, input=json.dumps(expense_payload))
            output = json.loads(result.text) if hasattr(result, "text") else result
            session_id = session.id
        else:
            session_id = f"emu-session-{sub_name}"
            output = await root_expense_workflow_emu(session_id, expense_payload)
    except Exception as e:
        logger.error(f"Workflow execution failed: {e}")
        raise HTTPException(status_code=500, detail=f"Workflow execution error: {str(e)}")
        
    return {
        "status": "processed",
        "subscription": sub_name,
        "session_id": session_id,
        "output": output
    }

@app.get("/reviews")
async def get_reviews():
    return {
        "pending_reviews_count": len(human_reviews_db),
        "reviews": human_reviews_db
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
