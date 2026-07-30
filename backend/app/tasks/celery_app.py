"""Celery 应用 + 翻译任务。

- FIFO 排队：Celery 默认使用 Redis 列表，先进先出
- 全局并发：管理员通过 system_config.max_concurrency 动态配置（应用层 Redis 计数器实现）
- 定时任务：每天凌晨 3 点执行过期文件清理（需启动 celery beat）
"""
from __future__ import annotations

import logging
import threading
import time

from celery import Celery
from celery.schedules import crontab
from sqlalchemy import text, update

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.storage import download_bytes, upload_bytes
from app.models.task import TaskStatus, TranslationTask
from app.services.document_engine import TranslationContext, translate_file
from app.services.glossary import get_glossary_for_lang_pair
from app.services.retention import cleanup_expired_files
from app.services.tm_lookup import lookup_tm
from app.services.translator import get_translator

logger = logging.getLogger(__name__)

_settings = get_settings()

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
    """对账孤儿任务：RUNNING 但已无活跃 worker（并发槽位过期/缺失）的任务标记为 FAILED。

    判定：任务处于 RUNNING，但其并发槽位（ZSET translation:running）缺失或心跳超过 TTL。
    正在跑的任务每 ~2s 刷新槽位心跳，槽位 600s TTL 自愈；worker 被杀后心跳停止、
    槽位过期消失，但 DB 状态仍停在 RUNNING —— 这类孤儿任务在此自动标记失败，
    用户可点重试。同时清理可能泄漏的槽位。
    """
    from sqlalchemy import select, update

    db = SessionLocal()
    reconciled = 0
    try:
        running = list(db.scalars(
            select(TranslationTask).where(TranslationTask.status == TaskStatus.RUNNING)
        ))
        if not running:
            return {"checked": 0, "reconciled": 0}
        r = _redis_client()
        now = time.time()
        stale_cutoff = now - _SLOT_TTL_SECONDS - 120  # TTL + 2 分钟缓冲
        for task in running:
            score = r.zscore(_CONCURRENCY_ZSET, task.id)
            # 槽位缺失(None) 或 心跳过期 → worker 已死，孤儿任务
            if score is None or score < stale_cutoff:
                # 竞态防护：仅当任务此刻仍为 RUNNING 才标 FAILED。
                # 从上面查询 running 列表到这里执行 update 之间存在时间窗口，
                # 用户可能恰好重试了该孤儿任务（RUNNING→QUEUED），或 worker 抢占
                # 置为其它状态；若无此条件会把 QUEUED 误覆盖成 FAILED，误杀重试。
                result = db.execute(
                    update(TranslationTask)
                    .where(
                        TranslationTask.id == task.id,
                        TranslationTask.status == TaskStatus.RUNNING,
                    )
                    .values(
                        status=TaskStatus.FAILED,
                        error_message="任务运行中断（服务器重启或 worker 异常退出），请点重试重新翻译。",
                    )
                )
                if result.rowcount != 1:
                    # 状态已在窗口内变化，跳过（不清槽位——留给新状态的属主管理）
                    logger.info("对账：任务 %s 状态已变化，跳过标记", task.id)
                    continue
                # 清理可能残留的槽位
                _release_concurrency_slot(task.id)
                reconciled += 1
                logger.warning("对账：孤儿任务 %s 标记为 FAILED（槽位 score=%s）", task.id, score)
        if reconciled:
            db.commit()
    finally:
        db.close()
    logger.info("对账完成：检查 %d 个 RUNNING，标记 %d 个孤儿为 FAILED", len(running), reconciled)
    return {"checked": len(running), "reconciled": reconciled}


class TaskCancelled(Exception):
    """任务已被用户删除/取消，抛出该异常使 Worker 立即退出当前任务。"""


def _check_task_alive(task_id: str) -> None:
    """查询数据库，若任务已 DELETED 则抛 TaskCancelled。

    用独立 Session 避免与主事务冲突；只读查询，无副作用。
    """
    s = SessionLocal()
    try:
        t = s.get(TranslationTask, task_id)
        if t is None or t.status == TaskStatus.DELETED:
            raise TaskCancelled(f"任务 {task_id} 已被删除/取消")
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
    import redis
    return redis.from_url(_settings.redis_url, decode_responses=True)


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


def _acquire_concurrency_slot(task_id: str, poll_interval: float = 2.0) -> None:
    """获取翻译许可，超出上限时阻塞等待。

    用 Redis ZSET + Lua 原子判断：清理 TTL 过期的僵尸条目后，在册数未满才放行。
    等待期间定期检查任务是否已被删除，避免对已删任务继续占用 worker。
    """
    try:
        r = _redis_client()
        acquire = r.register_script(_ACQUIRE_LUA)
        while True:
            # 任务在排队期间可能已被删除
            _check_task_alive(task_id)

            max_c = _get_max_concurrency()
            ok = acquire(
                keys=[_CONCURRENCY_ZSET],
                args=[time.time(), _SLOT_TTL_SECONDS, max_c, task_id],
            )
            if ok == 1:
                logger.info("任务 %s 获得并发槽位（上限 %d）", task_id, max_c)
                return
            logger.info("任务 %s 等待并发槽位（上限 %d）", task_id, max_c)
            time.sleep(poll_interval)
    except TaskCancelled:
        raise
    except Exception as exc:
        logger.warning("并发闸门异常，降级为直接放行：%s", exc)
        return


def _heartbeat_concurrency_slot(task_id: str) -> None:
    """刷新槽位心跳（任务执行中周期调用），避免长任务被 TTL 误回收。"""
    try:
        _redis_client().zadd(_CONCURRENCY_ZSET, {task_id: time.time()})
    except Exception:
        pass


def _release_concurrency_slot(task_id: str) -> None:
    """释放翻译许可。"""
    try:
        _redis_client().zrem(_CONCURRENCY_ZSET, task_id)
    except Exception as exc:
        logger.warning("释放并发槽位失败：%s", exc)


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


@celery_app.task(name="translation.run", bind=True, max_retries=0)
def run_translation_task(self, task_id: str) -> None:  # noqa: ARG001
    db = SessionLocal()
    slot_acquired = False
    try:
        task = db.get(TranslationTask, task_id)
        if task is None:
            return
        # 任务可能在排队期间已被删除
        if task.status == TaskStatus.DELETED:
            return

        # 应用层并发闸门：等待获取全局翻译许可
        # 在标记 RUNNING 之前等待，让用户看到的是"排队中"而非假进度
        _acquire_concurrency_slot(task_id)
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
            .values(status=TaskStatus.RUNNING, progress=10)
        )
        db.commit()
        if claimed.rowcount != 1:
            logger.info("任务 %s 状态已非 QUEUED（已删除或已被其它 worker 抢占），跳过", task_id)
            return
        db.refresh(task)

        source_bytes = download_bytes(task.source_object)

        # 段落级进度回调：把 [10%, 90%] 映射到实际段数；最少 1% 步进合并以减少写库
        # 注意：on_progress 会被多个翻译线程并发调用，SQLAlchemy Session 非线程安全，
        # 必须用锁串行化 DB 操作。
        last_written = {"v": 10}
        last_alive_check = {"t": 0.0}
        progress_lock = threading.Lock()

        def on_progress(done: int, total: int) -> None:
            # 存活检查 + 槽位心跳：节流到最多每 2s 一次。
            # 否则每完成一段都查一次库，高并发下会打满连接池。
            now = time.time()
            do_check = False
            with progress_lock:
                if now - last_alive_check["t"] >= 2.0:
                    last_alive_check["t"] = now
                    do_check = True
            if do_check:
                # 已删除则抛 TaskCancelled，让翻译循环立即退出
                _check_task_alive(task_id)
                # 刷新并发槽位心跳，避免长任务被 TTL 误回收
                _heartbeat_concurrency_slot(task_id)
            pct = 10 + int((done / max(total, 1)) * 80)
            pct = max(10, min(90, pct))
            with progress_lock:
                if pct - last_written["v"] < 1:
                    return
                last_written["v"] = pct
                task.progress = pct  # 更新主 Session 对象属性（不 commit）
            # 用独立 Session 写 DB，避免多线程共享主 Session 的 commit 问题
            # SQLAlchemy Session 非线程安全，多线程并发 commit 会导致状态不一致
            progress_db = SessionLocal()
            try:
                progress_db.execute(
                    text("UPDATE translation_tasks SET progress = :p WHERE id = :id"),
                    {"p": pct, "id": task_id}
                )
                progress_db.commit()
            except Exception:
                progress_db.rollback()
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

        task.progress = 90
        db.commit()

        # 上传译文前最后确认一次任务未被删除，缩小竞态窗口
        _check_task_alive(task_id)

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
            )
            .values(
                result_object=result_object,
                status=TaskStatus.SUCCEEDED,
                progress=100,
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
        # 条件更新：仅 QUEUED/RUNNING 可转 FAILED——已删除的任务保持 DELETED，
        # 已被其它流程收尾的任务也不会被覆写（检查+赋值的旧写法存在竞态）。
        try:
            db.execute(
                update(TranslationTask)
                .where(
                    TranslationTask.id == task_id,
                    TranslationTask.status.in_(
                        [TaskStatus.QUEUED, TaskStatus.RUNNING]
                    ),
                )
                .values(
                    status=TaskStatus.FAILED,
                    error_message=_humanize_error(exc)[:1000],
                )
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        # 不再 raise：max_retries=0 已禁用重试，且抛异常会导致日志混乱
        return
    finally:
        if slot_acquired:
            _release_concurrency_slot(task_id)
        db.close()
