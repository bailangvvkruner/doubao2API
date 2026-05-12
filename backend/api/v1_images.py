"""
doubao2api v1_images — OpenAI 兼容的 /v1/images/generations 接口

豆包特有逻辑：
  - 复用 chat 端点的文生图能力（通过 media_intent 检测）
  - 支持标准 OpenAI Images API 参数
  - 支持流式响应 (multipart/mixed)
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse

from backend.core.config import resolve_bot_id, settings, API_KEYS
from backend.services.doubao_client import DoubaoClient

log = logging.getLogger("doubao2api.images")
router = APIRouter()

SUPPORTED_SIZES = {
    "256x256": "256x256",
    "512x512": "512x512",
    "1024x1024": "1024x1024",
    "1024x1792": "1024x1792",
    "1792x1024": "1792x1024",
}

STYLE_MAP = {
    "vivid": "vivid",
    "natural": "natural",
}


def _check_auth(request: Request) -> str:
    """检查 API Key 鉴权，返回 token。"""
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""

    if not token:
        token = request.headers.get("x-api-key", "").strip()
    if not token:
        token = (
            request.query_params.get("key", "").strip()
            or request.query_params.get("api_key", "").strip()
        )

    admin_k = settings.ADMIN_KEY

    if API_KEYS:
        if token != admin_k and token not in API_KEYS and not token:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    return token


def _build_prompt_for_t2i(prompt: str, style: Optional[str] = None) -> str:
    """构建文生图提示词，可添加风格修饰。"""
    enhanced = prompt
    if style == "vivid":
        enhanced = f"请生成一张图片，要求：{prompt}，风格生动逼真"
    elif style == "natural":
        enhanced = f"请生成一张图片，要求：{prompt}，风格自然真实"
    return enhanced


def _make_chunk(completion_id: str, created: int, model: str, image_url: str = "", revised_prompt: str = "") -> str:
    """构建 SSE 格式的 image.generation.chunk。"""
    data = {
        "id": completion_id,
        "object": "image.generation.chunk",
        "created": created,
        "model": model,
    }
    if image_url or revised_prompt:
        data["data"] = {"url": image_url, "revised_prompt": revised_prompt}
    return json.dumps(data, ensure_ascii=False)


def _make_done_chunk(completion_id: str, created: int, model: str) -> str:
    """构建 SSE 完成块。"""
    return json.dumps(
        {
            "id": completion_id,
            "object": "image.generation.chunk",
            "created": created,
            "model": model,
            "done": True,
        },
        ensure_ascii=False,
    )


@router.post("/images/generations")
@router.post("/v1/images/generations")
async def images_generations(request: Request):
    """OpenAI 兼容的文生图接口。

    请求参数：
        prompt: 图片描述（必填）
        model: 模型名称（可选，默认使用配置的文生图模型）
        n: 生成数量（可选，默认1，最大10）
        size: 图片尺寸（可选，如 "1024x1024"）
        response_format: 返回格式 "url" 或 "b64_json"（可选，默认 "url"）
        style: 风格 "vivid" 或 "natural"（可选）
        user: 用户标识（可选）
    """
    app = request.app
    client: DoubaoClient = app.state.doubao_client

    token = _check_auth(request)

    users_db = app.state.users_db
    users = await users_db.get()
    user = next((u for u in users if u["id"] == token), None)
    if user and user.get("quota", 0) <= user.get("used_tokens", 0):
        raise HTTPException(status_code=402, detail="Quota Exceeded")

    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})

    prompt = req_data.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(400, {"error": {"message": "prompt is required", "type": "invalid_request_error"}})

    model_name = req_data.get("model", "doubao-pro")
    n = min(int(req_data.get("n", 1)), 10)
    if n < 1:
        n = 1

    size = req_data.get("size", "1024x1024")
    if size not in SUPPORTED_SIZES:
        size = "1024x1024"

    response_format = req_data.get("response_format", "url")
    if response_format not in ("url", "b64_json"):
        response_format = "url"

    style = req_data.get("style")
    if style and style not in STYLE_MAP:
        style = None

    stream = req_data.get("stream", False)

    bot_id = resolve_bot_id(model_name)
    completion_id = f"imggen-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    log.info(
        f"[Images] model={model_name}→bot_id={bot_id}, n={n}, size={size}, "
        f"response_format={response_format}, stream={stream}, prompt_len={len(prompt)}"
    )

    if stream:
        return await _handle_stream(
            client=client,
            prompt=prompt,
            bot_id=bot_id,
            n=n,
            size=size,
            response_format=response_format,
            style=style,
            model_name=model_name,
            completion_id=completion_id,
            created=created,
        )
    else:
        return await _handle_sync(
            client=client,
            prompt=prompt,
            bot_id=bot_id,
            n=n,
            size=size,
            response_format=response_format,
            style=style,
            model_name=model_name,
            completion_id=completion_id,
            created=created,
            users_db=users_db,
            token=token,
            user_text=prompt,
        )


async def _handle_stream(
    client: DoubaoClient,
    prompt: str,
    bot_id: str,
    n: int,
    size: str,
    response_format: str,
    style: Optional[str],
    model_name: str,
    completion_id: str,
    created: int,
):
    """处理流式响应。"""

    async def generate():
        t2i_prompt = _build_prompt_for_t2i(prompt, style)
        img_count = 0
        errors = []

        try:
            async for event in client.stream_with_retry(text=t2i_prompt, bot_id=bot_id):
                if event["type"] == "image":
                    url = event.get("url", "")
                    if url:
                        img_count += 1
                        revised = f"{prompt}" if style else prompt
                        chunk_data = {"url": url, "revised_prompt": revised}
                        yield f"data: {json.dumps({'id': completion_id, 'object': 'image.generation.chunk', 'created': created, 'model': model_name, 'data': chunk_data}, ensure_ascii=False)}\n\n"
                        if img_count >= n:
                            break

                elif event["type"] == "delta":
                    pass

                elif event["type"] == "error":
                    errors.append(event.get("message", "Unknown error"))

                elif event["type"] == "done":
                    break

            if not img_count and errors:
                yield f"data: {json.dumps({'error': {'message': errors[0], 'type': 'upstream_error'}}, ensure_ascii=False)}\n\n"
            elif not img_count:
                yield f"data: {json.dumps({'error': {'message': 'No images generated', 'type': 'upstream_error'}}, ensure_ascii=False)}\n\n"

        except Exception as e:
            log.error(f"[Images-Stream] exception: {e}")
            yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'internal_error'}}, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _handle_sync(
    client: DoubaoClient,
    prompt: str,
    bot_id: str,
    n: int,
    size: str,
    response_format: str,
    style: Optional[str],
    model_name: str,
    completion_id: str,
    created: int,
    users_db,
    token: str,
    user_text: str,
):
    """处理同步响应。"""
    all_images = []

    for i in range(n):
        try:
            t2i_prompt = _build_prompt_for_t2i(prompt, style)
            result, acc, session_id = await client.chat_with_retry(text=t2i_prompt, bot_id=bot_id)

            if result.error:
                log.warning(f"[Images] generation {i+1}/{n} failed: {result.error}")
                continue

            image_urls = result.image_urls
            if image_urls:
                for url in image_urls:
                    all_images.append({
                        "url": url,
                        "revised_prompt": prompt,
                    })
                    if len(all_images) >= n:
                        break

        except Exception as e:
            log.error(f"[Images] generation {i+1}/{n} exception: {e}")
            continue

        if len(all_images) >= n:
            break

    if not all_images:
        raise HTTPException(status_code=500, detail="Failed to generate images")

    data_response = []
    for img in all_images[:n]:
        if response_format == "b64_json":
            data_response.append({"b64_json": "", "revised_prompt": img["revised_prompt"]})
        else:
            data_response.append({"url": img["url"], "revised_prompt": img["revised_prompt"]})

    total_tokens = len(prompt) * n
    users = await users_db.get()
    for u in users:
        if u["id"] == token:
            u["used_tokens"] = u.get("used_tokens", 0) + total_tokens
            break
    await users_db.save(users)

    return JSONResponse({
        "created": created,
        "data": data_response,
    })
