# Homelab AI Setup Guide
**System:** Ubuntu 26.04 LTS  
**Hardware:** AMD Radeon AI PRO R9700 (32GB) + NVIDIA RTX 4060 (8GB) + Intel Arc iGPU  
**Date:** 2026-05-25

---

## Overview

| Service | Port | Auto-start |
|---------|------|------------|
| Open WebUI | 3000 | Docker |
| ComfyUI | 3001 | systemd |
| Phi-4 Q8 (default LLM) | 9001 | systemd |
| LlamaShift (UI) | 8002 | manual |
| Other LLMs (on-demand) | 9000, 9002–9009 | manual / custom webapp |

---

## Mode Switcher (LlamaShift)

The LlamaShift web UI (`http://localhost:8002`) lets you toggle between two operating modes:

### Single-Port Mode (default)
- All models share **port 9000** (the "master port")
- Only **one model runs at a time**
- Starting a new model **auto-stops** whatever is currently running
- **Ideal for Open WebUI** — single endpoint to configure
- Toggle the switch in the header bar: `SINGLE` ↔ `MULTI`
- Mode is **persisted** to `mode_config.json` (survives server restarts)

### Multi-Port Mode
- Each model runs on its **own dedicated port** (9000, 9002–9009)
- **Multiple models can run simultaneously**
- GPU overlap handling still applies (overlapping GPUs auto-stop competing models)
- **Ideal for testing/comparing** models side-by-side
- Toggle the switch in the header bar: `MULTI` ↔ `SINGLE`

### Switching Modes
- The mode toggle is in the **header bar** between the system metrics and action buttons
- Switching modes **does not force-stop** running models — you'll get a warning
- After switching, the UI will refresh to reflect the new mode
- In single-port mode, the port column in model cards shows `9000` for all models

### Open WebUI Configuration

**For single-port mode (recommended):**
```
OPENAI_API_BASE_URLS=http://host.docker.internal:9000/v1
OPENAI_API_KEYS=none
OPENAI_API_BASE_URL_NAMES=Your-Model-Name
```

**For multi-port mode:**
```
OPENAI_API_BASE_URLS=http://host.docker.internal:9000/v1;http://host.docker.internal:9001/v1;...
OPENAI_API_KEYS=none;none;...
OPENAI_API_BASE_URL_NAMES=Model1;Model2;...
```

---

## Part 1: llama.cpp Dual GPU Setup

> Full details in `dual-gpu-llama-setup.md`. Summary below.

### What Works
- ROCm 7.x from Ubuntu repos (`sudo apt install rocm`)
- CUDA 12.6 from NVIDIA repo (NOT `apt install nvidia-cuda-toolkit`)
- GCC 13 as CUDA host compiler
- CUDA header patch for glibc conflict (6x `noexcept(true)` sed commands)
- llama.cpp built with `-DGGML_HIP=ON -DGGML_VULKAN=ON` (NOT CUDA+HIP together)
- Dual GPU: R9700 (ROCm0) + RTX 4060 (Vulkan2) for 70B models

### What Didn't Work
- ROCm 6.x — no gfx1201 kernels
- `apt install nvidia-cuda-toolkit` — removes ROCm
- CUDA 12.8 — GCC 15 conflict
- `-DGGML_HIP=ON -DGGML_CUDA=ON` together — HIP takes over CUDA backend
- `--tensor-split` with mixed backends — causes OOM; use auto-fit instead

### Build Command
```bash
HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$(hipconfig -R)" cmake -S . -B build \
  -DGGML_HIP=ON \
  -DGGML_VULKAN=ON \
  -DGPU_TARGETS=gfx1201 \
  -DCMAKE_BUILD_TYPE=Release

cmake --build build --config Release -- -j$(nproc)
```

### Model Serving
```bash
export RADV_DEBUG=nocompute
export GPU_MAX_HW_QUEUES=1

# Example: 70B on both GPUs
./build/bin/llama-server \
  --model ~/models/Llama-3.3-70B-Instruct-Q4_K_M.gguf \
  --device ROCm0,Vulkan2 \
  --ctx-size 512 \
  -np 1 \
  --port 9006
```

---

## Part 2: Open WebUI

### Install
```bash
# Requires Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# log out and back in
```

### docker-compose.yml
```yaml
services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: open-webui
    restart: unless-stopped
    volumes:
      - /home/safiyu/Homelab/openwebui:/app/backend/data
    environment:
      # All llama.cpp model endpoints (OpenAI-compatible)
      # 9000 = GPT-OSS 20B
      # 9001 = Phi-4 Q8
      # 9002 = Gemma 4 31B IT
      # 9003 = Qwen 3.6 27B
      # 9004 = DeepSeek R1 Distill 32B
      # 9005 = Qwen3 Coder 30B A3B (Q6_K quantization)
      # 9006 = Llama 3.3 70B Instruct (dual GPU)
      # 9007 = Qwen 3.6 35B A3B Vision (single GPU)
      # 9008 = Qwen3 8B
      # 9009 = Qwen 3.6 35B A3B Vision (dual GPU, 256k context)
      - OPENAI_API_BASE_URLS=http://host.docker.internal:9000/v1;http://host.docker.internal:9001/v1;http://host.docker.internal:9002/v1;http://host.docker.internal:9003/v1;http://host.docker.internal:9004/v1;http://host.docker.internal:9005/v1;http://host.docker.internal:9006/v1;http://host.docker.internal:9007/v1;http://host.docker.internal:9008/v1;http://host.docker.internal:9009/v1
      - OPENAI_API_KEYS=none;none;none;none;none;none;none;none;none;none
      - OPENAI_API_BASE_URL_NAMES=GPT-OSS-20B;Phi-4-Q8;Gemma4-31B;Qwen3.6-27B;DeepSeek-R1-32B;Qwen3-Coder-30B-A3B;Llama3.3-70B;Qwen3.6-35B-Vision;Qwen3-8B;Qwen3.6-35B-Vision-Dual
      - ENABLE_OLLAMA_API=false
      - WEBUI_AUTH=false
      - WEBUI_NAME=Local AI
      - PORT=3000
    network_mode: host
```

```bash
docker compose up -d
```

### What Didn't Work
- `network_mode: bridge` with `host.docker.internal` — container couldn't reach host ports
- `ports: "3000:8080"` with `network_mode: host` — incompatible, use `PORT=` env var instead
- `OPENAI_API_BASE_URLS` with existing DB volume — env vars only seed on first run; wipe volume if changing

### Notes
- Models only appear in UI after llama-server is running on that port
- Web search: use DuckDuckGo (no API key needed) — Settings → Admin Panel → Web Search
- File access: built-in, users upload directly in chat
- System prompt for Phi-4:
  ```
  You are a helpful, knowledgeable AI assistant. You are direct, friendly, and thorough. 
  You provide accurate, well-structured responses and acknowledge uncertainty when appropriate. 
  For coding tasks, write clean and well-commented code. For complex topics, use clear 
  explanations with examples. Keep responses concise unless detail is specifically needed. 
  When given access to the internet, use it to provide up-to-date and accurate information. 
  When given files, read and analyze them carefully before responding.
  ```

---

## Part 3: Phi-4 Auto-start (systemd)

```bash
sudo tee /etc/systemd/system/llama-phi4.service << 'EOF'
[Unit]
Description=llama.cpp - Phi-4 Q8 (port 9001)
After=network.target

[Service]
Type=simple
User=safiyu
WorkingDirectory=/home/safiyu

Environment=RADV_DEBUG=nocompute
Environment=GPU_MAX_HW_QUEUES=1
Environment=PATH=/usr/local/cuda-12.6/bin:/usr/bin:/bin

ExecStart=/home/safiyu/llama.cpp/build/bin/llama-server \
    --model /home/safiyu/models/phi-4-Q8_0.gguf \
    --device ROCm0 \
    -ngl 99 \
    --ctx-size 8192 \
    --port 9001 \
    --host 0.0.0.0

Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable llama-phi4
sudo systemctl start llama-phi4
```

---

## Part 4: ComfyUI (Image Generation)

### What Didn't Work
- **Automatic1111** — too many legacy dependency conflicts with Python 3.14, setuptools 70+, clip package broken
- **ROCm 6.2 PyTorch wheels** — missing `libroctracer64.so.4` on ROCm 7.x system
- **ROCm 7.2.1 AMD wheels** — same missing library issue (system has ROCm 7.1.1)
- **ROCm 7.1.0 AMD wheels** — 404, wrong filename
- **python3.12 venv** — system `python3-torch-rocm` package only built for Python 3.14
- **`--system-site-packages` venv** — pip kept overriding system torch with CUDA version
- **FLUX full precision (BF16)** — GPU hang at 8GB VRAM loading t5xxl_fp16 text encoder
- **`requirements.txt` installing torch** — always pulled CUDA version, overrode ROCm torch
- **Running Python from `~/ComfyUI` directory** — `torch/_C` folder conflict crashes import

### What Works

#### 1. Install system ROCm PyTorch (Python 3.14)
```bash
sudo apt install -y python3-torch-rocm libtorch-rocm-2.9
```

#### 2. Clone ComfyUI
```bash
cd ~
git clone https://github.com/comfyanonymous/ComfyUI
```

#### 3. Create venv with system site packages using Python 3.14
```bash
cd ~/ComfyUI
python3.14 -m venv venv --system-site-packages
```

#### 4. Remove any user-installed CUDA torch that overrides system ROCm torch
```bash
rm -rf ~/.local/lib/python3.14/site-packages/torch*
rm -rf ~/.local/lib/python3.14/site-packages/torchvision*
rm -rf ~/.local/lib/python3.14/site-packages/torchaudio*
rm -rf ~/.local/lib/python3.14/site-packages/triton*
rm -rf ~/.local/lib/python3.14/site-packages/nvidia*
```

#### 5. Install ComfyUI deps (skip torch)
```bash
source ~/ComfyUI/venv/bin/activate
pip install -r requirements.txt --ignore-installed torch torchvision torchaudio
deactivate
```

#### 6. Verify (must run from outside ComfyUI directory)
```bash
cd ~
source ~/ComfyUI/venv/bin/activate
python -c "import torch; print(torch.__version__); print(torch.cuda.get_device_name(0))"
# Expected: 2.9.1+debian / AMD Radeon AI PRO R9700
deactivate
```

#### 7. Install systemd service
```bash
sudo tee /etc/systemd/system/comfyui.service << 'EOF'
[Unit]
Description=ComfyUI Image Generation Server
After=network.target

[Service]
Type=simple
User=safiyu
WorkingDirectory=/home/safiyu/ComfyUI

Environment=HSA_OVERRIDE_GFX_VERSION=11.0.0
Environment=ROCM_PATH=/opt/rocm
Environment=RADV_DEBUG=nocompute
Environment=GPU_MAX_HW_QUEUES=1

ExecStart=/home/safiyu/ComfyUI/venv/bin/python main.py \
    --listen 0.0.0.0 \
    --port 3001 \
    --use-split-cross-attention \
    --disable-async-offload

Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable comfyui
sudo systemctl start comfyui
```

### Models

| File | Folder |
|------|--------|
| `flux1-dev-fp8.safetensors` | `models/checkpoints/` |
| `clip_l.safetensors` | `models/text_encoders/` |
| `t5xxl_fp8_e4m3fn_scaled.safetensors` | `models/text_encoders/` |
| `ae.safetensors` | `models/vae/` |

Download:
```bash
# Requires HuggingFace token with FLUX.1-dev license accepted
# https://huggingface.co/black-forest-labs/FLUX.1-dev

cd ~/ComfyUI/models/checkpoints
wget --header="Authorization: Bearer YOUR_HF_TOKEN" \
  https://huggingface.co/Kijai/flux-fp8/resolve/main/flux1-dev-fp8.safetensors

cd ~/ComfyUI/models/text_encoders
wget --header="Authorization: Bearer YOUR_HF_TOKEN" \
  https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors
wget --header="Authorization: Bearer YOUR_HF_TOKEN" \
  https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors

cd ~/ComfyUI/models/vae
wget --header="Authorization: Bearer YOUR_HF_TOKEN" \
  https://huggingface.co/black-forest-labs/FLUX.1-dev/resolve/main/ae.safetensors
```

### FLUX Notes
- Use `flux1-dev-fp8.safetensors` (single checkpoint) — loads with `CheckpointLoaderSimple`
- Do NOT use full `flux1-dev.safetensors` (23GB BF16) — causes GPU hang on gfx1201
- Do NOT use `t5xxl_fp16.safetensors` — too large, causes GPU hang
- Stop Phi-4 before generating images to free VRAM: `sudo systemctl stop llama-phi4`

---

## Part 5: LLM Model Reference

| Alias | File | Port | Device | Context | Notes |
|-------|------|------|--------|---------|-------|
| gpt_oss | openai_gpt-oss-20b-Q4_K_M.gguf | 9000 | ROCm0 | 128k | GPT-OSS 20B |
| phi4 | phi-4-Q8_0.gguf | 9001 | ROCm0 | 8k | Default, auto-starts via systemd |
| gemma3_27b | google_gemma-4-31B-it-Q4_K_M.gguf | 9002 | ROCm0 | 128k | Gemma 4 31B IT |
| qwen32 | Qwen_Qwen3.6-27B-Q4_K_M.gguf | 9003 | ROCm0 | 128k | Qwen 3.6 27B |
| r1 | DeepSeek-R1-Distill-Qwen-32B-Q4_K_M.gguf | 9004 | ROCm0 | 128k | DeepSeek R1 Distill 32B |
| qwen3_coder | Qwen3-Coder-30B-A3B-Instruct-Q6_K.gguf | 9005 | ROCm0 | 128k | Qwen3 Coder 30B A3B (Q6_K quant) |
| llama3_70b | Llama-3.3-70B-Instruct-Q4_K_M.gguf | 9006 | ROCm0,Vulkan2 | 8k | 70B dual GPU, -np 1 |
| qwen36 | Qwen3.6-35B-A3B-Q6_K.gguf | 9007 | ROCm0 | 128k | Vision model (requires mmproj) |
| qwen3_8b | Qwen_Qwen3-8B-Q8_0.gguf | 9008 | ROCm0 | 128k | Qwen3 8B |
| qwen36_dual | Qwen3.6-35B-A3B-Q6_K.gguf | 9009 | ROCm0,Vulkan2 | 256k | Dual GPU, -np 1, vision (requires mmproj) |

---

## Key Lessons

1. `python3` on Ubuntu 26.04 is Python 3.14 — system ROCm torch is built for 3.14
2. Never run `python -c "import torch"` from inside `~/ComfyUI` — torch folder conflict
3. Always clean `~/.local/lib/python3.14/site-packages/torch*` after any accidental pip install
4. `pip install -r requirements.txt` will pull CUDA torch — always use `--ignore-installed torch torchvision torchaudio`
5. FLUX full precision needs >32GB VRAM on gfx1201 — use FP8 checkpoint instead
6. Phi-4 and ComfyUI cannot run simultaneously — stop one before starting the other
7. `network_mode: host` in Docker — use `PORT=` env var, not `ports:` mapping
8. llama.cpp dual GPU: HIP+CUDA together doesn't work — use HIP+Vulkan instead