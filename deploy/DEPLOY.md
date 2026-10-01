# Deploy: invoice-ai fully local on the A100 (Linux)

Everything runs on one box, localhost only. No data leaves the machine.

```
PDF -> PP-OCRv5 (GPU, per-job lang) -> {texts,boxes,scores}
    -> ocr_to_text -> Ollama (Qwen, json_object) -> Invoice JSON
FastAPI (:8000) serves the API + built SPA. Sanctions judge -> same Ollama.
```

## 0. Prereqs
- NVIDIA A100 40GB, recent driver + CUDA 12.x runtime (`nvidia-smi` works).
- Python 3.12, [uv](https://docs.astral.sh/uv/), Node 20+, git.
- App checked out at `/opt/invoice-ai` (adjust paths below if different).

## 1. Python deps (PaddleOCR on GPU)
```bash
cd /opt/invoice-ai
uv sync
# PaddlePaddle GPU wheel — A100 is compute capability 8.0 -> use cu126:
uv pip install paddlepaddle-gpu==3.2.1 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/
uv pip install -U paddleocr
# Smoke (downloads PP-OCRv5 weights on first run):
uv run python -c "from paddleocr import PaddleOCR; PaddleOCR(device='gpu:0', ocr_version='PP-OCRv5', lang='en'); print('paddle gpu ok')"
```
> First OCR for a new language pulls that recognizer. Persian uses `lang="fa"` (PaddleOCR's ISO token → PP-OCRv5 `arabic_PP-OCRv5_mobile_rec`, covers پ چ ژ گ + Persian digits); English uses `lang="en"`. The app selects this per job. Needs `paddleocr>=3.7.0`. **Pre-pull the Persian recognizer** so the first Persian job doesn't stall (and so an air-gapped box has it at all):
> ```bash
> uv run python -c "from paddleocr import PaddleOCR; PaddleOCR(device='gpu:0', ocr_version='PP-OCRv5', lang='fa'); print('paddle fa ok')"
> ```
> **Air-gapped:** run the two smoke commands once on a connected machine, then copy `~/.paddlex/official_models/` to the box (or set `PADDLE_PDX_CACHE_HOME` to a vendored dir). Do **not** pass `lang="arabic"` — it's a dead 2.x alias that loads no recognizer → empty OCR.

## 2. Ollama + model
```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo install -Dm644 deploy/ollama.override.conf /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama

ollama list                 # reuse an existing Qwen if present
ollama pull qwen3:32b       # otherwise (or if the existing one underperforms the bake-off)
ollama ps                   # confirm it stays resident (KEEP_ALIVE=-1)
```

## 3. Config
```bash
cp .env.example .env
# Set in .env:
#   LLM_BASE_URL=http://localhost:11434/v1
#   EXTRACT_MODEL=<tag from `ollama list`, e.g. qwen3:32b>
#   OPENROUTER_API_KEY=ollama          # dummy, non-empty
#   LLM_STRUCTURED_MODE=json_object
#   LLM_DISABLE_THINKING=1             # auto-on for qwen3 anyway
#   (SANCTIONS_MODEL defaults to EXTRACT_MODEL)
```

## 4. Build the frontend (served by FastAPI)
```bash
cd frontend && npm ci && npm run build && cd ..
# Produces frontend/dist/, which app/main.py mounts at / (SPA + assets).
```

## 5. Run as a service
```bash
# Edit deploy/invoice-ai.service: User/Group/WorkingDirectory/venv path.
sudo install -Dm644 deploy/invoice-ai.service /etc/systemd/system/invoice-ai.service
sudo systemctl daemon-reload && sudo systemctl enable --now invoice-ai
sudo systemctl status invoice-ai
```
App is on `http://<box>:8000` (API + UI on one port).

## 6. Firewall
Ollama has **no auth** — keep it on localhost (it is, via the override). Expose only the app port to the LAN you trust:
```bash
sudo ufw allow from <trusted-cidr> to any port 8000 proto tcp
sudo ufw deny 11434           # belt-and-suspenders; Ollama already binds localhost
```

## 7. Verify (acceptance gates)
1. **No external calls** — while running a job + a sanctions check, confirm zero traffic to `openrouter.ai`:
   `sudo tcpdump -n host openrouter.ai` (should stay silent).
2. **Persian OCR fixed** — upload a Persian doc, pick "فارسی / Persian", confirm OCR text is correct (not the old `lang=ch` garbage), boxes + scores present.
3. **Bbox UI** — in review, click fields → highlights land on the page.
4. **Extraction bake-off** — run a held-out English+Persian set through the local Qwen and (temporarily) cloud gpt-5-mini; compare amounts/dates(incl. Jalali)/company/line-items/SWIFT. If the local model is short, pull a larger/higher-quant Qwen or add Persian few-shot examples.
5. **VRAM** — `nvidia-smi` < 40GB during a job; `ollama ps` shows model resident.
6. **Reboot** — `sudo reboot`; after boot a job runs clean (systemd brings up ollama + app).

## Rollback
Edit `.env`: `LLM_BASE_URL=https://openrouter.ai/api/v1`, `EXTRACT_MODEL=openai/gpt-5-mini`, real `OPENROUTER_API_KEY`; `sudo systemctl restart invoice-ai`. OCR language is per-job, so no global revert needed.
