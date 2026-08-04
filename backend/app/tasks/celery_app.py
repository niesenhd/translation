"""Celery 应用 + 翻译任务。

- FIFO 排队：Celery 默认使用 Redis 列表，先进先出
- 全局并发：管理员通过 system_config.max_concurrency 动态配置（应用层 Redis 计数器实现）
- 定时任务：每天凌晨 3 点执行过期文件清理（需启动 celery beat）
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import timedelta

from celery import Celery
from celery.schedules import crontab
from sqlalchemy import or_, select, update

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.storage import download_bytes, upload_bytes
from app.models.base import utc_now
from app.models.task import TaskStatus, TranslationTask
from app.services.document_engine import TranslationContext, translate_file
from app.services.glossary import get_glossary_for_lang_pair
from app.services.retention import cleanup_expired_files
from app.services.tm_lookup import lookup_tm
from app.services.translator import get_translator

logger = logging.getLogger(__name__)

_settings = get_settings()
_redis_process_client = None
_redis_process_pid: int | None = None
_redis_init_lock = threading.Lock()

celery_app = Celery(
    "translation",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=False,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # 严格 FIFO，不预取
    beat_schedule={
        # 每天 03:00 清理过期文件
        "cleanup-expired-files": {
            "task": "translation.cleanup_expired_files",
            "schedule": crontab(hour=3, minute=0),
        },
        # 每 5 分钟对账：把 worker 重启/异常退出导致的"孤儿 RUNNING 任务"标记为失败，
        # 避免状态永久卡在 RUNNING（worker 被杀时来不及跑收尾）。
        "reconcile-stuck-tasks": {
            "task": "translation.reconcile_stuck_tasks",
            "schedule": crontab(minute="*/5"),
        },
        # OA 用户同步：工作时段 9:00-18:00 每 3 小时（9:00, 12:00, 15:00, 18:00）
        "sync-oa-users": {
            "task": "translation.sync_oa_users",
            "schedule": crontab(hour="9-18/3", minute=0),
        },
    },
)


@celery_app.task(name="translation.cleanup_expired_files")
def cleanup_expired_files_task() -> dict:
    """Celery Beat 定时任务：清理过期文件（覆写 + 删除）。"""
    return cleanup_expired_files().as_dict()


@celery_app.task(name="translation.sync_oa_users")
def sync_oa_users_task() -> dict:
    """Celery Beat 定时任务：从律智荟 OA 同步在职人员到本地 users 表。"""
    from app.services.oa_sync import sync_users_from_oa
    return sync_users_from_oa()


@celery_app.task(name="translation.reconcile_stuck_tasks")
def reconcile_stuck_tasks_task() -> dict:
    """恢复孤儿 RUNNING/QUEUED，并清理中断的 UPLOADING 任务。"""
    db = SessionLocal()
    now = utc_now()
    running_cutoff = now - timedelta(seconds=_ORPHAN_TIMEOUT_SECONDS)
    queued_cutoff = now - timedelta(seconds=_QUEUED_RECONCILE_SECONDS)
    uploading_cutoff = now - timedelta(seconds=_UPLOADING_TIMEOUT_SECONDS)
    requeue_ids: list[str] = []
    stale_slots: list[tuple[str, str | None]] = []
    uploading_objects: list[str] = []
    checked_running = checked_queued = checked_uploading = 0
    try:
        running = list(db.scalars(
            select(TranslationTask).where(TranslationTask.status == TaskStatus.RUNNING)
        ))
        checked_running = len(running)
        for task in running:
            if task.heartbeat_at is None or task.heartbeat_at < running_cutoff:
                result = db.execute(
                    update(TranslationTask)
                    .where(
                        TranslationTask.id == task.id,
                        TranslationTask.status == TaskStatus.RUNNING,
                        TranslationTask.attempt_id == task.attempt_id,
                        or_(
                            TranslationTask.heartbeat_at.is_(None),
                            TranslationTask.heartbeat_at < running_cutoff,
                        ),
                    )
                    .values(
                        status=TaskStatus.QUEUED,
                        progress=0,
                        error_message=None,
                        attempt_id=None,
                        heartbeat_at=None,
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    logger.info("对账：任务 %s 的状态或心跳已变化，跳过标记", task.id)
                    continue
                stale_slots.append((task.id, task.attempt_id))
                requeue_ids.append(task.id)
                logger.warning(
                    "对账：孤儿任务 %s（attempt=%s）已恢复为 QUEUED",
                    task.id,
                    task.attempt_id,
                )

        stale_queued = list(db.scalars(
            select(TranslationTask).where(
                TranslationTask.status == TaskStatus.QUEUED,
                TranslationTask.updated_at < queued_cutoff,
            )
        ))
        checked_queued = len(stale_queued)
        for task in stale_queued:
            refreshed = db.execute(
                update(TranslationTask)
                .where(
                    TranslationTask.id == task.id,
                    TranslationTask.status == TaskStatus.QUEUED,
                    TranslationTask.updated_at < queued_cutoff,
                )
                .values(updated_at=now)
            )
            if refreshed.rowcount == 1 and task.id not in requeue_ids:
                requeue_ids.append(task.id)

        stale_uploading = list(db.scalars(
            select(TranslationTask).where(
                TranslationTask.status == TaskStatus.UPLOADING,
                TranslationTask.updated_at < uploading_cutoff,
            )
        ))
        checked_uploading = len(stale_uploading)
        for task in stale_uploading:
            failed = db.execute(
                update(TranslationTask)
                .where(
                    TranslationTask.id == task.id,
                    TranslationTask.status == TaskStatus.UPLOADING,
                    TranslationTask.updated_at < uploading_cutoff,
                )
                .values(
                    status=TaskStatus.FAILED,
                    error_message="上传中断或超时，请重新上传文件。",
                    updated_at=now,
                )
            )
            if failed.rowcount == 1 and task.source_object:
                uploading_objects.append(task.source_object)

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    for task_id, attempt_id in stale_slots:
        _release_concurrency_slot(task_id, attempt_id)

    if uploading_objects:
        from app.core.storage import secure_remove_object

        for object_name in uploading_objects:
            try:
                secure_remove_object(object_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("清理超时上传对象 %s 失败：%s", object_name, exc)

    enqueued = 0
    enqueue_failed = 0
    for task_id in requeue_ids:
        try:
            celery_app.send_task("translation.run", args=[task_id])
            enqueued += 1
        except Exception as exc:  # noqa: BLE001
            # 保持 QUEUED，下一轮对账会继续尝试；broker 故障时不把可恢复任务误置失败。
            enqueue_failed += 1
            logger.error("对账：任务 %s 重新入队失败，将在下轮重试：%s", task_id, exc)

    result = {
        "running_checked": checked_running,
        "running_recovered": len(stale_slots),
        "queued_checked": checked_queued,
        "uploading_checked": checked_uploading,
        "uploading_failed": len(uploading_objects),
        "enqueued": enqueued,
        "enqueue_failed": enqueue_failed,
    }
    logger.info("任务对账完成：%s", result)
    return result


class TaskCancelled(Exception):
    """任务已被用户删除/取消，抛出该异常使 Worker 立即退出当前任务。"""


def _check_task_alive(task_id: str, attempt_id: str | None = None) -> None:
    """确认任务未删除；提供 attempt_id 时还要确认当前 worker 仍拥有该尝试。

    用独立 Session 避免与主事务冲突；只读查询，无副作用。
    """
    s = SessionLocal()
    try:
        t = s.get(TranslationTask, task_id)
        if t is None or t.status == TaskStatus.DELETED:
            raise TaskCancelled(f"任务 {task_id} 已被删除/取消")
        if attempt_id is not None and (
            t.status != TaskStatus.RUNNING or t.attempt_id != attempt_id
        ):
            raise TaskCancelled(f"任务 {task_id} 的执行权已转移")
    finally:
        s.close()


# ====== 应用层并发闸门（基于 Redis ZSET + TTL 自愈）======
# 通过 ZSET 记录"正在翻译"的任务（member=task_id，score=最近心跳时间），
# 使管理员在后台调整 max_concurrency 能即时生效。
# Celery worker 的 --concurrency 只控制"同时执行的任务数"（进程槽位），
# 本闸门在其上再加一层"全局翻译许可"，确保实际并发翻译文件数不超过管理员上限。
#
# 相比单纯的 INCR 计数器，ZSET + TTL 方案解决两个隐患：
# 1) 槽位泄漏：worker 子进程被 OOM/SIGKILL 强杀（finally 来不及释放）时，
#    其条目会在 TTL 后自动过期回收，不会永久占用名额。
# 2) 误清计数：无需任何"启动清零"逻辑，从根本上避免 beat / 多 worker 进程
#    在他人正运行时把计数器清零导致超额放行。

_CONCURRENCY_ZSET = "translation:running"
# 槽位心跳过期秒数：超过此时长无心跳即视为死任务被回收。
# 需大于"任务获得槽位 → 第一次进度回调"的最坏耗时（大扫描件 OCR 较慢），留足余量。
_SLOT_TTL_SECONDS = 600
_HEARTBEAT_INTERVAL_SECONDS = 30.0
_ORPHAN_TIMEOUT_SECONDS = 120
_QUEUED_RECONCILE_SECONDS = 15 * 60
_UPLOADING_TIMEOUT_SECONDS = 15 * 60

# 原子获取脚本：清理僵尸 → 已在册则续期放行 → 在册数未满则登记放行 → 否则拒绝
_ACQUIRE_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local maxc = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - ttl)
if redis.call('ZSCORE', key, member) then
    redis.call('ZADD', key, now, member)
    return 1
end
if redis.call('ZCARD', key) < maxc then
    redis.call('ZADD', key, now, member)
    return 1
end
return 0
"""


def _redis_client():
    """返回当前 worker/beat 进程复用的 Redis 客户端与连接池。"""
    global _redis_process_client, _redis_process_pid
    import redis

    process_id = os.getpid()
    if _redis_process_client is None or _redis_process_pid != process_id:
        with _redis_init_lock:
            if _redis_process_client is None or _redis_process_pid != process_id:
                old_client = _redis_process_client
                _redis_process_client = redis.from_url(
                    _settings.redis_url,
                    decode_responses=True,
                )
                _redis_process_pid = process_id
                if old_client is not None:
                    try:
                        old_client.close()
                    except Exception:  # noqa: BLE001
                        pass
    return _redis_process_client


def _slot_member(task_id: str, attempt_id: str | None) -> str:
    return f"{task_id}:{attempt_id}" if attempt_id else task_id


def _get_max_concurrency() -> int:
    """从 system_config 读取管理员配置的并发上限，回退到环境变量默认值。"""
    from app.api.admin import KEY_MAX_CONCURRENCY
    from app.models.system_config import SystemConfig

    db = SessionLocal()
    try:
        cfg = db.get(SystemConfig, KEY_MAX_CONCURRENCY)
        if cfg is not None:
            try:
                return max(1, int(cfg.value))
            except (ValueError, TypeError):
                pass
    finally:
        db.close()
    return max(1, _settings.translation_max_concurrency)


def _acquire_concurrency_slot(
    task_id: str, attempt_id: str, poll_interval: float = 2.0
) -> None:
    """获取翻译许可，超出上限时阻塞等待。

    用 Redis ZSET + Lua 原子判断：清理 TTL 过期的僵尸条目后，在册数未满才放行。
    等待期间定期检查任务是否已被删除，避免对已删任务继续占用 worker。
    """
    member = _slot_member(task_id, attempt_id)
    while True:
        # 任务在排队期间可能已被删除
        _check_task_alive(task_id)
        try:
            r = _redis_client()
            acquire = r.register_script(_ACQUIRE_LUA)
            max_c = _get_max_concurrency()
            ok = acquire(
                keys=[_CONCURRENCY_ZSET],
                args=[time.time(), _SLOT_TTL_SECONDS, max_c, member],
            )
            if ok == 1:
                logger.info("任务 %s 获得并发槽位（上限 %d）", task_id, max_c)
                return
            logger.info("任务 %s 等待并发槽位（上限 %d）", task_id, max_c)
        except Exception as exc:  # noqa: BLE001
            # fail-closed：Redis 不可用时绝不能绕过全局并发上限。
            logger.error("并发闸门不可用，任务 %s 保持等待：%s", task_id, exc)
        time.sleep(poll_interval)


def _heartbeat_concurrency_slot(task_id: str, attempt_id: str) -> None:
    """刷新槽位心跳（任务执行中周期调用），避免长任务被 TTL 误回收。"""
    try:
        _redis_client().zadd(
            _CONCURRENCY_ZSET,
            {_slot_member(task_id, attempt_id): time.time()},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("任务 %s 刷新 Redis 并发心跳失败：%s", task_id, exc)


def _release_concurrency_slot(task_id: str, attempt_id: str | None) -> None:
    """释放翻译许可。"""
    try:
        _redis_client().zrem(_CONCURRENCY_ZSET, _slot_member(task_id, attempt_id))
    except Exception as exc:
        logger.warning("释放并发槽位失败：%s", exc)


def _heartbeat_attempt(task_id: str, attempt_id: str) -> bool:
    """刷新 Redis 与数据库心跳；返回该 attempt 是否仍拥有任务。"""
    _heartbeat_concurrency_slot(task_id, attempt_id)
    heartbeat_db = SessionLocal()
    try:
        result = heartbeat_db.execute(
            update(TranslationTask)
            .where(
                TranslationTask.id == task_id,
                TranslationTask.status == TaskStatus.RUNNING,
                TranslationTask.attempt_id == attempt_id,
            )
            .values(heartbeat_at=utc_now())
        )
        heartbeat_db.commit()
        return result.rowcount == 1
    except Exception as exc:  # noqa: BLE001
        heartbeat_db.rollback()
        logger.warning("任务 %s 刷新数据库心跳失败：%s", task_id, exc)
        # 数据库暂时不可用不能证明执行权已转移；恢复后条件更新仍会校验 attempt。
        return True
    finally:
        heartbeat_db.close()


def _heartbeat_loop(
    task_id: str,
    attempt_id: str,
    stop_event: threading.Event,
    ownership_lost: threading.Event,
) -> None:
    """独立于文档进度运行，覆盖下载、OCR、术语抽取、翻译与上传阶段。"""
    while not stop_event.is_set():
        if not _heartbeat_attempt(task_id, attempt_id):
            ownership_lost.set()
            return
        stop_event.wait(_HEARTBEAT_INTERVAL_SECONDS)


def _humanize_error(exc: Exception) -> str:
    """把内部异常转成用户能看懂的中文错误描述。"""
    msg = str(exc)
    low = msg.lower()
    if "rate limit" in low or "429" in low or "too many requests" in low:
        return "翻译模型限流，请稍后重试。建议降低并发或升级 API 配额。"
    if "timeout" in low or "timed out" in low:
        return "翻译模型响应超时，请检查网络或稍后重试。"
    if "connection" in low or "connect" in low:
        return f"无法连接翻译服务：{msg}"
    if "401" in low or "unauthorized" in low or "api key" in low.replace("apikey", "api key"):
        return "翻译模型 API Key 无效或已过期，请检查模型配置。"
    if "400" in low or "bad request" in low:
        return f"翻译模型拒绝请求（可能是内容超长或触发安全策略）：{msg}"
    if "熔断" in msg or "翻译服务异常" in msg:
        return msg  # 我们自己的熔断信息已是中文
    if "ocr" in low or "paddle" in low:
        return f"图片识别失败：{msg}"
    if "minio" in low or "s3" in low:
        return f"文件存储错误：{msg}"
    return f"翻译失败：{msg}"


@celery_app.task(
    name="translation.run",
    bind=True,
    max_retries=0,
    acks_late=True,
    reject_on_worker_lost=True,
)
def run_translation_task(self, task_id: str) -> None:  # noqa: ARG001
    db = SessionLocal()
    attempt_id = str(uuid.uuid4())
    slot_acquired = False
    attempt_claimed = False
    heartbeat_stop = threading.Event()
    ownership_lost = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    try:
        task = db.get(TranslationTask, task_id)
        if task is None:
            return
        # 任务可能在排队期间已被删除
        if task.status == TaskStatus.DELETED:
            return

        # 应用层并发闸门：等待获取全局翻译许可
        # 在标记 RUNNING 之前等待，让用户看到的是"排队中"而非假进度
        _acquire_concurrency_slot(task_id, attempt_id)
        slot_acquired = True

        # 条件更新"抢占"任务：仅 QUEUED 状态可转 RUNNING。
        # 两个作用：1) 任务在等槽位期间被删除时不会把 DELETED 覆写回 RUNNING;
        # 2) 同一任务被重复入队（如并发 retry）时只有一个 worker 能抢到，
        #    另一个在此退出，不会双份翻译。
        claimed = db.execute(
            update(TranslationTask)
            .where(
                TranslationTask.id == task_id,
                TranslationTask.status == TaskStatus.QUEUED,
            )
            .values(
                status=TaskStatus.RUNNING,
                progress=10,
                attempt_id=attempt_id,
                heartbeat_at=utc_now(),
            )
        )
        db.commit()
        if claimed.rowcount != 1:
            logger.info("任务 %s 状态已非 QUEUED（已删除或已被其它 worker 抢占），跳过", task_id)
            return
        attempt_claimed = True
        db.refresh(task)

        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(task_id, attempt_id, heartbeat_stop, ownership_lost),
            name=f"translation-heartbeat-{task_id}",
            daemon=True,
        )
        heartbeat_thread.start()

        source_bytes = download_bytes(task.source_object)

        # 段落级进度回调：把 [10%, 90%] 映射到实际段数；最少 1% 步进合并以减少写库
        # 注意：on_progress 会被多个翻译线程并发调用，SQLAlchemy Session 非线程安全，
        # 必须用锁串行化 DB 操作。
        last_written = {"v": 10}
        progress_lock = threading.Lock()

        def on_progress(done: int, total: int) -> None:
            if ownership_lost.is_set():
                raise TaskCancelled(f"任务 {task_id} 的执行权已转移")
            pct = 10 + int((done / max(total, 1)) * 80)
            pct = max(10, min(90, pct))
            with progress_lock:
                if pct - last_written["v"] < 1:
                    return
                last_written["v"] = pct
            # 用独立 Session 写 DB，避免多线程共享主 Session 的 commit 问题
            # SQLAlchemy Session 非线程安全，多线程并发 commit 会导致状态不一致
            progress_db = SessionLocal()
            try:
                result = progress_db.execute(
                    update(TranslationTask)
                    .where(
                        TranslationTask.id == task_id,
                        TranslationTask.status == TaskStatus.RUNNING,
                        TranslationTask.attempt_id == attempt_id,
                    )
                    .values(progress=pct)
                )
                progress_db.commit()
                if result.rowcount != 1:
                    ownership_lost.set()
                    raise TaskCancelled(f"任务 {task_id} 的执行权已转移")
            except TaskCancelled:
                progress_db.rollback()
                raise
            except Exception as exc:  # noqa: BLE001
                progress_db.rollback()
                logger.warning("任务 %s 写入进度失败：%s", task_id, exc)
            finally:
                progress_db.close()

        # 加载术语库
        glossary = get_glossary_for_lang_pair(task.source_lang, task.target_lang)

        # 每个任务开始前重置模型单例，确保管理员后台切换模型后 worker 进程立即生效。
        # （reset_* 只影响本 worker 子进程的内存单例；prefork 下同一进程同一时刻只跑
        #  一个任务，故此处重置安全，不会影响其它正在执行的任务。）
        from app.services.translator import reset_translator
        from app.services.ocr import reset_ocr_client
        reset_translator()
        reset_ocr_client()
        translator = get_translator()

        # 文档级术语抽取（功能B）：一次调用抽出全文关键术语，合并进 glossary（STRICT），
        # 保证定义术语/专有名词全文统一译法。失败或文本过短返回空，不影响翻译。
        if _settings.translation_doc_term_extraction:
            from app.services.doc_term_extractor import extract_document_terms
            doc_terms = extract_document_terms(
                task.file_ext, source_bytes,
                task.source_lang, task.target_lang, translator,
            )
            if doc_terms:
                # doc-term 优先：放在列表前，_match_glossary 的 strict 优先 +
                # 最长优先去重会确保它们压过同源的全局 preferred 条目
                glossary = doc_terms + (glossary or [])

        ctx = TranslationContext(
            target_lang=task.target_lang,
            source_lang=task.source_lang,
            output_mode=task.output_mode,
            translate_images=task.translate_images.value,
            on_progress=on_progress,
            glossary=glossary,
            tm_lookup=lookup_tm,
            refine_mode=getattr(task, "refine_mode", None) and task.refine_mode.value or "none",
            footnote_mode=getattr(task, "footnote_mode", None) and task.footnote_mode.value or "bilingual",
        )

        result_bytes, out_ext = translate_file(
            task.file_ext,
            source_bytes,
            translator,
            ctx,
            pdf_output_format=task.pdf_output_format.value,
        )

        progressed = db.execute(
            update(TranslationTask)
            .where(
                TranslationTask.id == task_id,
                TranslationTask.status == TaskStatus.RUNNING,
                TranslationTask.attempt_id == attempt_id,
            )
            .values(progress=90)
        )
        db.commit()
        if progressed.rowcount != 1:
            raise TaskCancelled(f"任务 {task_id} 的执行权已转移")

        # 上传译文前最后确认一次任务未被删除，缩小竞态窗口
        _check_task_alive(task_id, attempt_id)

        result_object = f"results/{task.id}.{out_ext}"
        upload_bytes(result_object, result_bytes)

        # 条件更新收尾：仅 RUNNING 状态可转 SUCCEEDED。
        # 若用户在"最后一次存活检查 → 此处提交"之间删除了任务，rowcount=0，
        # 此时必须把刚上传的译文清掉——否则用户已"安全删除"的法律文档译文
        # 会重新出现在 MinIO 并永久残留。
        finished = db.execute(
            update(TranslationTask)
            .where(
                TranslationTask.id == task_id,
                TranslationTask.status == TaskStatus.RUNNING,
                TranslationTask.attempt_id == attempt_id,
            )
            .values(
                result_object=result_object,
                status=TaskStatus.SUCCEEDED,
                progress=100,
                heartbeat_at=utc_now(),
            )
        )
        db.commit()
        if finished.rowcount != 1:
            logger.info("任务 %s 在收尾前被删除，清理已上传的译文对象", task_id)
            from app.core.storage import secure_remove_object
            try:
                secure_remove_object(result_object)
            except Exception as exc:  # noqa: BLE001
                logger.warning("清理译文对象 %s 失败：%s", result_object, exc)
            return
    except TaskCancelled as exc:
        # 任务已删除：不修改状态（保持 DELETED），不重试，安静退出
        try:
            db.rollback()
        except Exception:
            pass
        return
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        # attempt 已抢占时只允许它结束自己的 RUNNING；抢占前则只处理仍为 QUEUED
        # 且没有属主的任务。删除、重试和新 attempt 都不会被旧 worker 覆盖。
        try:
            failure = update(TranslationTask).where(TranslationTask.id == task_id)
            if attempt_claimed:
                failure = failure.where(
                    TranslationTask.status == TaskStatus.RUNNING,
                    TranslationTask.attempt_id == attempt_id,
                )
            else:
                failure = failure.where(
                    TranslationTask.status == TaskStatus.QUEUED,
                    TranslationTask.attempt_id.is_(None),
                )
            db.execute(
                failure.values(
                    status=TaskStatus.FAILED,
                    error_message=_humanize_error(exc)[:1000],
                    heartbeat_at=utc_now() if attempt_claimed else None,
                )
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        # 不再 raise：max_retries=0 已禁用重试，且抛异常会导致日志混乱
        return
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=_HEARTBEAT_INTERVAL_SECONDS + 1)
        if slot_acquired:
            _release_concurrency_slot(task_id, attempt_id)
        db.close()
