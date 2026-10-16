"""agentcards – kártya-publikáló toolok (publish_*_card) a telefonos dashboardhoz."""
from __future__ import annotations

import json
import logging

from . import schemas, store

logger = logging.getLogger(__name__)
TOOLSET = "agentcards"


def _fail(error, details=None):
    out = {"success": False, "error": error}
    if details:
        out["details"] = details
    return json.dumps(out, ensure_ascii=False)


def register(ctx):
    def handle_publish_water_card(args, **kw):
        args = dict(args or {})
        severity = args.pop("severity", None)
        sid = kw.get("session_id") or kw.get("task_id")
        try:
            ev = schemas.build_event("water", args, severity, "agent", session_id=sid)
            store.append_event(ev)
        except schemas.CardValidationError as e:
            return _fail("validation_failed", e.errors)
        except (OSError, ValueError) as e:
            logger.exception("agentcards: store failed")
            return _fail(f"store_failed: {type(e).__name__}")
        logger.info("agentcards: water card %s published (%s)", ev["id"], ev["severity"])
        return json.dumps({"success": True, "card_id": ev["id"], "type": "water",
                           "severity": ev["severity"], "ts": ev["ts"]})

    ctx.register_tool(
        name="publish_water_card",
        toolset=TOOLSET,
        schema=schemas.PUBLISH_WATER_CARD_SCHEMA,
        handler=handle_publish_water_card,
        emoji="🌊",
    )
