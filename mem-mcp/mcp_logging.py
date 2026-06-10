import time
import os
import logging
from logging.handlers import RotatingFileHandler
import structlog
from functools import wraps
from typing import Any, Dict, Callable, Optional

# Ensure logs directory exists
LOG_DIR = "/app/logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "mcp_tools.log")

# Configure standard logging handler for the file
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5)
file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))

# Attach to the standard logger that structlog will wrap
logging.getLogger("mcp.memory").addHandler(file_handler)
logging.getLogger("mcp.memory").setLevel(logging.INFO)

# Configure structlog to use the standard library logging so messages reach the file handler
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer() if os.getenv("LOG_JSON") else structlog.dev.ConsoleRenderer(),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger("mcp.memory")

def log_mcp_interaction(
    tool_name: str,
    arguments: Dict[str, Any],
    result: Any = None,
    duration_ms: float = 0.0,
    error: Optional[str] = None,
    context: Optional[str] = None
):
    """
    Logs an MCP tool interaction with specific fields for refining 
    search queries, tool descriptions, and skill mapping.
    """
    # Detect "soft errors" where the tool returns an error message instead of raising
    is_soft_error = isinstance(result, str) and result.startswith("Error:")
    effective_success = error is None and not is_soft_error
    
    # If it's a soft error and no hard error was passed, use the result as the error message
    effective_error = error
    if is_soft_error and not effective_error:
        effective_error = result

    log_payload = {
        "mcp_event": "mcp_tool_execution",
        "tool": tool_name,
        "arguments": arguments,
        "duration_ms": round(duration_ms, 2),
        "success": effective_success,
        "context_hint": context,
    }

    if result:
        # Truncate result for logs to avoid bloat while keeping enough for analysis
        log_payload["result_summary"] = str(result)[:500]
    
    if effective_error:
        log_payload["error"] = str(effective_error)

    logger.info("tool_use_stats", **log_payload)

def monitor_mcp_tool(tool_name: str, context_provider: Optional[Callable] = None):
    """Decorator for async MCP tool handlers."""
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            start_time = time.perf_counter()
            ctx = context_provider() if context_provider else None
            try:
                res = await func(*args, **kwargs)
                duration = (time.perf_counter() - start_time) * 1000
                log_mcp_interaction(tool_name, kwargs, result=res, duration_ms=duration, context=ctx)
                return res
            except Exception as e:
                duration = (time.perf_counter() - start_time) * 1000
                log_mcp_interaction(tool_name, kwargs, error=str(e), duration_ms=duration, context=ctx)
                raise
        return wrapper
    return decorator