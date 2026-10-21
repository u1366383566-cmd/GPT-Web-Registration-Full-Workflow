# -*- coding: utf-8 -*-
"""本地 AT 过期巡检；仅失效账号调用查活（同会话自动查套餐）。"""
from __future__ import annotations

import logging
import math
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from contextlib import contextmanager

from core.inspection_lease import acquire_inspection_lease

from core import db, live_check_service
from core.chatgpt_plan import decode_jwt_payload_unverified, normalize_token

logger = logging.getLogger(__name__)
DEFAULT_WORKERS = 3
MAX_WORKERS = 32
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="account-inspection")


def token_needs_refresh(token: str, now: float | None = None) -> bool:
    """只读取 JWT exp，不访问 API；缺失/格式错误也按失效处理。"""
    if not isinstance(token, str):
        return True
    token = normalize_token(token)
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        return True
    payload = decode_jwt_payload_unverified(token)
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return True
    try:
        return not math.isfinite(exp) or exp <= (time.time() if now is None else now)
    except (ValueError, OverflowError):
        return True


def _check_live(account: dict, executor: ThreadPoolExecutor) -> dict:
    queued = live_check_service.enqueue_account_live_check(
        account_id=account["id"], email=account.get("email", ""),
        trigger="inspection", return_future=True, executor=executor,
    )
    if not queued.get("accepted"):
        return {"ok": False, "status": "rejected", "error": queued.get("error") or "查活入队被拒绝"}
    return queued["future"].result()


def validate_workers(workers: int) -> int:
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"巡检线程数必须为 1–{MAX_WORKERS} 的整数")
    return workers


class InspectionStopped(RuntimeError):
    """持久化任务已结束，旧 worker 不再执行后续阶段。"""


def _run(run: dict) -> None:
    # 每个 ID 只进入一次共享迭代器；领取与统计各自加锁。
    account_ids = list(dict.fromkeys(run["account_ids"]))
    run["account_ids"] = account_ids
    run["total_count"] = len(account_ids)
    run.setdefault("skipped_count", 0)
    run.setdefault("deactivated_skipped_count", 0)
    pending = iter(account_ids)
    claim_lock = threading.Lock()
    stats_lock = threading.Lock()
    stopped = threading.Event()

    def persist(failure=None):
        saved = db.save_account_inspection(run, failure=failure) if failure is not None else db.save_account_inspection(run)
        if not saved:
            raise InspectionStopped("巡检任务已结束或被中断")

    def record(*, failure=None, **counts):
        with stats_lock:
            previous = {key: run[key] for key in counts}
            for key, value in counts.items():
                run[key] += value
            try:
                persist(failure)
            except BaseException:
                # 明细落库失败时不能让 finally 单独保存已经增加的失败计数。
                run.update(previous)
                raise

    def inspect_account(account_id, stage_executor):
        account = db.get_account(account_id)
        if account is None:
            record(skipped_count=1)
            return
        if str(account.get("live_check_status") or "").strip().lower() == "deactivated":
            record(skipped_count=1, deactivated_skipped_count=1, failure=dict(
                account_id=account_id, email=account.get("email", ""), status="deactivated_skipped",
                error="账号已标记为已废，本次巡检跳过，不执行 AT 检查、查活及查套餐。" +
                      (" 原因：" + str(account["live_check_error"]) if account.get("live_check_error") else ""),
            ))
            return
        invalid = token_needs_refresh(account.get("access_token", ""))
        record(checked_count=1, invalid_count=int(invalid))
        if not invalid:
            return
        try:
            live_result = _check_live(account, stage_executor)
        except Exception as exc:
            logger.exception("巡检查活异常 account_id=%s", account_id)
            live_result = {"ok": False, "status": "error", "error": f"{type(exc).__name__}: {str(exc)[:1500]}"}
        live_ok = bool(live_result.get("ok"))
        counts = {"live_success_count" if live_ok else "live_failed_count": 1}
        if live_ok:
            plan_ok = bool((live_result.get("plan_result") or {}).get("ok"))
            counts["plan_success_count" if plan_ok else "plan_failed_count"] = 1
        failure = None if live_ok else dict(account_id=account_id, email=account.get("email", ""),
                                            status=live_result.get("status") or "failed",
                                            error=live_result.get("error") or "查活失败，未返回具体原因")
        record(failure=failure, **counts)

    def worker(stage_executor):
        while True:
            with claim_lock:
                if stopped.is_set():
                    return
                account_id = next(pending, None)
            if account_id is None:
                return
            try:
                inspect_account(account_id, stage_executor)
            except BaseException:
                # 一个 worker 发生存储/执行故障后，不应让其他 worker 继续领取整份清单。
                # 已在执行中的账号仍完成收尾，避免遗留会话和队列占用。
                stopped.set()
                raise

    try:
        workers = validate_workers(run.get("workers", DEFAULT_WORKERS))
        run["status"] = "running"
        persist()
        # 独立阶段线程池，不受手动查活固定线程数限制；仍复用统一账号占用与限流。
        # 阶段执行与等待它的巡检 worker 分池，避免线程池饥饿死锁。
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="inspection-stage") as stages:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="inspection-worker") as pool:
                futures = [pool.submit(worker, stages) for _ in range(min(workers, len(account_ids)))]
                for future in futures:
                    future.result()
        run["status"] = "completed"
    except InspectionStopped as exc:
        run.update(status="interrupted", error=str(exc))
    except Exception as exc:
        logger.exception("账号巡检异常")
        run.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        run["finished_at"] = datetime.now().isoformat(timespec="seconds")
        db.save_account_inspection(run)


@contextmanager
def startup_recovery_guard():
    """同库巡检仍持有进程锁时，其他实例不得把它及其阶段任务恢复为失败。"""
    lease = acquire_inspection_lease()
    try:
        yield lease is not None
    finally:
        if lease is not None:
            lease.close()


def _run_with_lease(run, lease):
    try:
        _run(run)
    finally:
        lease.close()


def _release_cancelled_inspection(future, run, lease):
    if not future.cancelled():
        return
    try:
        cancelled = dict(run, status="interrupted", finished_at=datetime.now().isoformat(timespec="seconds"),
                         error="巡检在开始执行前被取消")
        db.save_account_inspection(cancelled)
    except Exception:
        logger.exception("写入巡检取消状态失败")
    finally:
        lease.close()


def _recover_orphaned_inspections() -> None:
    # 调用方必须持有进程锁；此时不会有执行中的巡检，可以清理异常退出遗留状态。
    db.recover_interrupted_account_inspections(
        error="巡检执行器已退出或任务异常中断，已释放任务占用；统计保留已完成部分")


def list_inspections(limit: int = 50, offset: int = 0) -> dict:
    # 不依赖重启：最后一次数据库写入失败时，下一次轮询也能解除虚假的忙碌状态。
    lease = acquire_inspection_lease()
    try:
        if lease is not None:
            _recover_orphaned_inspections()
        return db.list_account_inspections(limit=limit, offset=offset)
    finally:
        if lease is not None:
            lease.close()


def start_inspection(workers: int = DEFAULT_WORKERS, archived: str | bool = "0") -> tuple[dict, bool]:
    workers = validate_workers(workers)
    if type(archived) is bool:
        archived = "only" if archived else "0"
    if not isinstance(archived, str) or archived not in ("0", "only", "all"):
        raise ValueError("归档范围必须为 0（未归档）、only（已归档）或 all（全部）")
    lease = acquire_inspection_lease()
    if lease is None:
        active = db.list_account_inspections()["active"]
        return active or {"status": "queued", "error": "巡检正在启动或收尾，请稍后重试"}, False
    submitted = False
    try:
        _recover_orphaned_inspections()
        run, created = db.create_account_inspection(workers=workers, archived=archived)
        if created:
            try:
                future = _EXECUTOR.submit(_run_with_lease, run.copy(), lease)
                submitted = True
                future.add_done_callback(lambda done: _release_cancelled_inspection(done, run, lease))
            except Exception as exc:
                run.update(status="failed", error=str(exc)[:200], finished_at=datetime.now().isoformat(timespec="seconds"))
                db.save_account_inspection(run)
                raise
        return {k: v for k, v in run.items() if k != "account_ids"}, created
    finally:
        if not submitted:
            lease.close()
