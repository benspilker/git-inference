Graphics cards that can comfortably run a quantized ~11 B‑parameter LLM (e.g., Llama 3.2) in 8‑bit or 4‑bit precision

Precision	Approx. VRAM needed*	Recommended GPUs (VRAM)	Why it works
8‑bit (INT8)	~22 GB	• NVIDIA RTX 4090 – 24 GB GDDR6X
• NVIDIA RTX 4090 Ti (if available) – 24 GB
• NVIDIA RTX A6000 / RTX 6000 Ada – 48 GB
• AMD Instinct MI250X – 128 GB HBM2	8‑bit quantization cuts the FP16 memory footprint by ~4×, so a 22 GB model fits in 24 GB‑class cards with headroom for the model’s KV cache and batch size.
4‑bit (INT4)	~11 GB	• NVIDIA RTX 4090 – 24 GB (plenty of margin)
• NVIDIA RTX 4080 – 16 GB (tight but workable)
• NVIDIA RTX A5000 – 24 GB
• AMD Radeon RX 7900 XTX – 24 GB GDDR6
• AMD Instinct MI250X – 128 GB	4‑bit quantization reduces memory ~8×, so an 11 GB model easily fits on most modern 16‑GB‑plus GPUs.
Mixed‑precision (FP8/INT8 hybrid)	~14 GB	• NVIDIA RTX 4090 – 24 GB
• NVIDIA RTX 4090 Ti – 24 GB
• NVIDIA RTX A6000 – 48 GB	Hybrid schemes keep the KV cache in higher precision while quantizing the weights, landing in the 14‑GB range.

*These numbers are rough estimates based on the typical size of an 11 B‑parameter model after quantization (weights ≈ 4 bytes × 11 B ÷ quantization factor) plus additional memory for the KV cache, optimizer state (if fine‑tuning), and the runtime framework.

Practical tips for running Llama 3.2‑style models

Choose a quantization scheme

INT8 (e.g., bitsandbytes load_in_8bit=True) → works on 24 GB GPUs.
INT4 (e.g., bitsandbytes load_in_4bit=True) → can run on 16 GB GPUs, though you may need to reduce batch size or sequence length.

Framework support

PyTorch + bitsandbytes for 8‑bit/4‑bit loading.
vLLM also supports INT8/INT4 and can efficiently manage the KV cache.

Batch size & context length

KV cache scales with batch_size × context_len × hidden_dim.
On a 24 GB card, a batch of 1–2 with a 4 k context is safe; larger batches may require a 48 GB card.

CPU‑offload fallback

If you only have a 12‑16 GB GPU, you can offload part of the KV cache to RAM (e.g., using accelerate’s cpu_offload), but latency will increase.

Power & cooling

High‑end GPUs (RTX 4090/6000) draw 350 W+; ensure adequate PSU and airflow.
Example hardware shortlist
GPU	VRAM	Approx. price (USD, 2024)	Good for
RTX 4090	24 GB GDDR6X	1,600–1,800	8‑bit & 4‑bit LLMs, single‑GPU inference
RTX 4080	16 GB GDDR6X	1,200–1,300	4‑bit inference, modest batch size
RTX A6000 / RTX 6000 Ada	48 GB GDDR6	4,500–5,000	8‑bit with large KV cache, multi‑user serving
AMD Instinct MI250X	128 GB HBM2	7,000–8,000	Enterprise‑grade, multi‑model hosting
RX 7900 XTX	24 GB GDDR6	1,000–1,200	4‑bit inference, gaming‑oriented builds

Next step: Do you need guidance on how to set up the quantization pipeline (e.g., using bitsandbytes or vLLM) on one of these GPUs, or are you looking for a specific purchase recommendation based on a budget?
