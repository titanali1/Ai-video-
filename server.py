#!/usr/bin/env python3
"""Deisa's same-origin API and optional AI provider gateway.

Provider credentials stay on the server. The Android/WebView client only sends
prompts and an optional selected reference image to this service.
"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import secrets
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_BODY = 24 * 1024 * 1024


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip("\"'")
        if name and name not in os.environ:
            os.environ[name] = value


load_dotenv(ROOT / ".env")


def workflow_is_ready(kind: str) -> bool:
    prefix = "COMFY_IMAGE" if kind == "image" else "COMFY_VIDEO"
    configured = os.getenv("COMFY_IMAGE_WORKFLOW" if kind == "image" else "COMFY_VIDEO_WORKFLOW")
    prompt_node = os.getenv(prefix + "_PROMPT_NODE")
    if not (os.getenv("COMFY_URL") and configured and prompt_node):
        return False
    path = (ROOT / configured).resolve()
    if ROOT not in path.parents or not path.is_file():
        return False
    try:
        workflow = json.loads(path.read_text(encoding="utf-8"))
        return prompt_node in workflow
    except Exception:
        return False


def public_provider_status() -> dict:
    return {
        "huggingface": bool(os.getenv("HF_TOKEN")),
        "comfyui": workflow_is_ready("image"),
        "comfyuiVideo": workflow_is_ready("video"),
        "freeUnlimited": "self-hosted-only",
    }


def response_error(handler, status: int, message: str, code: str = "PROVIDER_ERROR") -> None:
    data = json.dumps({"error": message, "code": code}, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def hf_generate(kind: str, prompt: str, model: str, options: dict) -> tuple[bytes, str, str]:
    token = os.getenv("HF_TOKEN")
    if not token:
        raise RuntimeError("برای Hugging Face، توکن HF_TOKEN را فقط روی سرور تنظیم کنید.")
    try:
        from huggingface_hub import InferenceClient
    except ImportError as exc:
        raise RuntimeError("وابستگی Hugging Face نصب نیست؛ دستور pip install -r requirements.txt را اجرا کنید.") from exc

    if kind == "video":
        raise RuntimeError("برای جلوگیری از هزینه‌ی ناخواسته، ویدیوی ابری Hugging Face فعال نیست؛ برای تولید بدون سهمیه‌ی API از ComfyUI خودمیزبان استفاده کنید.")
    provider = os.getenv("HF_PROVIDER", "hf-inference")
    timeout = int(os.getenv("HF_TIMEOUT_SECONDS", "900"))
    client = InferenceClient(provider=provider, api_key=token, timeout=timeout)
    model = model or (os.getenv("HF_IMAGE_MODEL") if kind == "image" else os.getenv("HF_VIDEO_MODEL"))
    if not model:
        raise RuntimeError(f"مدل Hugging Face برای {kind} پیکربندی نشده است.")
    try:
        if kind == "image":
            output = client.text_to_image(
                prompt,
                model=model,
                width=int(options.get("width", 1024)),
                height=int(options.get("height", 1024)),
                num_inference_steps=int(options.get("steps", 4)),
                seed=int(options.get("seed", secrets.randbelow(2**31))),
            )
            if isinstance(output, bytes):
                return output, "image/png", "deisa-image.png"
            buffer = io.BytesIO()
            output.save(buffer, format="PNG")
            return buffer.getvalue(), "image/png", "deisa-image.png"
        if kind == "video":
            output = client.text_to_video(
                prompt,
                model=model,
                num_frames=int(options.get("frames", 97)),
                num_inference_steps=int(options.get("steps", 30)),
                seed=int(options.get("seed", secrets.randbelow(2**31))),
            )
            if not isinstance(output, bytes):
                output = bytes(output)
            return output, "video/mp4", "deisa-video.mp4"
    except Exception as exc:
        # Do not include request headers or environment values in client errors.
        raise RuntimeError(f"ارائه‌دهنده‌ی Hugging Face خطا داد: {str(exc)[:420]}") from exc
    raise RuntimeError("نوع خروجی پشتیبانی نمی‌شود.")


def comfy_request(url: str, payload: bytes | None = None, content_type: str = "application/json", timeout: int = 60) -> bytes:
    headers = {"Content-Type": content_type}
    api_key = os.getenv("COMFY_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=payload, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def comfy_upload_image(image_data: str, filename: str) -> str:
    if "," in image_data and image_data.startswith("data:"):
        image_data = image_data.split(",", 1)[1]
    raw = base64.b64decode(image_data, validate=True)
    if len(raw) > 15 * 1024 * 1024:
        raise RuntimeError("حجم تصویر مرجع باید کمتر از ۱۵ مگابایت باشد.")
    base = os.getenv("COMFY_URL", "").rstrip("/")
    boundary = "----Deisa" + secrets.token_hex(16)
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename or "reference.png").name) or "reference.png"
    mime = mimetypes.guess_type(safe_name)[0] or "image/png"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{safe_name}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + raw + f"\r\n--{boundary}--\r\n".encode()
    result = json.loads(comfy_request(base + "/upload/image", body, f"multipart/form-data; boundary={boundary}"))
    if not result.get("name"):
        raise RuntimeError("ComfyUI تصویر مرجع را نپذیرفت.")
    return result["name"]


def comfy_generate(kind: str, prompt: str, options: dict) -> tuple[bytes, str, str]:
    base = os.getenv("COMFY_URL", "").rstrip("/")
    workflow_path = os.getenv("COMFY_IMAGE_WORKFLOW" if kind == "image" else "COMFY_VIDEO_WORKFLOW")
    if not base or not workflow_path:
        raise RuntimeError("ComfyUI یا workflow این خروجی روی سرور تنظیم نشده است.")
    workflow_file = (ROOT / workflow_path).resolve()
    if ROOT not in workflow_file.parents and workflow_file != ROOT:
        raise RuntimeError("مسیر workflow نامعتبر است.")
    try:
        workflow = json.loads(workflow_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"خواندن workflow ناموفق بود: {exc}") from exc

    prefix = "COMFY_IMAGE" if kind == "image" else "COMFY_VIDEO"
    prompt_node = os.getenv(prefix + "_PROMPT_NODE")
    if not prompt_node or prompt_node not in workflow:
        raise RuntimeError(f"شناسه‌ی گره‌ی prompt برای workflow {kind} تنظیم نشده است.")
    prompt_input = os.getenv(prefix + "_PROMPT_INPUT", "text")
    workflow[prompt_node].setdefault("inputs", {})[prompt_input] = prompt

    seed_node = os.getenv(prefix + "_SEED_NODE")
    if seed_node and seed_node in workflow:
        seed_input = os.getenv(prefix + "_SEED_INPUT", "seed")
        workflow[seed_node].setdefault("inputs", {})[seed_input] = int(options.get("seed", secrets.randbelow(2**31)))

    image_data = options.get("referenceImage")
    image_node = os.getenv("COMFY_IMAGE_NODE" if kind == "image" else "COMFY_VIDEO_IMAGE_NODE")
    if image_data and image_node:
        uploaded = comfy_upload_image(image_data, str(options.get("referenceName", "reference.png")))
        image_input = os.getenv("COMFY_IMAGE_INPUT" if kind == "image" else "COMFY_VIDEO_IMAGE_INPUT", "image")
        if image_node not in workflow:
            raise RuntimeError("گره‌ی تصویر مرجع در workflow پیدا نشد.")
        workflow[image_node].setdefault("inputs", {})[image_input] = uploaded

    queued = json.loads(comfy_request(base + "/prompt", json.dumps({"prompt": workflow}).encode()))
    prompt_id = queued.get("prompt_id")
    if not prompt_id:
        raise RuntimeError("ComfyUI نتوانست workflow را به صف اضافه کند.")

    deadline = time.monotonic() + int(os.getenv("COMFY_TIMEOUT_SECONDS", "1800"))
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            history = json.loads(comfy_request(base + "/history/" + urllib.parse.quote(prompt_id), timeout=30))
        except Exception:
            continue
        entry = history.get(prompt_id)
        if not entry:
            continue
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            raise RuntimeError("ComfyUI workflow با خطا متوقف شد؛ گره‌ها و مدل‌ها را در ComfyUI بررسی کنید.")
        outputs = entry.get("outputs", {})
        preferred = os.getenv(prefix + "_OUTPUT_NODE")
        node_outputs = [outputs[preferred]] if preferred in outputs else list(outputs.values())
        for node_output in node_outputs:
            for group in ("images", "videos", "gifs"):
                files = node_output.get(group) or []
                if not files:
                    continue
                item = files[0]
                query = urllib.parse.urlencode({
                    "filename": item["filename"],
                    "subfolder": item.get("subfolder", ""),
                    "type": item.get("type", "output"),
                })
                data = comfy_request(base + "/view?" + query, timeout=180)
                filename = Path(item["filename"]).name
                content_type = mimetypes.guess_type(filename)[0] or ("video/mp4" if group != "images" else "image/png")
                return data, content_type, filename
    raise RuntimeError("مهلت انتظار ComfyUI تمام شد؛ ممکن است GPU یا صف رندر مشغول باشد.")


class DeisaHandler(SimpleHTTPRequestHandler):
    server_version = "DeisaGateway/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        origin = os.getenv("DEISA_CORS_ORIGIN", "*")
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Expose-Headers", "X-Deisa-Filename, X-Deisa-Provider")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if urllib.parse.urlsplit(self.path).path == "/api/providers":
            data = json.dumps({"providers": public_provider_status()}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/api/generate":
            response_error(self, 404, "مسیر API پیدا نشد.", "NOT_FOUND")
            return
        required_key = os.getenv("DEISA_API_KEY")
        provided = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if required_key and not secrets.compare_digest(required_key, provided):
            response_error(self, 401, "کلید دسترسی سرور نادرست است.", "UNAUTHORIZED")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                response_error(self, 413, "درخواست خالی است یا از حد مجاز بزرگ‌تر است.", "BODY_TOO_LARGE")
                return
            request = json.loads(self.rfile.read(length))
            kind = request.get("kind")
            provider = request.get("provider")
            prompt = str(request.get("prompt", "")).strip()
            if kind not in ("image", "video") or not prompt:
                response_error(self, 400, "نوع خروجی یا متن توضیح معتبر نیست.", "INVALID_REQUEST")
                return
            if len(prompt) > 12000:
                response_error(self, 413, "توضیح بیش از حد طولانی است.", "PROMPT_TOO_LONG")
                return
            if provider == "huggingface":
                content, mime, filename = hf_generate(kind, prompt, str(request.get("model", "")), request.get("options", {}))
            elif provider == "comfyui":
                content, mime, filename = comfy_generate(kind, prompt, request.get("options", {}))
            else:
                response_error(self, 400, "ارائه‌دهنده‌ی انتخاب‌شده پشتیبانی نمی‌شود.", "UNKNOWN_PROVIDER")
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            safe_filename = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name) or "deisa-output"
            self.send_header("Content-Disposition", f'inline; filename="{safe_filename}"')
            self.send_header("X-Deisa-Filename", safe_filename)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Deisa-Provider", provider)
            self.end_headers()
            self.wfile.write(content)
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", "replace")
            response_error(self, 502, f"ارائه‌دهنده پاسخ {exc.code} داد: {detail[:300]}", "UPSTREAM_ERROR")
        except Exception as exc:
            response_error(self, 503, str(exc)[:600], "PROVIDER_UNAVAILABLE")


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    if not os.getenv("DEISA_API_KEY") and os.getenv("ENVIRONMENT") == "production":
        raise SystemExit("برای حالت production، DEISA_API_KEY را تنظیم کنید.")
    print(f"Deisa AI gateway running at http://{host}:{port}")
    print("Configure .env.example settings before requesting real generations.")
    ThreadingHTTPServer((host, port), DeisaHandler).serve_forever()


if __name__ == "__main__":
    main()
