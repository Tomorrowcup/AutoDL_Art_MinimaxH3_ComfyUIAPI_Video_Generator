import argparse
import base64
import json
import mimetypes
import os
import random
import sys
import time
from datetime import datetime

import requests

try:
    import msvcrt  # Windows 跨进程文件锁
    _LOCK_MOD = "msvcrt"
except ImportError:
    try:
        import fcntl  # Unix 跨进程文件锁
        _LOCK_MOD = "fcntl"
    except ImportError:
        _LOCK_MOD = None

REGISTRY_FILE = "tasks.json"
FINAL_STATUSES = {"SUCCESS", "FAILED"}


class registry_lock:
    """tasks.json 跨进程读-改-写锁。
    Windows 用 msvcrt.locking，Unix 用 fcntl.flock；均不可用时降级为无锁
    （仍有临时文件原子写兜底，只是并发下可能互相覆盖状态）。"""

    def __init__(self):
        self.f = None

    def __enter__(self):
        try:
            self.f = open(REGISTRY_FILE + ".lock", "a+")
            if _LOCK_MOD == "msvcrt":
                msvcrt.locking(self.f.fileno(), msvcrt.LK_LOCK, 1)
            elif _LOCK_MOD == "fcntl":
                fcntl.flock(self.f, fcntl.LOCK_EX)
        except Exception:
            if self.f:
                try:
                    self.f.close()
                except Exception:
                    pass
            self.f = None
        return self

    def __exit__(self, *exc):
        if self.f:
            try:
                if _LOCK_MOD == "msvcrt":
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
                elif _LOCK_MOD == "fcntl":
                    fcntl.flock(self.f, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self.f.close()
            except Exception:
                pass
            self.f = None
        return False


def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("错误: 未找到 config.json（请先在项目目录创建，含 api_key 字段）")
        sys.exit(1)
    except Exception as e:
        print(f"错误: 读取 config.json 失败: {e}")
        sys.exit(1)


# 本地素材字段：GUI 存档 payload 实际使用的键（ref_image_N / ref_audio_N / first_frame / last_frame）
LOCAL_ASSET_PREFIXES = ("ref_image_", "ref_audio_")
LOCAL_ASSET_KEYS = ("first_frame", "last_frame")


def load_payload(payload_file):
    with open(payload_file, "r", encoding="utf-8") as f:
        payload = json.load(f)
    base_dir = os.path.dirname(os.path.abspath(payload_file))
    for key, value in list(payload.items()):
        is_asset = (key.startswith(LOCAL_ASSET_PREFIXES) or key in LOCAL_ASSET_KEYS)
        if is_asset and isinstance(value, str) and not value.startswith("http") and not value.startswith("data:"):
            path = value if os.path.isabs(value) else os.path.join(base_dir, value)
            if os.path.exists(path):
                payload[key] = file_to_data_uri(path)
                print(f"  [本地文件] {key} -> base64 ({os.path.basename(path)})")
    return payload


def load_registry():
    if os.path.exists(REGISTRY_FILE):
        with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_registry(registry):
    """原子写：写临时文件后替换，防止 watch 与 submit 进程并发写坏 tasks.json"""
    tmp = REGISTRY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REGISTRY_FILE)


def get_headers(config):
    return {"Authorization": config["api_key"], "Content-Type": "application/json"}


def file_to_data_uri(path):
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    mime, _ = mimetypes.guess_type(path)
    mime = mime or "application/octet-stream"
    return f"data:{mime};base64,{data}"


def apply_seed(payload, args):
    seed_min, seed_max = 1, 999999999999999
    if getattr(args, "random_seed", False):
        seed = random.randrange(seed_min, seed_max + 1)
        payload["seed"] = seed
        print(f"  [seed] 随机生成 seed={seed}")
    elif getattr(args, "seed", None) is not None:
        if not (seed_min <= args.seed <= seed_max):
            print(f"错误: seed 需在 {seed_min}-{seed_max} 之间（收到 {args.seed}）")
            sys.exit(1)
        payload["seed"] = args.seed
        print(f"  [seed] 使用指定 seed={args.seed}")
    elif "seed" in payload:
        s = payload["seed"]
        if not (seed_min <= s <= seed_max):
            print(f"错误: payload 中的 seed={s} 超出范围 {seed_min}-{seed_max}")
            sys.exit(1)
        print(f"  [seed] 使用 payload 中的 seed={s}")
    return payload


def submit_task(config, payload_file, args):
    payload = apply_seed(load_payload(payload_file), args)
    url = f"{config['base_url']}/api/v1/comfyui/comfyui_workflow/{config['workflow_id']}"
    resp = requests.post(url, json=payload, headers=get_headers(config), timeout=120)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Success":
        print(json.dumps(data, ensure_ascii=False, indent=2))
        sys.exit(1)
    return data["data"]["task_id"], payload_file, payload.get("seed")


def query_task(config, task_id):
    url = f"{config['base_url']}/api/v1/comfyui/comfyui_workflow/result/{task_id}"
    resp = requests.get(url, headers=get_headers(config), timeout=60)
    resp.raise_for_status()
    return resp.json()["data"]


def download_results(config, task_id, results):
    os.makedirs(config["download_dir"], exist_ok=True)
    saved = []
    for idx, item in enumerate(results):
        url = item.get("url") or item.get("file") or item.get("result")
        if not url:
            continue
        ext = url.split("?")[0].rsplit(".", 1)[-1].lower() if "." in url.split("?")[0] else "bin"
        if len(ext) > 5:
            ext = "bin"
        filename = os.path.join(config["download_dir"], f"task_{task_id[:8]}_{idx}.{ext}")
        print(f"  [下载] {url}")
        resp = requests.get(url, timeout=300)
        resp.raise_for_status()
        with open(filename, "wb") as f:
            f.write(resp.content)
        saved.append(filename)
    return saved


def cmd_submit(args):
    config = load_config()
    task_id, payload_file, seed = submit_task(config, args.payload, args)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rec = {
        "payload_file": payload_file,
        "seed": seed,
        "created_at": now,
        "status": "QUEUED",
        "duration": 0,
        "results": [],
        "files": [],
    }
    # 锁内 load-latest + 合并写：不覆盖其他进程（GUI/watch）刚写入的记录
    with registry_lock():
        latest = load_registry()
        latest[task_id] = rec
        save_registry(latest)
    print(f"提交成功 task_id={task_id} payload={payload_file}" + (f" seed={seed}" if seed is not None else ""))
    print(f"运行 `python manager.py watch` 后台统一监测所有任务")


def poll_all(config, registry, verbose):
    """轮询一轮所有未终态任务。返回本轮有变更的任务 id 集合（供合并写）。"""
    changed_tids = set()
    for task_id, rec in list(registry.items()):
        # 跳过 GUI 写入的计划任务占位记录（不是可查询的 API 任务）
        if task_id.startswith("PLAN_"):
            continue
        if rec.get("status") in FINAL_STATUSES:
            continue
        try:
            data = query_task(config, task_id)
        except Exception as e:
            print(f"  [任务 {task_id[:8]}] 查询失败: {e}")
            continue
        status = data["status"]
        duration = data.get("duration", 0)
        if verbose or status != rec.get("status"):
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] 任务 {task_id[:8]} 状态: {status} 耗时: {duration}s")
        rec["status"] = status
        rec["duration"] = duration
        rec["results"] = data.get("results", [])
        if status == "SUCCESS":
            print(f"  [任务 {task_id[:8]}] 成功，开始下载...")
            files = download_results(config, task_id, rec["results"])
            rec["files"] = files
            print(f"  [任务 {task_id[:8]}] 完成! 保存: {files}")
        elif status == "FAILED":
            print(f"  [任务 {task_id[:8]}] 失败: {json.dumps(data, ensure_ascii=False)}")
        changed_tids.add(task_id)
    return changed_tids


def cmd_watch(args):
    config = load_config()
    while True:
        registry = load_registry()
        # 跳过 GUI 写入的 PLAN_ 占位记录（不可查询）
        pending = [t for t, r in registry.items()
                   if not t.startswith("PLAN_") and r.get("status") not in FINAL_STATUSES]
        if not pending:
            if args.daemon:
                if not args.quiet:
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] 无进行中任务，继续等待新提交 (Ctrl+C 退出)")
                time.sleep(config.get("poll_interval", 5))
                continue
            else:
                print("所有任务已完成。")
                return
        if not args.quiet:
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] 监测中: {len(pending)} 个进行中任务")
        changed_tids = poll_all(config, registry, args.quiet)
        # 锁内合并写：只覆盖本轮查过的任务，其他进程新写入的记录原样保留
        with registry_lock():
            latest = load_registry()
            for tid in changed_tids:
                latest[tid] = registry[tid]
            save_registry(latest)
        time.sleep(config.get("poll_interval", 5))


def cmd_status(args):
    registry = load_registry()
    # 过滤 GUI 写入的 PLAN_ 占位记录（结构不同，且非 API 任务）
    tasks = {t: r for t, r in registry.items() if not t.startswith("PLAN_")}
    if not tasks:
        print("暂无任务。")
        return
    print(f"{'task_id':<40} {'status':<10} {'耗时s':<8} {'seed':<18} {'payload'}")
    print("-" * 110)
    for task_id, rec in sorted(tasks.items(), key=lambda x: x[1].get("created_at", "")):
        seed = str(rec.get("seed") or "-")
        print(f"{task_id:<40} {rec.get('status', '?'):<10} {rec.get('duration', 0):<8} {seed:<18} {rec.get('payload_file', '-')}")


def cmd_download(args):
    config = load_config()
    registry = load_registry()
    if args.task_id not in registry:
        print(f"任务 {args.task_id} 不在任务列表中。")
        return
    rec = registry[args.task_id]
    if rec.get("status") != "SUCCESS":
        print(f"任务 {args.task_id} 状态为 {rec.get('status')}，无法下载。")
        return
    files = download_results(config, args.task_id, rec["results"])
    rec["files"] = files
    # 锁内合并写：不覆盖其他进程刚写入的记录
    with registry_lock():
        latest = load_registry()
        latest[args.task_id] = rec
        save_registry(latest)
    print(f"保存: {files}")


def main():
    parser = argparse.ArgumentParser(description="AutoDL ComfyUI 统一任务管理器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_submit = sub.add_parser("submit", help="提交任务（立即返回，不阻塞）")
    p_submit.add_argument("payload", nargs="?", default="payload.json", help="payload JSON 文件路径")
    p_submit.add_argument("--seed", type=int, default=None, help="指定 seed（覆盖 payload 中的 seed）")
    p_submit.add_argument("--random-seed", action="store_true", help="每次随机生成 seed（相同 payload 得到不同结果）")
    p_submit.set_defaults(func=cmd_submit)

    p_watch = sub.add_parser("watch", help="统一监测所有任务并自动下载")
    p_watch.add_argument("--daemon", action="store_true", help="无任务时也持续运行，等待新提交")
    p_watch.add_argument("--quiet", action="store_true", help="仅打印状态变化")
    p_watch.set_defaults(func=cmd_watch)

    p_status = sub.add_parser("status", help="查看所有任务状态")
    p_status.set_defaults(func=cmd_status)

    p_dl = sub.add_parser("download", help="重新下载某任务的结果")
    p_dl.add_argument("task_id")
    p_dl.set_defaults(func=cmd_download)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()