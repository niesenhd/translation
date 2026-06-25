"""
清理术语库 source_term：
1. 去掉词性标注前缀（n. v. vt. vi. adj. adv. prep. conj. pron. art. abbr. num.）
2. 多重释义只保留第一个（按 ; 和 , 分隔）
3. 全角数字转半角（４８ → 48）
"""
import re
import sys
import unicodedata

# 词性前缀正则
POS_PATTERN = re.compile(
    r'^(n|v|vt|vi|adj|adv|prep|conj|pron|art|abbr|num|aux)\.',
    re.IGNORECASE
)

# 全角数字范围：U+FF10-U+FF19
FW_DIGIT_MAP = {str(i): chr(0xFF10 + i) for i in range(10)}
DIGIT_TRANS = str.maketrans({v: k for k, v in FW_DIGIT_MAP.items()})

def to_halfwidth_digits(s: str) -> str:
    """全角数字转半角"""
    return s.translate(DIGIT_TRANS)

def clean_term(raw: str) -> str:
    """清理单个术语"""
    s = raw.strip()
    if not s:
        return s

    # 按 ; 分割，只取第一段（去掉其他词性的释义）
    if ';' in s:
        s = s.split(';')[0].strip()

    # 去掉词性前缀（可能出现在开头，也可能出现在 ; 之后的第一段开头）
    s = POS_PATTERN.sub('', s).strip()

    # 按 " ," 或 "," 分割，只保留第一个释义
    if ',' in s:
        s = s.split(',')[0].strip()

    # 再清理一次前缀（有些是 "n.xxx" 去掉后还有残留）
    s = POS_PATTERN.sub('', s).strip()

    # 全角数字转半角
    s = to_halfwidth_digits(s)

    return s


def main():
    # 连接数据库
    sys.path.insert(0, '/app')
    from app.core.database import SessionLocal
    from app.models.term import TermEntry

    db = SessionLocal()
    try:
        total = db.query(TermEntry).count()
        print(f"术语库总数: {total}")

        # 分批处理，避免内存爆炸
        batch_size = 2000
        updated = 0
        skipped = 0

        all_ids = [r[0] for r in db.query(TermEntry.id).all()]
        print(f"需要检查: {len(all_ids)} 条")

        for i in range(0, len(all_ids), batch_size):
            batch_ids = all_ids[i:i+batch_size]
            entries = db.query(TermEntry).filter(TermEntry.id.in_(batch_ids)).all()

            for entry in entries:
                original = entry.source_term
                cleaned = clean_term(original)

                if cleaned != original:
                    if cleaned:  # 确保清理后不为空
                        entry.source_term = cleaned
                        updated += 1
                    else:
                        skipped += 1
                        print(f"  跳过（清理后为空）: id={entry.id} source={original!r}")
                else:
                    skipped += 1

            db.commit()
            print(f"  已处理 {min(i+batch_size, len(all_ids))}/{len(all_ids)}, 更新 {updated} 条")

        print(f"\n完成！共更新 {updated} 条，跳过 {skipped} 条")

    finally:
        db.close()


if __name__ == '__main__':
    main()
