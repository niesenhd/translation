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
from sqlalchemy import text

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
    },
)


@celery_app.task(name="translation.cleanup_expired_files")
def cleanup_expired_files_task() -> dict:
    """Celery Beat 定时任务：清理过期文件（覆写 + 删除）。"""
    return cleanup_expired_files().as_dict()


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


# ====== 应用层并发闸门 ======
# 通过 Redis 原子计数器实现，使管理员在后台调整 max_concurrency 能即时生效。
# Celery worker 的 --concurrency 只控制"同时执行的任务数"（进程/线程槽位），
# 而本闸门在此基础上再加一层"全局翻译许可"，确保即使 worker 槽位较多，
# 实际并发翻译的文件数也不会超过管理员设定的上限。

_CONCURRENCY_KEY = "translation:running_count"


# Worker 启动时重置并发计数器，避免上次异常退出导致计数器泄漏（槽位被占满但无人释放）
@celery_app.on_after_configure.connect
def _reset_concurrency_on_start(sender, **kwargs):
    """worker 启动时把 running_count 归零。

    worker 重启意味着所有正在执行的任务已经丢失（不会有 release 调用），
    所以计数器必然是脏的，直接清零是最安全的做法。
    """
    try:
        import redis as _redis
        r = _redis.from_url(_settings.redis_url, decode_responses=True)
        r.set(_CONCURRENCY_KEY, 0)
        logger.info("Worker 启动：并发计数器已重置为 0")
    except Exception as exc:
        logger.warning("Worker 启动：重置并发计数器失败（降级忽略）：%s", exc)


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

    使用 Redis INCR 原子自增，拿到超过上限的号会被立即 DECR 退回并等待重试。
    在等待期间定期检查任务是否已被删除，避免对已删任务继续占用 worker。
    """
    import redis

    r = redis.from_url(_settings.redis_url, decode_responses=True)
    try:
        while True:
            # 任务在排队期间可能已被删除
            _check_task_alive(task_id)

            max_c = _get_max_concurrency()
            current = r.incr(_CONCURRENCY_KEY)
            if current <= max_c:
                logger.info("任务 %s 获得并发槽位（当前 %d/%d）", task_id, current, max_c)
                return
            # 超限：退回计数并等待
            r.decr(_CONCURRENCY_KEY)
            logger.info("任务 %s 等待并发槽位（当前 %d，上限 %d）", task_id, current - 1, max_c)
            time.sleep(poll_interval)
    except TaskCancelled:
        raise
    except Exception as exc:
        logger.warning("并发闸门异常，降级为直接放行：%s", exc)
        return


def _release_concurrency_slot() -> None:
    """释放翻译许可。"""
    import redis

    try:
        r = redis.from_url(_settings.redis_url, decode_responses=True)
        # DECR 但不低于 0
        current = r.decr(_CONCURRENCY_KEY)
        if current is not None and current < 0:
            r.set(_CONCURRENCY_KEY, 0)
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

        task.status = TaskStatus.RUNNING
        task.progress = 10
        db.commit()

        source_bytes = download_bytes(task.source_object)

        # 段落级进度回调：把 [10%, 90%] 映射到实际段数；最少 1% 步进合并以减少写库
        # 注意：on_progress 会被多个翻译线程并发调用，SQLAlchemy Session 非线程安全，
        # 必须用锁串行化 DB 操作。
        last_written = {"v": 10}
        progress_lock = threading.Lock()

        def on_progress(done: int, total: int) -> None:
            # 任务已被删除/取消 -> 抛 TaskCancelled 让翻译循环立即退出
            _check_task_alive(task_id)
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

        ctx = TranslationContext(
            target_lang=task.target_lang,
            source_lang=task.source_lang,
            output_mode=task.output_mode,
            translate_images=task.translate_images.value,
            on_progress=on_progress,
            glossary=glossary,
            tm_lookup=lookup_tm,
        )
        translator = get_translator()

        result_bytes, out_ext = translate_file(
            task.file_ext,
            source_bytes,
            translator,
            ctx,
            pdf_output_format=task.pdf_output_format.value,
        )

        task.progress = 90
        db.commit()

        result_object = f"results/{task.id}.{out_ext}"
        upload_bytes(result_object, result_bytes)

        task.result_object = result_object
        task.status = TaskStatus.SUCCEEDED
        task.progress = 100
        db.commit()
    except TaskCancelled as exc:
        # 任务已删除：不修改状态（保持 DELETED），不重试，安静退出
        try:
            db.rollback()
        except Exception:
            pass
        return
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        task = db.get(TranslationTask, task_id)
        if task is not None:
            # 若任务在执行期间被删除，不要把状态改成 FAILED
            if task.status == TaskStatus.DELETED:
                return
            task.status = TaskStatus.FAILED
            task.error_message = _humanize_error(exc)[:1000]
            db.commit()
        # 不再 raise：max_retries=0 已禁用重试，且抛异常会导致日志混乱
        return
    finally:
        if slot_acquired:
            _release_concurrency_slot()
        db.close()
