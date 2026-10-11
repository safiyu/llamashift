#!/usr/bin/env python3
"""
Model downloader utility for LlamaShift.
Downloads GGUF models directly into ~/models with progress bar.
"""

import os
import sys
import argparse
from huggingface_hub import hf_hub_download

PRESET_MODELS = {
    "gemma4": {
        "repo_id": "unsloth/gemma-4-26B-A4B-it-GGUF",
        "filename": "gemma-4-26B-A4B-it-UD-Q5_K_XL.gguf",
        "desc": "Google Gemma 4 26B-A4B-it (UD-Q5_K_XL, ~19.8 GB)",
    },
    "gemma4-q6": {
        "repo_id": "unsloth/gemma-4-26B-A4B-it-GGUF",
        "filename": "gemma-4-26B-A4B-it-UD-Q6_K_XL.gguf",
        "desc": "Google Gemma 4 26B-A4B-it (UD-Q6_K_XL, ~21.7 GB)",
    },
    "nemotron30b": {
        "repo_id": "unsloth/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF",
        "filename": "NVIDIA-Nemotron-3.5-Lightning-30B-A3B-UD-Q4_K_XL.gguf",
        "desc": "NVIDIA Nemotron-3.5-Lightning-30B-A3B (~18.5 GB)",
    }
}

def main():
    parser = argparse.ArgumentParser(description="Download GGUF models for LlamaShift")
    parser.add_argument("model", nargs="?", choices=list(PRESET_MODELS.keys()), default="gemma4",
                        help="Preset model to download (default: gemma4)")
    parser.add_argument("--dest", default="/home/safiyu/models",
                        help="Destination directory (default: /home/safiyu/models)")
    args = parser.parse_args()

    preset = PRESET_MODELS[args.model]
    dest_dir = os.path.expanduser(args.dest)
    os.makedirs(dest_dir, exist_ok=True)

    target_file = os.path.join(dest_dir, preset["filename"])
    if os.path.exists(target_file):
        print(f"[!] File already exists: {target_file}")
        sys.exit(0)

    print(f"[*] Downloading {preset['desc']}...")
    print(f"    From: {preset['repo_id']}")
    print(f"    To:   {target_file}")

    downloaded = hf_hub_download(
        repo_id=preset["repo_id"],
        filename=preset["filename"],
        local_dir=dest_dir,
        local_dir_use_symlinks=False
    )
    print(f"\n[✓] Successfully downloaded to {downloaded}!")

if __name__ == "__main__":
    main()
