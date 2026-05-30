# MCP Server Improvements Task List

## P0 - Critical Bugs

### [ ] 1. Fix Double Shutdown Recursion Bug
- **File:** `mcp_server.py` (lines 627-634)
- **Issue:** `GracefulHTTPServer.shutdown()` calls `shutdown_thread()` which calls `self.shutdown()` recursively
- **Fix:** Refactor to set shutdown event and directly call `server_close()` from the shutdown thread

### [ ] 2. Migrate to Official MCP SDK
- **Files:** `mcp_server.py`, `requirements.txt`
- **Issue:** Custom JSON-RPC implementation instead of official Anthropic MCP Python SDK
- **Fix:** 
  - Add `mcp>=1.0.0` to requirements.txt
  - Rewrite server using `from mcp import Server` and official tool/resource decorators
  - Replace manual JSON-RPC handling with SDK's built-in transports

---

## P1 - High Priority

### [ ] 3. Add Input Validation on Tool Arguments
- **File:** `mcp_server.py` (tool handlers)
- **Issue:** Tool args accessed directly without validation/type checking
- **Fix:** Add Pydantic models for each tool's input schema, validate before calling handlers

### [ ] 4. Add Authentication on MCP Layer
- **File:** `mcp_server.py`
- **Issue:** All MCP tools accessible without authentication, bypassing main server PIN security
- **Fix:** Add auth middleware layer that validates API key or PIN before routing tool calls

### [ ] 5. Fix Error Handling Consistency
- **File:** `mcp_server.py` (lines 595-600)
- **Issue:** API errors wrapped in `{"error": str(e)}` creating non-standard MCP responses
- **Fix:** Return proper MCP error objects with `-32600` range error codes

---

## P2 - Medium Priority

### [ ] 6. Make Tools List Dynamic
- **File:** `mcp_server.py` (lines 176-188)
- **Issue:** Tools list cached at initialization, won't reflect changes
- **Fix:** Fetch tools list on each `tools/list` call, or add refresh endpoint

### [ ] 7. Add MCP-Specific Health Check
- **File:** `mcp_server.py`
- **Issue:** No `/health` endpoint to distinguish MCP server vs main server status
- **Fix:** Add `/health` endpoint that checks both MCP and main server connectivity

### [ ] 8. Add `serverInfo` to Initialize Response
- **File:** `mcp_server.py` (lines 193-198)
- **Issue:** Initialize response missing recommended `serverInfo` with `name` and `version`
- **Fix:** Add `"serverInfo": {"name": "llama-shift-mcp", "version": "1.0.0"}`

### [ ] 9. Add Request Cancellation Support
- **File:** `mcp_server.py`
- **Issue:** No `$/cancel` notification handling for long-running API calls
- **Fix:** Implement cancellation tracking with threading.Event per request

---

## P3 - Low Priority / Cleanup

### [ ] 10. Single Source of Truth for Port
- **File:** `mcp_server.py` (lines 650, 677)
- **Issue:** Port `28002` hardcoded in two places
- **Fix:** Use `MCP_PORT` environment variable with single default

### [ ] 11. Fix SSE Headers for Spec Compliance
- **File:** `mcp_server.py` (line 117)
- **Issue:** Missing `Cache-Control` and `Connection` headers
- **Fix:** Add proper SSE headers

### [ ] 12. Fix Non-Standard handle_request() Override
- **File:** `mcp_server.py` (lines 133-135)
- **Issue:** Manual readline + _handle_one_request bypasses parent parsing
- **Fix:** Either remove override or properly delegate to parent class