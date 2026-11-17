REASONS = {
    "BAD_COLUMNS": "字段数量不符", "EMPTY_VALUE": "必要值为空",
    "DUPLICATE_CONFLICT": "同一业务键取值冲突", "TRUNCATED": "尾记录不完整",
    "INVALID_VALUE": "值不符合定义", "STALE_VERSION": "提交版本已过期",
    "UNKNOWN_CONTRACT": "报送契约版本未登记", "MISSING_BRANCH": "新版契约缺少分支机构",
    "MISSING_LEGAL_ENTITY": "新版契约缺少法人单位", "CONTENT_CONFLICT": "同一报送内容冲突",
    "RECEIPT_LOST": "回执丢失，待查询", "SEND_INTERRUPTED": "发送中断，可从已存状态恢复",
    "RECEIVER_UNAVAILABLE": "本地接收端不可用", "NOT_FOUND": "报送不存在",
}

def reason(code: str) -> str:
    return REASONS.get(code, "未登记原因")
