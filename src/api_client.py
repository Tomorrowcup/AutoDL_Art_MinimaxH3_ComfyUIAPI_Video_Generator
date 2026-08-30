# -*- coding: utf-8 -*-
"""AutoDL ComfyUI API 客户端（GUI 复用版）"""

import base64
import json
import mimetypes
import os
import random
import sys
import time

import requests

BASE_URL = "https://autodl.art"
API_PATH_SUBMIT = "/api/v1/comfyui/comfyui_workflow/{workflow_id}"
API_PATH_QUERY = "/api/v1/comfyui/comfyui_workflow/result/{task_id}"


def get_headers(api_key):
    return {"Authorization": api_key, "Content-Type": "application/json"}


def file_to_data_uri(path):
    """本地文件 -> data:{mime};base64,{b64}"""
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    mime, _ = mimetypes.guess_type(path)
    mime = mime or "application/octet-stream"
    return f"data:{mime};base64,{data}"


def _to_data_uri(value):
    return value if value.startswith(("http", "data:")) else file_to_data_uri(value)


def build_payload(workflow, prompt, duration, resolution, seed=None,
                  images=None, audios=None, first_frame=None, last_frame=None,
                  videos=None):
    """按工作流定义组装请求体；images/audios 为本地路径列表，自动转 base64"""
    payload = {}

    if workflow.get("frame_mode") == "first_last":
        if first_frame:
            payload["first_frame"] = _to_data_uri(first_frame)
        if last_frame:
            payload["last_frame"] = _to_data_uri(last_frame)
        payload["prompt"] = prompt
        payload["duration"] = duration
        payload["resolution"] = resolution
        if workflow.get("has_seed"):
            payload["seed"] = seed if seed is not None else random.randrange(1, 10**15)
        return payload

    dur_field = workflow.get("duration_field", "duration")
    payload[dur_field] = duration
    if workflow.get("has_prompt"):
        payload["prompt"] = prompt
    payload["resolution"] = resolution

    if workflow.get("has_seed"):
        payload["seed"] = seed if seed is not None else random.randrange(1, 10**15)

    ref_prefix = workflow.get("ref_prefix")
    if ref_prefix and images:
        for idx, img in enumerate(images):
            if not img:
                continue
            payload[f"{ref_prefix}{idx}"] = _to_data_uri(img)

    audio_prefix = workflow.get("audio_prefix")
    if audio_prefix and audios:
        for idx, aud in enumerate(audios):
            if not aud:
                continue
            payload[f"{audio_prefix}{idx}"] = _to_data_uri(aud)

    return payload


def submit_task(api_key, workflow_id, payload, base_url=BASE_URL, timeout=120):
    url = f"{base_url}{API_PATH_SUBMIT.format(workflow_id=workflow_id)}"
    resp = requests.post(url, json=payload, headers=get_headers(api_key), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Success":
        raise RuntimeError(json.dumps(data, ensure_ascii=False))
    return data["data"]["task_id"]


def query_task(api_key, task_id, base_url=BASE_URL, timeout=60):
    url = f"{base_url}{API_PATH_QUERY.format(task_id=task_id)}"
    resp = requests.get(url, headers=get_headers(api_key), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Success":
        raise RuntimeError(json.dumps(data, ensure_ascii=False))
    return data["data"]


def download_results(task_id, results, download_dir="outputs", label="video"):
    os.makedirs(download_dir, exist_ok=True)
    saved = []
    ts = time.strftime("%Y%m%d_%H%M%S") + f"_{int((time.time() * 1000) % 1000):03d}"
    for idx, item in enumerate(results):
        url = item.get("url") or item.get("file") or item.get("result")
        if not url:
            continue
        ext = url.split("?")[0].rsplit(".", 1)[-1].lower() if "." in url.split("?")[0] else "bin"
        if len(ext) > 5:
            ext = "bin"
        # 命名: 时间戳_工作流名_任务ID前8位_序号.ext
        filename = os.path.join(download_dir, f"{ts}_{label}_{task_id[:8]}_{idx}.{ext}")
        resp = requests.get(url, timeout=300)
        resp.raise_for_status()
        with open(filename, "wb") as f:
            f.write(resp.content)
        saved.append(filename)
    return saved
