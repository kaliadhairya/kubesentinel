import json
import logging
from typing import Optional
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from app.services.agent import sre_agent

logger = logging.getLogger("kubesentinel.api.chat")
router = APIRouter(prefix="/api/chat", tags=["Chat & SRE Copilot"])


class ChatRequest(BaseModel):
    message: str
    namespace: Optional[str] = "production"
    service: Optional[str] = "checkout-service"


@router.post("")
async def chat_stream(req: ChatRequest):
    """Streams autonomous SRE investigation steps, charts, and remediation actions via SSE."""

    async def event_generator():
        try:
            async for step in sre_agent.investigate_stream(
                query=req.message,
                namespace=req.namespace or "production",
                service_name=req.service or "checkout-service",
            ):
                payload = json.dumps(step)
                yield f"data: {payload}\n\n"
        except Exception as e:
            logger.error(f"Error in chat stream: {e}", exc_info=True)
            err_payload = json.dumps({"type": "error", "message": str(e)})
            yield f"data: {err_payload}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
