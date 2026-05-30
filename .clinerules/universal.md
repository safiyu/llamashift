# Universal Rules

## Search Strategy
- NEVER use search_files tool — it is unreliable in this project
- ALWAYS use execute_command with rg instead:
  - rg -n "pattern" path/to/file
  - rg -n "pattern" --type py
  - rg -n "pattern" static/index.js
- 0 results from search_files = tool failed, use rg instead
- 0 results from rg = code doesn't exist, then create it
- To understand a file's structure, use list_code_definition_names, NOT read_file

## File Size Management
- NEVER read files over 500 lines in full
- For large files, use rg to locate the specific function first, then read only that section
- read_file line ranges are mandatory for files over 200 lines
- Maximum 200 lines per read_file call — use line ranges
- Do NOT include full file contents in task context — reference by function name only

## Context Discipline
- Do NOT read a file just to see its structure — use list_code_definition_names
- Do NOT re-read files you have already read in this session
- Do NOT include unchanged code in responses — reference it by name
- After completing a subtask, summarize what was done in 2-3 lines max
- You already have the full function list from list_code_definition_names

## Architecture (llama-shift)
- API endpoints → api.py
- GPU/process logic → process.py
- WebSocket/telemetry → telemetry.py
- Config load/save → config.py
- DO NOT add new functions to server.py — it is legacy
- For api.py: handle_api_gpu is the GPU endpoint, handle_api_files is the files endpoint
- For process.py: get_gpu_telemetry() is the GPU detection function

## Git Policy
- NO git actions unless explicitly asked

## Tool Usage Priority
- list_code_definition_names → understand structure
- rg via execute_command → find specific code
- read_file with line ranges → read only what is needed