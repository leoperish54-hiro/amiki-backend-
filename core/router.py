import os
from core.model import ask_model
from core.request_trace import RequestTrace
from core.context import add_message
from core.memory_extractor import extract_memories
from core.memory_classifier import classify_memory
from core.memory_manager import remember
from core.learning_engine import capture_feedback
from core.research_learning import capture_research_candidates
from core.prompt import build_model_request
from core.awareness import build_reasoning_awareness
from core.attachments import AttachmentInput
from core.attachment_ingestion import ingest_attachment
from core.attachment_extraction import extract_attachment_content
from core.attachment_context import (
    build_attachment_context,
    format_attachment_context,
)
from core.capabilities import get_capabilities, is_available
from core.handlers.analysis import handle as analysis_handler
from core.handlers.university import handle as university_handler
from core.handlers.workspace import handle as workspace_handler
from core.handlers.noctis_requiem import handle as noctis_requiem_handler
from core.handlers.web import handle as web_handler
from core.agents.gateway import AgentGateway
from core.observability import get_observability
from core.conversation_contracts import ConversationInput
from core.model_result_contracts import ModelResult


def process_memory_candidates(message, correlation_id=None):
    """Extract, classify, and execute valid memory operations."""
    candidates = extract_memories(message)

    processed = []

    for candidate in candidates:
        classification = classify_memory(candidate)
        decision = classification.get("decision")

        category = candidate.get("category")
        key = candidate.get("key")
        value = candidate.get("value")

        if decision == "remember":
            if remember(category, key, value):
                processed.append(candidate)
                get_observability().emit({
                    "event_type": "MEMORY_WRITE",
                    "severity": "INFO",
                    "source": "backend",
                    "component": "memory_manager",
                    "summary": "Persistent memory write completed.",
                    "status": "completed",
                    "correlation_id": correlation_id,
                    "metadata": {"category": category},
                })

        elif decision == "forget":
            from core.memory_manager import forget

            if forget(category, key):
                processed.append(candidate)
                get_observability().emit({
                    "event_type": "MEMORY_CLEAR",
                    "severity": "INFO",
                    "source": "backend",
                    "component": "memory_manager",
                    "summary": "Persistent memory clear completed.",
                    "status": "completed",
                    "correlation_id": correlation_id,
                    "metadata": {"category": category},
                })

    return processed



def get_available_capabilities():
    """Return capabilities currently available to Amiki."""
    return {
        name: available
        for name, available in get_capabilities().items()
        if available
    }

 
 
def resolve_capability(message):
    """Determine which available capability best matches a message."""
    # Explicit specialist directives take precedence over capability routing.
    if _AGENT_GATEWAY._directive(message) is not None:
        return "agent"

    from core.learning_projects import detect_learning_request
    if detect_learning_request(message):
        return "learning"

    message = message.lower()

    capability_keywords = {
        "analysis": ("analyze", "analysis", "calculate", "explain"),
        "university": ("university", "course", "assignment", "lecture"),
        "workspace": ("workspace", "project", "file", "code"),
        "noctis_requiem": ("noctis requiem", "noctis", "character", "faction", "vessel", "sovereignty", "anomaly", "anomalies"),
        "web": ("weather", "temperature", "current", "right now", "latest", "today", "tonight", "live", "news", "online", "recent", "now"),
    }

    for capability, keywords in capability_keywords.items():
        if not is_available(capability):
            continue

        if any(keyword in message for keyword in keywords):
            return capability

    # Workspace fallback: recognize an explicit workspace action when
    # accompanied by a concrete file/path target. Bare or ambiguous
    # actions remain unrouted.
    if is_available("workspace"):
        workspace_actions = (
            "read", "open", "view", "show", "inspect",
            "execute", "run", "test", "edit", "change", "modify",
        )
        ignored = {
            "the", "my", "this", "that", "me", "please",
            "file", "workspace", "project", "code", "script",
            "python", "in", "for",
        }

        words = message.split()

        if words and words[0] in workspace_actions:
            targets = [
                word.strip(".,!?;:'\"()[]{}")
                for word in words[1:]
                if word.strip(".,!?;:'\"()[]{}").lower() not in ignored
            ]

            for target in targets:
                if (
                    "." in target
                    or "/" in target
                    or "\\" in target
                ):
                    return "workspace"

    # Automatic specialist routing is a fallback: concrete built-in
    # capabilities and explicit workspace actions must retain priority.
    if getattr(_AGENT_GATEWAY, "auto_route", lambda _message: None)(message) is not None:
        return "agent"

    return None


_AGENT_GATEWAY = AgentGateway()


CAPABILITY_HANDLERS = {
    "analysis": analysis_handler,
    "university": university_handler,
    "workspace": workspace_handler,
    "noctis_requiem": noctis_requiem_handler,
    "web": web_handler,
}


def register_capability(name, handler):
    """Register a handler for a capability."""
    if not isinstance(name, str) or not name:
        raise ValueError("Capability name must be a non-empty string.")

    if not callable(handler):
        raise TypeError("Capability handler must be callable.")

    CAPABILITY_HANDLERS[name] = handler


def get_capability_handler(name):
    """Return the registered handler for a capability."""
    return CAPABILITY_HANDLERS.get(name)


def dispatch_capability(capability, message, correlation_id=None):
    """Dispatch a message to an available capability."""
    if capability == "agent":
        # The gateway is the policy boundary for both explicit directives and
        # automatic specialist routing. Do not duplicate its routing rules
        # here: doing so can resolve a message to "agent" and then silently
        # drop the dispatch when the user did not use an "agent:" prefix.
        return _AGENT_GATEWAY.execute(message, correlation_id=correlation_id)

    if capability is None:
        return None

    if capability == "learning":
        from core.learning_projects import detect_learning_request, run_learning_request
        subject = detect_learning_request(message)
        if subject is None:
            return None
        return {
            "capability": "learning",
            "status": "handled",
            "result": run_learning_request(subject),
        }

    if not is_available(capability):
        return None

    handler = get_capability_handler(capability)

    if handler is not None:
        result = handler(message)

        return {
            "capability": capability,
            "status": "handled",
            "result": result,
        }

    return {
        "capability": capability,
        "status": "available",
        "result": None,
    }


def run_pipeline(message, capability=None, include_trace=False, attachments=None, location_context=None, conversation_id=None):
    """Run Amiki's core conversation pipeline."""
    import traceback

    request = ConversationInput.create(
        message,
        capability=capability,
        attachments=attachments,
        location_context=location_context,
        conversation_id=conversation_id,
    )
    message = request.message
    capability = request.capability
    attachments = request.attachments
    location_context = request.location_context
    conversation_id = request.conversation_id

    trace = RequestTrace()

    log_path = os.path.expanduser("~/amiki-api.log")

    def mark(stage):
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"PIPELINE STAGE: {stage}\\n")

    try:
        trace.start_stage("attachments")
        mark("attachments:start")

        attachment_contexts = []
        model_parts = []

        for attachment_input in attachments:
            if not isinstance(attachment_input, AttachmentInput):
                raise TypeError(
                    "Pipeline attachments must be AttachmentInput objects."
                )

            get_observability().emit({
                "event_type": "ATTACHMENT_RECEIVED",
                "severity": "INFO",
                "source": "backend",
                "component": "attachment_pipeline",
                "summary": "Attachment received for processing.",
                "status": "received",
                "correlation_id": trace.request_id,
                "metadata": {
                    "mime_type": attachment_input.attachment.mime_type,
                    "size_bytes": attachment_input.attachment.size_bytes,
                },
            })

            try:
                ingested = ingest_attachment(
                    attachment_input.attachment,
                    attachment_input.data,
                )
                extracted = extract_attachment_content(ingested)
                context = build_attachment_context(extracted)
            except (ValueError, TypeError):
                get_observability().emit({
                    "event_type": "ATTACHMENT_REJECTED",
                    "severity": "WARNING",
                    "source": "backend",
                    "component": "attachment_pipeline",
                    "summary": "Attachment was rejected by validation or extraction.",
                    "status": "rejected",
                    "correlation_id": trace.request_id,
                })
                raise

            attachment_contexts.append(context)
            get_observability().emit({
                "event_type": "ATTACHMENT_PROCESSED",
                "severity": "INFO",
                "source": "backend",
                "component": "attachment_pipeline",
                "summary": "Attachment processed successfully.",
                "status": "completed",
                "correlation_id": trace.request_id,
                "metadata": {"content_type": context.content_type},
            })

            if context.data is not None:
                model_parts.append(
                    {
                        "type": "image",
                        "mime_type": context.content_type,
                        "data": context.data,
                    }
                )

        mark(f"attachments:done:{len(attachment_contexts)}")
        trace.complete_stage(
            "attachments",
            detail=f"{len(attachment_contexts)} attachment(s)",
        )

        trace.start_stage("memory")
        mark("memory:start")
        processed_memories = process_memory_candidates(
            message,
            correlation_id=trace.request_id,
        )
        learning_candidate = capture_feedback(message)
        if learning_candidate:
            get_observability().emit({
                "event_type": "LEARNING_CANDIDATE_CAPTURED",
                "severity": "INFO",
                "source": "backend",
                "component": "learning_engine",
                "summary": "Explicit correction captured for review; not auto-promoted.",
                "status": "pending_review",
                "correlation_id": trace.request_id,
            })
        mark(f"memory:done:{len(processed_memories)}")
        trace.complete_stage(
            "memory",
            detail=f"{len(processed_memories)} memory operation(s)",
        )

        trace.start_stage("capability:resolve")
        mark("capability:resolve:start")

        if capability is None:
            capability = resolve_capability(message)

        trace.selected_capability = capability

        mark(f"capability:resolve:done:{capability}")
        trace.complete_stage(
            "capability:resolve",
            detail=capability,
        )

        trace.start_stage("capability:dispatch")
        mark("capability:dispatch:start")
        dispatch = dispatch_capability(
            capability,
            message,
            correlation_id=trace.request_id,
        )
        research_candidates = capture_research_candidates(dispatch)
        if research_candidates:
            get_observability().emit({
                "event_type": "RESEARCH_LEARNING_CANDIDATES_CAPTURED",
                "severity": "INFO",
                "source": "backend",
                "component": "learning_engine",
                "summary": "Source-backed research candidates were queued for review.",
                "status": "pending_review",
                "correlation_id": trace.request_id,
                "metadata": {"candidate_count": len(research_candidates)},
            })
        mark("capability:dispatch:done")
        trace.complete_stage("capability:dispatch")

        trace.start_stage("prompt")
        mark("prompt:start")
        reasoning_awareness = build_reasoning_awareness(trace)
        attachment_prompt_context = format_attachment_context(
            attachment_contexts
        )
        model_request = build_model_request(
            message,
            dispatch,
            reasoning_awareness=reasoning_awareness,
            attachment_context=attachment_prompt_context,
            location_context=location_context,
            conversation_id=conversation_id,
        )
        mark("prompt:done")
        trace.complete_stage("prompt")

        trace.start_stage("model")
        mark("model:start")
        model_result = ModelResult.from_legacy_response(
            ask_model(
                model_request,
                trace=trace,
                model_parts=model_parts,
            )
        )
        response = model_result.to_legacy_response()
        mark("model:done")

        if not model_result.succeeded:
            error_detail = response.removeprefix("ERROR:").strip()
            trace.fail_stage("model", detail=error_detail)
            trace.set_error(error_detail)
        else:
            trace.complete_stage("model")

        trace.start_stage("context:user")
        mark("context:user:start")
        if conversation_id is None:
            add_message("user", message)
        else:
            add_message("user", message, conversation_id=conversation_id)
        mark("context:user:done")
        trace.complete_stage("context:user")

        if model_result.succeeded:
            trace.start_stage("context:assistant")
            mark("context:assistant:start")
            if conversation_id is None:
                add_message("assistant", response)
            else:
                add_message(
                    "assistant",
                    response,
                    conversation_id=conversation_id,
                )
            mark("context:assistant:done")
            trace.complete_stage("context:assistant")

        trace.set_result(
            "error" if not model_result.succeeded else "success"
        )

        mark("pipeline:complete")

        if not model_result.succeeded:
            trace.publish_failure()
        else:
            trace.publish_complete()

        if include_trace:
            return response, trace

        return response

    except Exception as error:
        trace.set_error(str(error))

        if trace.current_stage:
            trace.fail_stage(
                trace.current_stage,
                detail="Internal pipeline error",
            )

        trace.publish_failure()

        with open(log_path, "a", encoding="utf-8") as log:
            log.write("\\n=== PIPELINE STAGE EXCEPTION ===\\n")
            traceback.print_exc(file=log)
            log.write("=== END PIPELINE STAGE EXCEPTION ===\\n")

        raise

def chat(message, capability=None, attachments=None, location_context=None, conversation_id=None):
    """Send a message through Amiki's conversation pipeline."""
    try:
        return (
            run_pipeline(
                message,
                capability,
                attachments=attachments,
                location_context=location_context,
            )
            if conversation_id is None
            else run_pipeline(
                message,
                capability,
                attachments=attachments,
                location_context=location_context,
                conversation_id=conversation_id,
            )
        )
    except Exception:
        import traceback
        log_path = os.path.expanduser("~/amiki-api.log")
        with open(log_path, "a", encoding="utf-8") as log:
            log.write("\n=== AMIKI PIPELINE EXCEPTION ===\n")
            traceback.print_exc(file=log)
            log.write("=== END AMIKI PIPELINE EXCEPTION ===\n")
        return "ERROR: Unexpected failure in Amiki chat pipeline."
