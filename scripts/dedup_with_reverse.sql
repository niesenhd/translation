-- 术语库去重 + 反向转换
-- 策略：
--   1. 每个 zh→en 的 source_term 保留一条最优条目
--   2. 其余同义词（rn > 1）转为 en→zh 反向记录
--   3. 每个 en→zh 方向也只保留一条（避免英文同义时再次堆叠）
-- 顺序：先 INSERT（从待删除行提取反向），再 DELETE

BEGIN;

-- 步骤1：从 zh→en 的"将被删除"条目中，提取反向 en→zh 记录
-- 用 CTE 先算好排名，rn>1 的就是要转换的
-- 反向时：同一个英文 target_term 可能对应多个中文 source_term，
-- 取最短中文（最通用），其余丢弃
WITH ranked AS (
    SELECT id, source_term, target_term, domain, priority, note,
           ROW_NUMBER() OVER (
               PARTITION BY source_term, lang_pair
               ORDER BY
                   CASE WHEN domain = '法学' THEN 1 ELSE 0 END,
                   CASE WHEN priority = 'strict' THEN 0 ELSE 1 END,
                   LENGTH(target_term),
                   updated_at DESC
           ) as rn
    FROM term_entries
    WHERE lang_pair = 'zh→en'
),
-- rn > 1 的条目：target(英文) → source(中文) 反转
reverse_candidates AS (
    SELECT
        target_term AS en_term,   -- 英文术语
        source_term AS zh_term,   -- 对应中文
        domain,
        priority,
        note
    FROM ranked
    WHERE rn > 1
),
-- 每个英文术语只取一个中文译法（取最短中文，最通用）
reverse_deduped AS (
    SELECT DISTINCT ON (en_term)
        en_term,
        zh_term,
        domain,
        priority,
        note
    FROM reverse_candidates
    ORDER BY en_term, LENGTH(zh_term), priority
)
INSERT INTO term_entries (id, source_term, target_term, lang_pair, domain, priority, note, created_at, updated_at)
SELECT
    gen_random_uuid()::varchar(36),
    en_term,
    zh_term,
    'en→zh',
    domain,
    priority,
    COALESCE(note, '') || ' [反向：原 zh→en 同义词]',
    NOW(),
    NOW()
FROM reverse_deduped
-- 不覆盖已存在的 en→zh 条目（当前为 0，但保险起见保留）
WHERE NOT EXISTS (
    SELECT 1 FROM term_entries e
    WHERE e.lang_pair = 'en→zh' AND e.source_term = reverse_deduped.en_term
);

-- 步骤2：删除 zh→en 中多余的同义词条目（已被转为反向的）
WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY source_term, lang_pair
               ORDER BY
                   CASE WHEN domain = '法学' THEN 1 ELSE 0 END,
                   CASE WHEN priority = 'strict' THEN 0 ELSE 1 END,
                   LENGTH(target_term),
                   updated_at DESC
           ) as rn
    FROM term_entries
    WHERE lang_pair = 'zh→en'
)
DELETE FROM term_entries
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

COMMIT;
