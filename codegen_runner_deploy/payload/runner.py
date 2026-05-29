"""
树莓派端 Runner — 轮询云端拉任务、执行、上报结果
"""
import glob
import json
import logging
import os
import shutil
import signal
import subprocess
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# ===== 配置 =====
SERVER_URL = os.getenv("CODEGEN_SERVER_URL", "https://your-server.com")
DEVICE_ID = os.getenv("DEVICE_ID", "dev_001")
DEVICE_SECRET = os.getenv("DEVICE_SECRET", "xxx")
HARDWARE_TOKEN = os.getenv("HARDWARE_TOKEN", "xxx")

APP_DIR = "/opt/device-app/current"
OUTPUT_DIR = "/opt/device-app/output"
BACKUP_DIR = "/opt/device-app/backup"
SDK_DIR = "/opt/device-app/sdk"
PGID_FILE = "/opt/device-app/runner.pgid"  # 给 runner 自己重启时清理上一轮孤儿进程组用
POLL_INTERVAL = 3
VALIDATION_WAIT = 15

# ===== 全局状态 =====
current_process = None
current_pgid = None


def poll_server():
    """轮询云端拉取任务"""
    try:
        resp = requests.post(
            f"{SERVER_URL}/api/codegen/device/poll",
            json={"device_id": DEVICE_ID, "device_secret": DEVICE_SECRET},
            headers={"Authorization": HARDWARE_TOKEN},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json().get("data")
            if data and data.get("task_id"):
                return data
        return None
    except Exception as e:
        log.error("轮询失败: %s", e)
        return None


def stop_current_app():
    """停止当前运行的应用进程及其所有孙子进程。

    SDK 的 face/speaker 会派生 paplay/aplay/eye_matrix_8x8 子进程，仅给 main.py
    发信号会让它们被孤儿化继续跑硬件，造成"新任务起来旧表情/音频还在"。
    main.py 由 start_app 用 start_new_session=True 拉到独立 process group，
    这里用 killpg 一次性把整组（含所有孙子）干掉。
    """
    global current_process, current_pgid
    pgid = current_pgid
    proc = current_process

    def _killpg_safe(sig):
        if pgid is None:
            return
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass  # 组里没成员了
        except PermissionError:
            # 组里全是 root 进程（sudo'd eye_matrix），face user 投不进去
            # 已知局限：依赖此前 SIGTERM 阶段 sudo 把信号转发给 root 子进程
            log.warning("killpg(%s, %s) EPERM — pgid 里可能有 root 进程", pgid, sig)

    if proc is not None and proc.poll() is None:
        log.info("停止旧进程 PID=%s PGID=%s ...", proc.pid, pgid)
        if pgid is not None:
            _killpg_safe(signal.SIGTERM)
        else:
            try:
                proc.send_signal(signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("SIGTERM 超时，对进程组 %s 发 SIGKILL", pgid)
            if pgid is not None:
                _killpg_safe(signal.SIGKILL)
            else:
                try: proc.kill()
                except Exception: pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
    # 兜底：即使 leader 已退出，孙子可能还在 — 再 killpg 一次确保整组清干净
    _killpg_safe(signal.SIGKILL)
    if os.path.exists(PGID_FILE):
        try: os.remove(PGID_FILE)
        except Exception: pass
    current_process = None
    current_pgid = None


def kill_stale_pgid():
    """runner 自身重启时调用 — 清理上一轮 runner 进程崩掉后留下的孤儿应用组。

    安全校验：只在 /proc/<pgid>/cmdline 还能读到、并且包含我们的 main.py 路径时
    才 killpg；防止 Pi 重启后 pgid 被复用、误杀别的服务的会话首进程。
    "leader 已死但孙子还活" 的罕见情况这里清不到 — 留给上层运维清。
    """
    if not os.path.exists(PGID_FILE):
        return
    try:
        with open(PGID_FILE) as f:
            pgid = int(f.read().strip())
    except Exception:
        try: os.remove(PGID_FILE)
        except Exception: pass
        return

    main_py_path = os.path.join(APP_DIR, "main.py")
    cmdline_path = f"/proc/{pgid}/cmdline"
    try:
        with open(cmdline_path, "rb") as f:
            cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except FileNotFoundError:
        log.info("stale pgid=%s 的 leader 已不存在，跳过 killpg", pgid)
        cmdline = None
    except Exception as e:
        log.warning("读 /proc/%s/cmdline 失败: %s — 跳过 killpg", pgid, e)
        cmdline = None

    if cmdline is not None and main_py_path in cmdline:
        try:
            os.killpg(pgid, signal.SIGKILL)
            log.info("清理上次 runner 留下的孤儿进程组 pgid=%s", pgid)
        except ProcessLookupError:
            pass
        except PermissionError:
            log.warning("kill 孤儿 pgid=%s EPERM（root 进程组）", pgid)
        except Exception as e:
            log.warning("kill 孤儿 pgid=%s 失败: %s", pgid, e)
    elif cmdline is not None:
        log.info("pgid=%s 的 leader cmdline 不像 main.py（%r），不杀", pgid, cmdline[:100])

    try: os.remove(PGID_FILE)
    except Exception: pass


def backup_current():
    """备份当前代码"""
    main_py = os.path.join(APP_DIR, "main.py")
    if os.path.exists(main_py):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        shutil.copy2(main_py, os.path.join(BACKUP_DIR, "main.py"))
        log.info("已备份当前代码")


def deploy_code(code: str):
    """部署新代码"""
    os.makedirs(APP_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    main_py = os.path.join(APP_DIR, "main.py")
    with open(main_py, "w") as f:
        f.write(code)
    log.info("新代码已部署")


def start_app(task_id: str = "", attempt: int = 1):
    """启动应用进程。

    注入：
    - PYTHONPATH 让子进程能 `from device import ...`
    - TASK_ID / CODEGEN_ATTEMPT 让 SDK _tracer 知道把事件归属到哪个 task
    - start_new_session=True 让 main.py 成为新 process group leader，停的时候
      killpg 一次性把它和它派生的孙子（paplay/aplay/eye_matrix 等）整组终止
    """
    global current_process, current_pgid
    main_py = os.path.join(APP_DIR, "main.py")
    env = {
        **os.environ,
        "PYTHONPATH": SDK_DIR,
        "TASK_ID": task_id,
        "CODEGEN_ATTEMPT": str(attempt),
    }
    current_process = subprocess.Popen(
        ["python3", main_py],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=APP_DIR,
        env=env,
        start_new_session=True,
    )
    try:
        current_pgid = os.getpgid(current_process.pid)
    except ProcessLookupError:
        current_pgid = None
    # 持久化 pgid — runner 进程崩了下次启动时还能清孤儿组
    if current_pgid is not None:
        try:
            os.makedirs(os.path.dirname(PGID_FILE), exist_ok=True)
            with open(PGID_FILE, "w") as f:
                f.write(str(current_pgid))
        except Exception as e:
            log.warning("写 pgid 失败: %s", e)
    log.info("应用已启动, PID=%s PGID=%s (TASK_ID=%s, attempt=%s)",
             current_process.pid, current_pgid, task_id, attempt)
    return current_process


def post_deploy_event(task_id: str, attempt: int, code_size: int):
    """部署成功瞬间给 backend 写一个 deploy event，作为 runtime log 的分隔符。"""
    try:
        requests.post(
            f"{SERVER_URL}/api/codegen/device/event",
            json={
                "device_id": DEVICE_ID,
                "device_secret": DEVICE_SECRET,
                "task_id": task_id,
                "attempt": attempt,
                "op": "deploy",
                "input": {"code_size_bytes": code_size},
            },
            headers={"Authorization": HARDWARE_TOKEN},
            timeout=5,
        )
    except Exception as e:
        log.warning("deploy event 上报失败: %s", e)


def read_output(proc, max_chars=2000):
    """非阻塞读取进程输出"""
    import select

    stdout = ""
    stderr = ""
    if proc.stdout and select.select([proc.stdout], [], [], 0)[0]:
        stdout = proc.stdout.read(max_chars).decode("utf-8", errors="replace")
    if proc.stderr and select.select([proc.stderr], [], [], 0)[0]:
        stderr = proc.stderr.read(max_chars).decode("utf-8", errors="replace")
    return stdout[-max_chars:], stderr[-max_chars:]


def check_expected_outputs(expected_outputs: dict) -> dict:
    """检查预期输出是否存在"""
    result = {}
    if not expected_outputs:
        return result

    for pattern in expected_outputs.get("files", []):
        if pattern.startswith("/"):
            full_pattern = pattern
        elif pattern.startswith("output/"):
            # output/xxx 解析到 /opt/device-app/output/xxx
            full_pattern = os.path.join(os.path.dirname(OUTPUT_DIR), pattern)
        else:
            full_pattern = os.path.join(APP_DIR, pattern)
        matches = glob.glob(full_pattern)
        result[f"file:{pattern}"] = len(matches) > 0

    return result


def report_to_server(task_id, success, stdout, stderr, output_check, process_alive):
    """上报执行结果"""
    try:
        resp = requests.post(
            f"{SERVER_URL}/api/codegen/device/report",
            json={
                "device_id": DEVICE_ID,
                "device_secret": DEVICE_SECRET,
                "task_id": task_id,
                "success": success,
                "stdout": stdout,
                "stderr": stderr,
                "output_check": output_check,
                "process_alive": process_alive,
            },
            headers={"Authorization": HARDWARE_TOKEN},
            timeout=10,
        )
        log.info("上报结果: success=%s, status_code=%s", success, resp.status_code)
    except Exception as e:
        log.error("上报失败: %s", e)


def rollback():
    """回滚到备份版本"""
    backup_main = os.path.join(BACKUP_DIR, "main.py")
    if os.path.exists(backup_main):
        shutil.copy2(backup_main, os.path.join(APP_DIR, "main.py"))
        log.info("已回滚到备份版本")
        start_app()
    else:
        log.warning("无备份可回滚")


def main():
    log.info("Runner 启动: device_id=%s, server=%s", DEVICE_ID, SERVER_URL)
    # 上一轮 runner 崩溃可能留下孤儿应用组（孙子被 init 接管继续跑硬件）
    kill_stale_pgid()

    while True:
        task = poll_server()

        if not task:
            time.sleep(POLL_INTERVAL)
            continue

        task_id = task["task_id"]
        code = task["code"]
        expected_outputs = task.get("expected_outputs", {})
        attempt = int(task.get("attempt") or 1)

        log.info("收到任务: %s (attempt=%s)", task_id, attempt)

        # 停旧进程 → 备份 → 部署 → 起 deploy event 标记 → 启动
        stop_current_app()
        backup_current()
        deploy_code(code)
        post_deploy_event(task_id, attempt, len(code))
        proc = start_app(task_id=task_id, attempt=attempt)

        # 验证期
        log.info("验证期: 等待 %s 秒...", VALIDATION_WAIT)
        time.sleep(VALIDATION_WAIT)

        # 检查结果
        process_alive = proc.poll() is None
        stdout, stderr = read_output(proc)

        # 如果进程已退出，读取完整输出
        if not process_alive:
            remaining_out, remaining_err = proc.communicate(timeout=5)
            stdout += remaining_out.decode("utf-8", errors="replace")
            stderr += remaining_err.decode("utf-8", errors="replace")
            stdout = stdout[-2000:]
            stderr = stderr[-2000:]

        output_check = check_expected_outputs(expected_outputs)
        output_check_passed = all(output_check.values()) if output_check else True

        success = process_alive and output_check_passed and not stderr.strip()

        # 上报
        report_to_server(task_id, success, stdout, stderr, output_check, process_alive)

        # 失败则回滚
        if not success:
            log.warning("任务 %s 验证失败, 回滚", task_id)
            stop_current_app()
            rollback()


if __name__ == "__main__":
    main()
