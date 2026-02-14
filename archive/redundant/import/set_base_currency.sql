UPDATE pams_policy
SET value = value || '{"base_currency":"CNY"}'::jsonb,
    updated_at = now()
WHERE key = 'ips_v1';
