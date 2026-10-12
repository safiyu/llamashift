from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import urllib.error
import urllib.request
from typing import Any

import httpx

from mcp.server.fastmcp import FastMCP

# =============================================================================
# Logging
# =============================================================================

# Use a dedicated logger namespace to avoid polluting the main app's logging
logger = logging.getLogger("llama-shift.mcp")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [MCP] %(levelname)s: %(message)s"))
    logger.addHandler(handler)

# =============================================================================
# Configuration
# =============================================================================

MAIN_SERVER_URL: str = os.environ.get("MAIN_SERVER_URL", "http://localhost:8002")
MCP_AUTH_TOKEN: str = os.environ.get("MCP_AUTH_TOKEN", "")
API_TIMEOUT: int = int(os.environ.get("MCP_API_TIMEOUT", "30"))
MCP_PORT: int = int(os.environ.get("MCP_PORT", "28002"))
MCP_HOST: str = os.environ.get("MCP_HOST", "")

# Reusable httpx async client (lazy-initialized)
_http_client: httpx.AsyncClient | None = None



# =============================================================================
# MCP Server Instance
# =============================================================================

# Create the FastMCP server with standard metadata
mcp = FastMCP(
    name="llama-switcher-mcp",
    version="1.0.0",
    instructions="Manage LLM models via llama-shift. Use list_models to discover available models before starting or stopping them.",
)


# =============================================================================
# HTTP Helper: Async HTTP client using httpx
# =============================================================================

class MCPError(Exception):
    """Error raised when the main server returns an error or is unreachable."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.message = message
        self.status = status
        super().__init__(message)


def _build_headers() -> dict[str, str]:
    """Build headers for API requests, including auth token if configured."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if MCP_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {MCP_AUTH_TOKEN}"
    return headers


async def _get_client() -> httpx.AsyncClient:
    """Return the shared async HTTP client, creating it if needed."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            base_url=MAIN_SERVER_URL,
            headers=_build_headers(),
            timeout=httpx.Timeout(API_TIMEOUT),
        )
    return _http_client


async def _call_api(method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Make an async HTTP call to the main server REST API.

    Raises MCPError on HTTP errors or connection failures.
    """
    client = await _get_client()

    try:
        response = await client.request(method, path, json=data)

        # Handle HTTP errors
        if response.status_code >= 400:
            try:
                error_body = response.json()
            except Exception:
                error_body = {}

            if response.status_code in (401, 403):
                raise MCPError(
                    "Authentication failed. Check MCP_AUTH_TOKEN configuration.",
                    status=response.status_code,
                )
            if response.status_code == 503:
                raise MCPError(
                    "Main server is restarting, please try again.",
                    status=response.status_code,
                )
            raise MCPError(
                error_body.get("error", f"API error (HTTP {response.status_code})"),
                status=response.status_code,
            )

        return response.json()

    except httpx.TimeoutException:
        raise MCPError(
            f"Connection to main server timed out after {API_TIMEOUT}s",
            status=None,
        )
    except httpx.ConnectError as e:
        raise MCPError(f"Cannot connect to main server at {MAIN_SERVER_URL}: {e}")
    except MCPError:
        raise
    except Exception as e:
        raise MCPError(f"Unexpected error: {e}")


async def _close_client() -> None:
    """Gracefully close the shared HTTP client."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None



# =============================================================================
# Safe call wrapper — catches MCPError so tools return structured results
# =============================================================================

def _safe_call(operation: str, fn: Any, default: dict) -> dict:
    """Call *fn* (blocking) and catch MCPError, returning a friendly error dict.

    For sync wrappers around async operations we run inside asyncio.run.
    In FastMCP the tool handlers are already awaited, so _call_api can be
    called directly — this wrapper is kept for compatibility.
    """
    try:
        return fn()
    except MCPError as e:
        logger.error("%s failed (HTTP %s): %s", operation, e.status, e, exc_info=True)
        return {"error": str(e), "error_type": "MCPError", "http_status": e.status}
    except Exception as e:
        logger.error("%s failed: %s", operation, e, exc_info=True)
        return {"error": str(e), "error_type": type(e).__name__}


async def _safe_call_async(operation: str, coro: Any, default: dict) -> dict:
    """Await a coroutine and catch MCPError, returning a friendly error dict.

    *coro* should be a coroutine object (not an already-resolved result).
    If the coroutine raises, we return *default* augmented with error info.
    """
    try:
        return await coro  # type: ignore[return-value]
    except MCPError as e:
        logger.error("%s failed (HTTP %s): %s", operation, e.status, e, exc_info=True)
        return {"error": str(e), "error_type": "MCPError", "http_status": e.status}
    except Exception as e:
        logger.error("%s failed: %s", operation, e, exc_info=True)
        return {"error": str(e), "error_type": type(e).__name__}



# =============================================================================
# Async helper functions (used by resources and tools)
# =============================================================================

async def _do_list_models() -> dict[str, Any]:
    """Fetch the list of available models from the main server."""
    return await _call_api("GET", "/api/models")


async def _do_get_model_status() -> dict[str, Any]:
    """Fetch the current status of all models from the main server."""
    return await _call_api("GET", "/api/status")


async def _do_get_gpu_info() -> dict[str, Any]:
    """Fetch GPU telemetry from the main server."""
    return await _call_api("GET", "/api/gpu")


async def _do_get_system_stats() -> dict[str, Any]:
    """Fetch system stats from the main server."""
    data = await _call_api("GET", "/api/status")
    return {"host": data.get("host")}


async def _do_get_mode() -> dict[str, Any]:
    """Fetch deployment mode from the main server."""
    data = await _call_api("GET", "/api/config")
    return {"mode": data.get("mode"), "modes": data.get("modes", [])}


async def _do_start_model(model_id: str) -> dict[str, Any]:
    data = await _call_api("POST", "/api/start", {"model": model_id})
    return {
        "success": data.get("success"),
        "message": data.get("message"),
        "stopped": data.get("stopped", []),
    }


async def _do_stop_model(model_id: str) -> dict[str, Any]:
    data = await _call_api("POST", "/api/stop", {"model": model_id})
    return {
        "success": data.get("success"),
        "message": data.get("message"),
    }


async def _do_stop_all_models() -> dict[str, Any]:
    data = await _call_api("POST", "/api/stop_all")
    return {
        "success": data.get("success"),
        "message": data.get("message"),
        "stopped_count": len(data.get("stopped_pids", [])),
    }


async def _do_get_model_logs(model_id: str, lines: int) -> dict[str, Any]:
    data = await _call_api("GET", f"/api/logs?model={model_id}&lines={lines}")
    return {
        "model": data.get("model"),
        "logs": data.get("logs", ""),
    }


async def _do_set_mode(mode: str) -> dict[str, Any]:
    data = await _call_api("POST", "/api/config", {"mode": mode})
    return {
        "success": data.get("success"),
        "mode": data.get("mode"),
        "message": data.get("message"),
        "warning": data.get("warning"),
    }


# =============================================================================
# Resources (synchronous — read-only, call sync wrappers)
# =============================================================================

def _sync_models_list() -> dict[str, Any]:
    """Sync wrapper for list_models (for resource decorators)."""
    try:
        return asyncio.run(_do_list_models())
    except MCPError as e:
        return {"error": str(e), "error_type": "MCPError"}


def _sync_model_status() -> dict[str, Any]:
    """Sync wrapper for get_model_status."""
    try:
        return asyncio.run(_do_get_model_status())
    except MCPError as e:
        return {"error": str(e), "error_type": "MCPError"}


def _sync_health_check() -> dict[str, Any]:
    """Sync health check for resource."""
    try:
        req = urllib.request.Request(f"{MAIN_SERVER_URL}/api/status")
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = json.loads(resp.read().decode("utf-8"))
        return {
            "status": "ok",
            "main_server": MAIN_SERVER_URL,
            "mode": status.get("mode"),
            "auth_configured": bool(MCP_AUTH_TOKEN),
        }
    except Exception as e:
        return {
            "status": "degraded",
            "main_server": MAIN_SERVER_URL,
            "error": str(e),
        }


def _sync_gpu_info() -> dict[str, Any]:
    try:
        return asyncio.run(_do_get_gpu_info())
    except MCPError as e:
        return {"error": str(e), "error_type": "MCPError"}


def _sync_system_stats() -> dict[str, Any]:
    try:
        return asyncio.run(_do_get_system_stats())
    except MCPError as e:
        return {"error": str(e), "error_type": "MCPError"}


def _sync_mode() -> dict[str, Any]:
    try:
        return asyncio.run(_do_get_mode())
    except MCPError as e:
        return {"error": str(e), "error_type": "MCPError"}


@mcp.resource("config://models")
def resource_models() -> str:
    """Real-time listing of all available LLM models with their configurations."""
    return json.dumps(_sync_models_list(), indent=2)


@mcp.resource("status://models")
def resource_model_status() -> str:
    """Current running/stopped status of all models with resource usage."""
    return json.dumps(_sync_model_status(), indent=2)


@mcp.resource("health://server")
def resource_health() -> str:
    """Health status of the MCP server and connection to the main server."""
    return json.dumps(_sync_health_check(), indent=2)


@mcp.resource("gpu://info")
def resource_gpu_info() -> str:
    """GPU telemetry: temperature, utilization, memory, power."""
    return json.dumps(_sync_gpu_info(), indent=2)


@mcp.resource("system://stats")
def resource_system_stats() -> str:
    """System resource statistics including CPU load and memory usage."""
    return json.dumps(_sync_system_stats(), indent=2)


@mcp.resource("config://mode")
def resource_mode() -> str:
    """Current deployment mode (single_port or multi_port)."""
    return json.dumps(_sync_mode(), indent=2)



# =============================================================================
# Tools (async — proper FastMCP async handlers)
# =============================================================================


def _validate_model_id(model_id: str) -> dict[str, Any] | None:
    """Validate model_id parameter. Returns error dict or None."""
    if not model_id or not isinstance(model_id, str) or not model_id.strip():
        return {"error": "model_id must be a non-empty string"}
    return None


@mcp.tool()
async def list_models() -> dict[str, Any]:
    """List all available LLM models with their configurations and current status.

    Returns model IDs, names, filenames, ports, devices, and context sizes.
    Run this first to discover available model IDs before using other tools.
    """
    return await _safe_call_async(
        "list_models",
        _do_list_models(),
        {"models": [], "count": 0},
    )


@mcp.tool()
async def get_model_status() -> dict[str, Any]:
    """Get the current status of all models (running/stopped) with resource usage."""
    return await _safe_call_async(
        "get_model_status",
        _do_get_model_status(),
        {},
    )


@mcp.tool()
async def start_model(model_id: str) -> dict[str, Any]:
    """Start an LLM model by its identifier.

    In single-port mode, stops any currently running model first.
    In multi-port mode, can run multiple models simultaneously.
    """
    err = _validate_model_id(model_id)
    if err:
        return err
    return await _safe_call_async(
        f"start_model({model_id})",
        _do_start_model(model_id),
        {},
    )


@mcp.tool()
async def stop_model(model_id: str) -> dict[str, Any]:
    """Stop a running LLM model by its identifier."""
    err = _validate_model_id(model_id)
    if err:
        return err
    return await _safe_call_async(
        f"stop_model({model_id})",
        _do_stop_model(model_id),
        {},
    )


@mcp.tool()
async def stop_all_models() -> dict[str, Any]:
    """Stop all running LLM models immediately."""
    return await _safe_call_async(
        "stop_all_models",
        _do_stop_all_models(),
        {},
    )


@mcp.tool()
async def get_model_logs(model_id: str, lines: int = 100) -> dict[str, Any]:
    """Retrieve the last N lines of logs from a running model.

    Args:
        model_id: The model identifier
        lines: Number of log lines to retrieve (default: 100, max: 5000)
    """
    err = _validate_model_id(model_id)
    if err:
        return err
    clamped_lines = max(1, min(lines, 5000))
    return await _safe_call_async(
        f"get_model_logs({model_id})",
        _do_get_model_logs(model_id, clamped_lines),
        {},
    )


@mcp.tool()
async def get_gpu_info() -> dict[str, Any]:
    """Get GPU telemetry information including temperature, utilization,
    memory usage, and power draw."""
    return await _safe_call_async(
        "get_gpu_info",
        _do_get_gpu_info(),
        {},
    )


@mcp.tool()
async def get_system_stats() -> dict[str, Any]:
    """Get system resource statistics including CPU load and memory usage."""
    return await _safe_call_async(
        "get_system_stats",
        _do_get_system_stats(),
        {},
    )


@mcp.tool()
async def get_mode() -> dict[str, Any]:
    """Get the current deployment mode (single_port or multi_port)."""
    return await _safe_call_async(
        "get_mode",
        _do_get_mode(),
        {},
    )


@mcp.tool()
async def set_mode(mode: str) -> dict[str, Any]:
    """Change the deployment mode.

    Args:
        mode: 'single_port' runs one model at a time on port 9000.
              'multi_port' allows multiple models on different ports simultaneously.
              Recommended to stop all models before switching modes.
    """
    if mode not in ("single_port", "multi_port"):
        return {
            "error": f"Invalid mode '{mode}'. Must be 'single_port' or 'multi_port'."
        }
    return await _safe_call_async(
        f"set_mode({mode})",
        _do_set_mode(mode),
        {},
    )


@mcp.tool()
async def health_check() -> dict[str, Any]:
    """Check the health status of the MCP server and its connection to the main server."""
    return _sync_health_check()



# =============================================================================
# Server Entry Point
# =============================================================================

def _signal_handler() -> None:
    """Handle shutdown signals gracefully."""
    logger.info("Shutdown signal received, closing resources...")
    asyncio.run(_close_client())


def run_mcp_server(port: int | None = None, host: str | None = None) -> None:
    """Run the MCP server using the official mcp SDK.

    Uses SSE transport so the server can be discovered by MCP clients
    via the /sse endpoint and invoked via POST to /message.
    """
    port = port if port is not None else MCP_PORT
    host = host if host is not None else MCP_HOST

    # Log configuration at startup
    logger.info("MCP Server configuration:")
    logger.info("  - Main server: %s", MAIN_SERVER_URL)
    logger.info("  - Auth token: %s", "configured" if MCP_AUTH_TOKEN else "not set (unauthenticated)")
    logger.info("  - API timeout: %ds", API_TIMEOUT)
    logger.info("  - Port: %d", port)

    # Register signal handlers for graceful shutdown
    try:
        import signal as sig
        for s in (sig.SIGINT, sig.SIGTERM):
            sig.signal(s, lambda *_: asyncio.run(_close_client()))
    except (OSError, ValueError):
        # signal() only works in main thread; non-main threads skip gracefully
        pass

    # Verify main server is reachable at startup
    try:
        status = asyncio.run(_call_api("GET", "/api/status"))
        logger.info("Main server is running (mode: %s)", status.get("mode"))
    except MCPError as e:
        logger.warning("Could not connect to main server at %s: %s", MAIN_SERVER_URL, e)
        logger.warning("MCP server will retry connections on each API call")
    except Exception as e:
        logger.warning("Startup connectivity check failed: %s", e)

    logger.info("MCP Server starting on http://%s:%d", host or "*", port)

    # Use the SSE transport from the official SDK
    mcp.run(transport="sse", host=host or None, port=port)


if __name__ == "__main__":
    port = MCP_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"Invalid port number: {sys.argv[1]}. Using default {MCP_PORT}.")

    if len(sys.argv) > 2:
        os.environ["MAIN_SERVER_URL"] = sys.argv[2]

    run_mcp_server(port)

