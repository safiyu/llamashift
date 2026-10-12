#!/usr/bin/env python3
"""
VRAM pre-flight calculator for llama-shift.

Provides:
  * parse_gguf_metadata()  - read GGUF header architecture fields without loading tensors
  * estimate_vram_usage()  - analytical weights + KV-cache + overhead estimate
  * calculate_optimal_configuration() - auto-tune nGpuLayers / KV-quant / flash-attn

The formulas follow the spec in the LlamaShift 2.0 design:

    VRAM_total = VRAM_weights + VRAM_kv + VRAM_overhead
    VRAM_kv    = 2 * N_layers * N_ctx * N_kv_heads * D_head * bytes_per_elem * N_parallel
                 (the leading "2" accounts for the separate K and V caches)

bytes_per_elem depends on the KV-cache quant type:
    f16 -> 2, q8_0 -> 1, q4_0 -> 0.5
"""

import os
import struct
from collections import OrderedDict

# ─── GGUF low-level helpers ──────────────────────────────────────────────

GGUF_MAGIC = 0x4647474C  # "GGML" little-endian
GGUF_VERSION_3 = 3

# GGUF value types (v3)
_GGUF_T_UINT8 = 0
_GGUF_T_INT8 = 1
_GGUF_T_UINT16 = 2
_GGUF_T_INT16 = 3
_GGUF_T_UINT32 = 4
_GGUF_T_INT32 = 5
_GGUF_T_FLOAT32 = 6
_GGUF_T_BOOL = 7
_GGUF_T_STRING = 8
_GGUF_T_ARRAY = 9
_GGUF_T_UINT64 = 10
_GGUF_T_INT64 = 11
_GGUF_T_FLOAT64 = 12


class _GGUFReader:
    """Minimal streaming GGUF header reader (metadata + tensor count only)."""

    def __init__(self, filepath, max_bytes=16 * 1024 * 1024):
        self.f = open(filepath, "rb")
        self.max_bytes = max_bytes

    def u8(self):
        b = self.f.read(1)
        return b[0] if b else None

    def u32(self):
        b = self.f.read(4)
        if len(b) < 4:
            return None
        return struct.unpack("<I", b)[0]

    def u64(self):
        b = self.f.read(8)
        if len(b) < 8:
            return None
        return struct.unpack("<Q", b)[0]

    def i64(self):
        b = self.f.read(8)
        if len(b) < 8:
            return None
        return struct.unpack("<q", b)[0]

    def f32(self):
        b = self.f.read(4)
        if len(b) < 4:
            return None
        return struct.unpack("<f", b)[0]

    def f64(self):
        b = self.f.read(8)
        if len(b) < 8:
            return None
        return struct.unpack("<d", b)[0]

    def string(self):
        ln = self.u64()
        if ln is None or ln > 10 * 1024 * 1024:
            return None
        data = self.f.read(ln)
        if len(data) < ln:
            return None
        return data.decode("utf-8", errors="replace")

    def read_value(self, vtype):
        if vtype in (_GGUF_T_UINT8, _GGUF_T_BOOL):
            return self.u8()
        if vtype == _GGUF_T_INT8:
            v = self.u8()
            return -128 + v if v is not None and v > 127 else v
        if vtype == _GGUF_T_UINT16:
            b = self.f.read(2)
            return struct.unpack("<H", b)[0] if len(b) == 2 else None
        if vtype == _GGUF_T_INT16:
            b = self.f.read(2)
            return struct.unpack("<h", b)[0] if len(b) == 2 else None
        if vtype == _GGUF_T_UINT32:
            return self.u32()
        if vtype == _GGUF_T_INT32:
            b = self.f.read(4)
            return struct.unpack("<i", b)[0] if len(b) == 4 else None
        if vtype == _GGUF_T_FLOAT32:
            return self.f32()
        if vtype == _GGUF_T_UINT64:
            return self.u64()
        if vtype == _GGUF_T_INT64:
            return self.i64()
        if vtype == _GGUF_T_FLOAT64:
            return self.f64()
        if vtype == _GGUF_T_STRING:
            return self.string()
        if vtype == _GGUF_T_ARRAY:
            atype = self.u32()
            alen = self.u64()
            if atype is None or alen is None or alen > 100000:
                return []
            return [self.read_value(atype) for _ in range(alen)]
        return None

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


def parse_gguf_metadata(filepath):
    """Parse GGUF architecture metadata without loading tensor weights.

    Returns a dict with the fields needed for VRAM sizing:
      n_layer, n_head, n_head_kv, head_dim, n_embd, n_ctx (file default),
      n_ff, file_size, arch.
    Missing fields are None.
    """
    meta = {
        "arch": None,
        "n_layer": None,
        "n_head": None,
        "n_head_kv": None,
        "head_dim": None,
        "n_embd": None,
        "n_ff": None,
        "n_ctx": None,
        "file_size": 0,
    }
    try:
        meta["file_size"] = os.path.getsize(filepath)
    except OSError:
        return meta

    r = _GGUFReader(filepath)
    try:
        magic = r.u32()
        if magic != GGUF_MAGIC:
            return meta
        version = r.u32()
        if version is None or version < GGUF_VERSION_3:
            return meta
        tensor_count = r.u64()
        kv_count = r.u64()
        if kv_count is None or kv_count > 5000:
            return meta

        kv = OrderedDict()
        for _ in range(kv_count):
            key = r.string()
            if key is None:
                break
            vtype = r.u32()
            if vtype is None:
                break
            kv[key] = r.read_value(vtype)

        # Derive sizing fields, resolving aliases across architectures.
        def pick(*names):
            for n in names:
                if n in kv and kv[n] is not None:
                    return kv[n]
            return None

        meta["arch"] = pick("general.architecture")
        meta["n_layer"] = _to_int(pick("block_count", "n_layer"))
        meta["n_head"] = _to_int(pick("attention.head_count"))
        meta["n_head_kv"] = _to_int(pick("attention.head_count_kv"))
        meta["n_embd"] = _to_int(pick("embedding_length"))
        meta["n_ff"] = _to_int(pick("ffn_length"))
        meta["n_ctx"] = _to_int(pick("context_length", "ctx_length"))
        # head_dim = n_embd / n_head
        if meta["n_embd"] and meta["n_head"]:
            meta["head_dim"] = meta["n_embd"] // meta["n_head"]
    except Exception:
        pass
    finally:
        r.close()
    return meta


def _to_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


# ─── Estimation ──────────────────────────────────────────────────────────

# KV-cache quant types -> bytes per element
_KV_BYTES = {
    "f16": 2.0,
    "bf16": 2.0,
    "q8_0": 1.0,
    "q4_0": 0.5,
}

# Rough per-model runtime overhead (compute buffers, CUDA/ROCm workspace,
# embeddings, etc.) in MiB. Scales gently with context.
_BASE_OVERHEAD_MIB = 512.0
_OVERHEAD_PER_KCTX_MIB = 16.0


def _kv_bytes_per_elem(kv_quant):
    return _KV_BYTES.get((kv_quant or "f16").lower(), 2.0)


def estimate_vram_usage(model_cfg, ctx_size=None, n_gpu_layers=None, kv_quant=None):
    """Estimate VRAM footprint (in MiB) for a model configuration.

    Falls back to file-size-derived estimates when GGUF metadata is unavailable.
    Returns a dict with a full breakdown plus a recommended layer count.
    """
    data_dir = model_cfg.get("dataDir")
    filename = model_cfg.get("filename", "")
    if data_dir is None:
        from config import _CONFIG
        data_dir = _CONFIG.get("dataDir", "~/models")
    filepath = os.path.join(os.path.expanduser(data_dir), filename)

    meta = parse_gguf_metadata(filepath) if os.path.exists(filepath) else {}
    file_bytes = meta.get("file_size") or 0
    file_mib = file_bytes / (1024 * 1024)

    ctx = int(ctx_size or model_cfg.get("ctxSize", 4096))
    n_parallel = int(model_cfg.get("nParallel", 1) or 1)
    kv_quant = kv_quant or model_cfg.get("kvQuant", "f16")
    bpe = _kv_bytes_per_elem(kv_quant)

    n_layer = meta.get("n_layer")
    n_head_kv = meta.get("n_head_kv") or meta.get("n_head")
    head_dim = meta.get("head_dim")

    # ── KV cache (full, i.e. everything that could be GPU-resident) ──
    kv_mib = 0.0
    if n_layer and n_head_kv and head_dim:
        # 2 (K+V) * layers * ctx * kv_heads * head_dim * bytes * parallel
        kv_bytes = 2 * n_layer * ctx * n_head_kv * head_dim * bpe * n_parallel
        kv_mib = kv_bytes / (1024 * 1024)

    # ── Weights (GPU-resident portion) ──
    ngl = model_cfg.get("nGpuLayers", 99)
    ngl = int(ngl) if n_gpu_layers is None else int(n_gpu_layers)
    total_layers = n_layer if n_layer else (ngl if ngl > 0 else 1)
    if ngl >= 99 or ngl >= total_layers:
        gpu_weight_fraction = 1.0
    else:
        gpu_weight_fraction = max(0.0, min(1.0, ngl / max(total_layers, 1)))
    weight_mib = file_mib * gpu_weight_fraction

    # ── Overhead ──
    overhead_mib = _BASE_OVERHEAD_MIB + _OVERHEAD_PER_KCTX_MIB * (ctx / 1024.0) * n_parallel

    total_mib = weight_mib + kv_mib + overhead_mib

    return {
        "filename": filename,
        "file_size_mib": round(file_mib, 1),
        "file_size_human": _human_size(file_bytes),
        "arch": meta.get("arch"),
        "n_layer": n_layer,
        "n_head": meta.get("n_head"),
        "n_head_kv": n_head_kv,
        "head_dim": head_dim,
        "n_embd": meta.get("n_embd"),
        "ctx_size": ctx,
        "n_parallel": n_parallel,
        "n_gpu_layers": ngl,
        "kv_quant": kv_quant,
        "breakdown": {
            "weights_mib": round(weight_mib, 1),
            "kv_cache_mib": round(kv_mib, 1),
            "overhead_mib": round(overhead_mib, 1),
        },
        "total_mib": round(total_mib, 1),
        "total_human": _human_size(int(total_mib * 1024 * 1024)),
        "metadata_complete": bool(n_layer and n_head_kv and head_dim),
    }


def calculate_optimal_configuration(model_cfg, available_vram_mb, free_vram_mb=None):
    """Auto-tune a model configuration to fit within available VRAM.

    Returns a dict with recommended nGpuLayers, kv_quant, flash_attention and
    a list of human-readable recommendations, plus a `fits` boolean.
    """
    available = float(available_vram_mb or 0)
    free = float(free_vram_mb if free_vram_mb is not None else available)

    base = estimate_vram_usage(model_cfg, kv_quant="f16")
    total_full = base["total_mib"]
    bd = base["breakdown"]

    recommendations = []
    result = {
        "fits": total_full <= free,
        "available_vram_mib": round(available, 1),
        "free_vram_mib": round(free, 1),
        "estimated_total_mib": round(total_full, 1),
        "kv_quant": "f16",
        "n_gpu_layers": base.get("n_gpu_layers"),
        "flash_attention": True,
        "recommendations": recommendations,
    }

    if not base["metadata_complete"]:
        recommendations.append(
            "GGUF metadata unavailable; using file-size weight estimate "
            "and default KV sizing. Results are approximate."
        )

    if result["fits"]:
        recommendations.append(
            "Estimated footprint fits within free VRAM. Flash attention recommended "
            "to further reduce KV-cache memory."
        )
        return result

    # ── Not enough VRAM: apply progressive mitigation ──
    kv_mib_full = bd["kv_cache_mib"]

    # Step 1: KV quantization q8_0 (halves KV)
    kv_q8 = kv_mib_full * 0.5
    total_q8 = base["total_mib"] - kv_mib_full + kv_q8
    if total_q8 <= free:
        result["kv_quant"] = "q8_0"
        result["estimated_total_mib"] = round(total_q8, 1)
        result["fits"] = True
        recommendations.append(
            "Enable KV-cache quantization (--cache-type-k q8_0 --cache-type-v q8_0) "
            f"— saves ~{kv_mib_full - kv_q8:.0f} MiB."
        )
        if result["fits"]:
            return result

    # Step 2: KV quantization q4_0 (quarters KV)
    kv_q4 = kv_mib_full * 0.25
    total_q4 = base["total_mib"] - kv_mib_full + kv_q4
    if total_q4 <= free:
        result["kv_quant"] = "q4_0"
        result["estimated_total_mib"] = round(total_q4, 1)
        result["fits"] = True
        recommendations.append(
            "Enable aggressive KV-cache quantization (--cache-type-k q4_0 "
            f"--cache-type-v q4_0) — saves ~{kv_mib_full - kv_q4:.0f} MiB."
        )
        if result["fits"]:
            return result

    # Step 3: Clamp GPU layers to offload some weights to CPU RAM.
    n_layer = base.get("n_layer")
    file_mib = base.get("file_size_mib", 0)
    if n_layer and file_mib > 0:
        weight_mib = bd["weights_mib"]
        # Target: free - kv - overhead, expressed as a fraction of weights
        budget = max(0.0, free - (kv_mib_full * 0.25) - bd["overhead_mib"])
        frac = max(0.0, min(1.0, budget / max(weight_mib, 1.0)))
        ngl = max(0, int(n_layer * frac))
        result["n_gpu_layers"] = ngl
        est = weight_mib * frac + kv_mib_full * 0.25 + bd["overhead_mib"]
        result["estimated_total_mib"] = round(est, 1)
        result["fits"] = est <= free
        result["kv_quant"] = "q4_0"
        recommendations.append(
            f"Reduce GPU offload to ~{ngl}/{n_layer} layers (-ngl {ngl}) to keep "
            "weights within VRAM; the remainder will run on CPU RAM (slower)."
        )

    recommendations.append(
        "Flash attention (--flash-attn on) is strongly recommended to lower "
        "KV-cache footprint regardless of the above."
    )
    return result


def fit_warnings(model_id, model_cfg, free_mib, outgoing_mib=0.0):
    """Compare the estimated footprint with the VRAM that will be free once outgoing models stop.

    Returns {"warnings": [...], "needed_mib": float, "available_mib": float}; the estimate is
    approximate, so callers treat the result as advice unless strictVram is on.
    """
    est = estimate_vram_usage(model_cfg)
    needed = float(est["total_mib"])
    available = float(free_mib) + float(outgoing_mib)
    warnings = []
    if needed > available:
        warnings.append(
            f"Model '{model_id}' needs about {needed:.0f} MiB of VRAM but only about "
            f"{available:.0f} MiB will be free; it may fail to load or run out of memory."
        )
    return {"warnings": warnings, "needed_mib": round(needed, 1), "available_mib": round(available, 1)}


def _human_size(size_bytes):
    if size_bytes == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"
