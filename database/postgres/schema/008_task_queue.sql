-- =====================================================
-- PAMS Task Queue - Telegram ↔ Claude Code Bridge
-- 008_task_queue.sql
-- =====================================================

CREATE TABLE IF NOT EXISTS pams_task_queue (
    id          BIGSERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL,
    message_id  BIGINT,
    status      VARCHAR(20) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    request     TEXT NOT NULL,
    response    TEXT,
    page_links  JSONB DEFAULT '[]'::jsonb,
    created_at  TIMESTAMP DEFAULT NOW(),
    updated_at  TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_task_queue_status ON pams_task_queue(status);
CREATE INDEX IF NOT EXISTS idx_task_queue_created ON pams_task_queue(created_at DESC);

-- Reuse existing trigger function from 005_transactions.sql
CREATE TRIGGER update_pams_task_queue_updated_at BEFORE UPDATE
    ON pams_task_queue FOR EACH ROW
    EXECUTE PROCEDURE update_updated_at_column();
