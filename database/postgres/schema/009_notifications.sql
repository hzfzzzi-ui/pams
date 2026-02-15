-- 推送通知表：供 Bot 每日推送和外部 Agent 发送消息
CREATE TABLE IF NOT EXISTS pams_notifications (
    id           BIGSERIAL PRIMARY KEY,
    chat_id      BIGINT NOT NULL,
    title        VARCHAR(200),
    body         TEXT NOT NULL,
    source       VARCHAR(50) NOT NULL DEFAULT 'system',
    status       VARCHAR(20) NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'sent', 'failed')),
    sent_at      TIMESTAMP,
    created_at   TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notif_pending
    ON pams_notifications(status) WHERE status = 'pending';
