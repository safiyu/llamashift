#!/usr/bin/env python3
"""
MCP (Model Context Protocol) Server for LLM Model Management

This server implements the Model Context Protocol (JSON-RPC 2.0 based) to allow
AI agents to discover, start, stop, and manage LLM models via standardized tools.

Integration: Communicates with the main llama-switcher server.py REST API
to perform actual model operations.
"""

import json
import os
import sys
import uuid
import time
import urllib.request
import urllib.error
import urllib.parse
from urllib.parse import urlparse, parse_qs
from socketserver import ThreadingMixIn
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [MCP] %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)

# Main server API base URL (where server.py is running)
MAIN_SERVER_URL = os.environ.get("MAIN_SERVER_URL", "http://localhost:8002")

# Global registry of active SSE sessions (sessionId -> wfile)
sse_sessions = {}
sse_sessions_lock = threading.Lock()


class MCPProtocolError(Exception):
    """Base exception for MCP protocol errors."""
    def __init__(self, code, message, data=None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)


# =============================================================================
# MCP Tools Definition
# =============================================================================

MCP_TOOLS = [
    {
        "name": "list_models",
        "description": "List all available LLM models with their configurations and current status",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_model_status",
        "description": "Get the current status of all models (running/stopped) with resource usage",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "start_model",
        "description": "Start an LLM model. In single-port mode, stops any currently running model first. "
                      "In multi-port mode, can run multiple models simultaneously.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {
                    "type": "string",
                    "description": "The model identifier (e.g., 'qwen3-32b', 'gemma4-31b', 'qwen3-8b')"
                }
            },
            "required": ["model_id"]
        }
    },
    {
        "name": "stop_model",
        "description": "Stop a running LLM model. In single-port mode, stops whatever model is currently running.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {
                    "type": "string",
                    "description": "The model identifier to stop"
                }
            },
            "required": ["model_id"]
        }
    },
    {
        "name": "stop_all_models",
        "description": "Stop all running LLM models immediately",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_model_logs",
        "description": "Retrieve the last N lines of logs from a running model",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {
                    "type": "string",
                    "description": "The model identifier"
                },
                "lines": {
                    "type": "integer",
                    "description": "Number of log lines to retrieve (default: 100)",
                    "default": 100
                }
            },
            "required": ["model_id"]
        }
    },
    {
        "name": "get_gpu_info",
        "description": "Get GPU telemetry information including temperature, utilization, memory usage, and power draw",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_system_stats",
        "description": "Get system resource statistics including CPU load and memory usage",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_mode",
        "description": "Get the current deployment mode (single_port or multi_port)",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "set_mode",
        "description": "Change the deployment mode. single_port runs one model at a time on port 9000. "
                      "multi_port allows multiple models on different ports simultaneously. "
                      "Recommended to stop all models before switching modes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["single_port", "multi_port"],
                    "description": "The deployment mode to switch to"
                }
            },
            "required": ["mode"]
        }
    }
]


# =============================================================================
# MCP Protocol Handler
# =============================================================================

class MCPHandler(BaseHTTPRequestHandler):
    """Handles MCP JSON-RPC 2.0 protocol requests."""
    
    # Class-level reference to server for shutdown signaling
    server_instance = None
    
    def version_string(self):
        return "llama-switcher-MCP/1.0"
    
    def log_message(self, format, *args):
        logger.info(format % args)
    
    # ---- HTTP Method Handlers ----
    
    def do_GET(self):
        """Handle GET requests - serve SSE (Server-Sent Events) endpoint."""
        parsed = urlparse(self.path)
        
        if parsed.path in ('/sse', '/'):
            self._handle_sse()
        elif parsed.path == '/info':
            self._send_json(200, {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False}
                },
                "server": {
                    "name": "llama-switcher-mcp",
                    "version": "1.0.0"
                }
            })
        else:
            self._send_json(404, {"error": "Not found"})
    
    def do_POST(self):
        """Handle POST requests - JSON-RPC 2.0 endpoint."""
        parsed_path = urlparse(self.path)
        
        if parsed_path.path in ('/message', '/'):
            self._handle_json_rpc()
        else:
            self._send_json(404, {"error": "Not found"})
    
    # ---- SSE (Server-Sent Events) Support ----
    
    def _handle_sse(self):
        """Serve SSE endpoint for MCP clients."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        
        # Generate a unique session ID for this SSE stream connection
        session_id = str(uuid.uuid4())
        logger.info(f"Establishing SSE connection with session ID: {session_id}")
        
        # Send initial endpoint event to the client specifying the POST endpoint
        endpoint_event = f"event: endpoint\ndata: /message?sessionId={session_id}\n\n"
        self.wfile.write(endpoint_event.encode('utf-8'))
        self.wfile.flush()
        
        # Register the wfile globally so POST handlers can push events back to this client
        with sse_sessions_lock:
            sse_sessions[session_id] = self.wfile
        
        # Keep connection alive
        try:
            while True:
                time.sleep(30)
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with sse_sessions_lock:
                sse_sessions.pop(session_id, None)
            logger.info(f"SSE session {session_id} disconnected")
    
    # ---- JSON-RPC 2.0 Handler ----
    
    def _handle_json_rpc(self):
        """Process a JSON-RPC 2.0 request."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            request = json.loads(body.decode('utf-8'))
        except (json.JSONDecodeError, ValueError) as e:
            self._send_mcp_response(self._error_response(-32700, "Parse error", str(e)))
            return
        
        # Validate request structure
        if not isinstance(request, dict) or request.get('jsonrpc') != '2.0':
            self._send_mcp_response(self._error_response(-32600, "Invalid Request"))
            return
        
        method = request.get('method')
        request_id = request.get('id')
        params = request.get('params', {})
        
        # Route to appropriate handler
        handlers = {
            'initialize': lambda p: self._handle_initialize(request_id),
            'notifications/initialized': self._handle_initialized,
            'tools/list': lambda p: self._handle_tools_list(request_id),
            'tools/call': lambda p: self._handle_tools_call(p, request_id),
            'resources/list': lambda p: self._handle_resources_list(request_id),
            'ping': lambda p: self._handle_ping(request_id),
        }
        
        handler = handlers.get(method)
        if handler:
            try:
                response = handler(params)
                if response is not None:
                    self._send_mcp_response(response)
                else:
                    # If it's a notification, respond immediately to the POST
                    self.send_response(202)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
            except MCPProtocolError as e:
                self._send_mcp_response(self._error_response(e.code, e.message, e.data))
            except Exception as e:
                logger.error(f"Error handling {method}: {e}", exc_info=True)
                self._send_mcp_response(self._error_response(-32603, "Internal error", str(e)))
        else:
            self._send_mcp_response(self._error_response(-32601, "Method not found", method))
    
    # ---- JSON-RPC Response Helpers ----
    
    def _send_mcp_response(self, response):
        """Send JSON-RPC response either via active SSE connection or HTTP response fallback."""
        parsed_path = urlparse(self.path)
        query_params = parse_qs(parsed_path.query)
        
        session_id = None
        if 'sessionId' in query_params:
            session_id = query_params['sessionId'][0]
        elif 'session_id' in query_params:
            session_id = query_params['session_id'][0]
            
        target_wfile = None
        with sse_sessions_lock:
            if session_id and session_id in sse_sessions:
                target_wfile = sse_sessions[session_id]
            elif sse_sessions:
                # Fallback to the latest active session
                target_wfile = list(sse_sessions.values())[-1]
                
        if target_wfile:
            # 1. Immediately respond to the POST request with HTTP 202 Accepted
            self.send_response(202)
            self.send_header('Content-Length', '0')
            self.end_headers()
            
            # 2. Send the JSON-RPC response as a message event over the SSE stream
            try:
                response_msg = f"event: message\ndata: {json.dumps(response)}\n\n"
                target_wfile.write(response_msg.encode('utf-8'))
                target_wfile.flush()
            except Exception as e:
                logger.error(f"Failed to send JSON-RPC response over SSE: {e}")
        else:
            # Legacy fallback: Send directly in HTTP POST response body
            self._send_json_response_legacy(response)
            
    def _send_json_response_legacy(self, response):
        """Send a JSON-RPC response directly in HTTP response body (legacy mode)."""
        data = json.dumps(response).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    
    def _send_json(self, code, data):
        """Send a plain JSON response."""
        body = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    
    @staticmethod
    def _response(request_id, result):
        return {"jsonrpc": "2.0", "result": result, "id": request_id}
    
    @staticmethod
    def _error_response(code, message, data=None):
        error = {"code": code, "message": message}
        if data:
            error["data"] = data
        return {"jsonrpc": "2.0", "error": error, "id": None}
    
    # ---- JSON-RPC Method Handlers ----
    
    def _handle_initialize(self, request_id):
        """Handle the initialize request (first call from MCP client)."""
        return self._response(
            request_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"listChanged": False}
                },
                "server": {
                    "name": "llama-switcher-mcp",
                    "version": "1.0.0",
                    "description": "Manage LLM models via llama-switcher"
                }
            }
        )
    
    def _handle_initialized(self, params):
        """Handle the initialized notification (no response needed)."""
        logger.info("MCP client initialized")
        return None
    
    def _handle_tools_list(self, request_id):
        """Return the list of available tools."""
        return self._response(
            request_id,
            {"tools": MCP_TOOLS}
        )
    
    def _handle_tools_call(self, params, request_id):
        """Execute a tool call."""
        if not isinstance(params, dict):
            raise MCPProtocolError(-32602, "Invalid params")
        
        tool_name = params.get('name')
        arguments = params.get('arguments', {})
        
        if not tool_name:
            raise MCPProtocolError(-32602, "Missing tool name")
        
        # Dispatch to tool handler
        handlers = {
            'list_models': self._tool_list_models,
            'get_model_status': self._tool_get_model_status,
            'start_model': self._tool_start_model,
            'stop_model': self._tool_stop_model,
            'stop_all_models': self._tool_stop_all_models,
            'get_model_logs': self._tool_get_model_logs,
            'get_gpu_info': self._tool_get_gpu_info,
            'get_system_stats': self._tool_get_system_stats,
            'get_mode': self._tool_get_mode,
            'set_mode': self._tool_set_mode,
        }
        
        handler = handlers.get(tool_name)
        if not handler:
            raise MCPProtocolError(-32601, f"Tool not found: {tool_name}")
        
        result = handler(arguments)
        return self._response(request_id, result)
    
    def _handle_resources_list(self, request_id):
        """Return available resources (currently none)."""
        return self._response(request_id, {"resources": []})
    
    def _handle_ping(self, request_id):
        """Handle a ping request (health check)."""
        return self._response(request_id, {})
    
    # ---- Tool Implementations ----
    
    def _call_api(self, method, path, data=None):
        """Make an API call to the main server."""
        url = f"{MAIN_SERVER_URL}{path}"
        try:
            if data is not None:
                body = json.dumps(data).encode('utf-8')
                req = urllib.request.Request(url, data=body, method=method)
                req.add_header('Content-Type', 'application/json')
            else:
                req = urllib.request.Request(url, method=method)
            
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try:
                error_body = json.loads(e.read().decode('utf-8'))
            except:
                error_body = {}
            raise MCPProtocolError(-32000, error_body.get("error", "API error"), {"status": e.code})
        except urllib.error.URLError as e:
            raise MCPProtocolError(-32000, f"Cannot connect to main server: {e.reason}")
        except Exception as e:
            raise MCPProtocolError(-32000, str(e))
    
    def _tool_list_models(self, args):
        """List all available models from config."""
        try:
            data = self._call_api("GET", "/api/config")
            models = data.get("models", {})
            result = []
            for model_id, config in models.items():
                result.append({
                    "id": model_id,
                    "name": config.get("name", model_id),
                    "filename": config.get("filename"),
                    "port": config.get("port"),
                    "devices": config.get("devices", []),
                    "ctxSize": config.get("ctxSize"),
                })
            return {"models": result, "count": len(result)}
        except MCPProtocolError as e:
            return {"error": str(e), "models": []}
    
    def _tool_get_model_status(self, args):
        """Get current status of all models."""
        try:
            data = self._call_api("GET", "/api/status")
            return {
                "mode": data.get("mode"),
                "models": data.get("models", []),
                "host": data.get("host"),
                "mcp": data.get("mcp")
            }
        except MCPProtocolError as e:
            return {"error": str(e)}
    
    def _tool_start_model(self, args):
        """Start a model."""
        model_id = args.get("model_id")
        if not model_id:
            raise MCPProtocolError(-32602, "Missing required parameter: model_id")
        
        try:
            data = self._call_api("POST", "/api/start", {"model": model_id})
            return {
                "success": data.get("success"),
                "message": data.get("message"),
                "stopped": data.get("stopped", [])
            }
        except MCPProtocolError as e:
            return {"error": str(e)}
    
    def _tool_stop_model(self, args):
        """Stop a model."""
        model_id = args.get("model_id")
        if not model_id:
            raise MCPProtocolError(-32602, "Missing required parameter: model_id")
        
        try:
            data = self._call_api("POST", "/api/stop", {"model": model_id})
            return {
                "success": data.get("success"),
                "message": data.get("message")
            }
        except MCPProtocolError as e:
            return {"error": str(e)}
    
    def _tool_stop_all_models(self, args):
        """Stop all running models."""
        try:
            data = self._call_api("POST", "/api/stop_all")
            return {
                "success": data.get("success"),
                "message": data.get("message"),
                "stopped_count": len(data.get("stopped_pids", []))
            }
        except MCPProtocolError as e:
            return {"error": str(e)}
    
    def _tool_get_model_logs(self, args):
        """Get model logs."""
        model_id = args.get("model_id")
        if not model_id:
            raise MCPProtocolError(-32602, "Missing required parameter: model_id")
        
        lines = args.get("lines", 100)
        
        try:
            data = self._call_api("GET", f"/api/logs?model={model_id}&lines={lines}")
            return {
                "model": data.get("model"),
                "logs": data.get("logs", "")
            }
        except MCPProtocolError as e:
            return {"error": str(e)}
    
    def _tool_get_gpu_info(self, args):
        """Get GPU telemetry information."""
        try:
            data = self._call_api("GET", "/api/gpu")
            return data
        except MCPProtocolError as e:
            return {"error": str(e)}
    
    def _tool_get_system_stats(self, args):
        """Get system resource statistics."""
        try:
            data = self._call_api("GET", "/api/status")
            return {"host": data.get("host")}
        except MCPProtocolError as e:
            return {"error": str(e)}
    
    def _tool_get_mode(self, args):
        """Get current deployment mode."""
        try:
            data = self._call_api("GET", "/api/config")
            return {"mode": data.get("mode"), "modes": data.get("modes", [])}
        except MCPProtocolError as e:
            return {"error": str(e)}
    
    def _tool_set_mode(self, args):
        """Set deployment mode."""
        mode = args.get("mode")
        if mode not in ("single_port", "multi_port"):
            raise MCPProtocolError(-32602, "Invalid mode. Must be 'single_port' or 'multi_port'")
        
        try:
            data = self._call_api("POST", "/api/config", {"mode": mode})
            return {
                "success": data.get("success"),
                "mode": data.get("mode"),
                "message": data.get("message"),
                "warning": data.get("warning")
            }
        except MCPProtocolError as e:
            return {"error": str(e)}


# =============================================================================
# Server Lifecycle
# =============================================================================

class GracefulHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles graceful shutdown and concurrent requests."""
    daemon_threads = True
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shutdown_event = threading.Event()
    
    def signal_handler(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down MCP server...")
        self._shutdown_event.set()
    
    def shutdown(self):
        logger.info("Initiating MCP server shutdown...")
        self._shutdown_event.set()
        threading.Thread(target=self.shutdown_thread, daemon=True).start()
    
    def shutdown_thread(self):
        """Shutdown in a separate thread to avoid blocking."""
        self.shutdown()
        self.server_close()
    
    def serve_forever(self, poll_interval=0.5):
        """Serve requests until shutdown is called."""
        import signal
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        
        try:
            while not self._shutdown_event.is_set():
                self.handle_request()
        finally:
            self.server_close()


def run_mcp_server(port=28002):
    """Run the MCP server."""
    server_address = ('', port)
    httpd = GracefulHTTPServer(server_address, MCPHandler)
    logger.info(f"MCP Server starting on port {port}")
    logger.info(f"Connecting to main server at {MAIN_SERVER_URL}")
    
    # Verify main server is reachable
    try:
        req = urllib.request.Request(f"{MAIN_SERVER_URL}/api/status")
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = json.loads(resp.read().decode('utf-8'))
            logger.info(f"Main server is running (mode: {status.get('mode')})")
    except Exception as e:
        logger.warning(f"Could not connect to main server at {MAIN_SERVER_URL}: {e}")
        logger.warning("MCP server will retry connections on each API call")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("MCP Server shutting down...")
    finally:
        httpd.server_close()
        logger.info("MCP Server stopped")


if __name__ == "__main__":
    port = 28002
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"Invalid port number: {sys.argv[1]}. Using default 28002.")
    
    # Allow overriding main server URL via environment variable
    if len(sys.argv) > 2:
        os.environ["MAIN_SERVER_URL"] = sys.argv[2]
    
    run_mcp_server(port)