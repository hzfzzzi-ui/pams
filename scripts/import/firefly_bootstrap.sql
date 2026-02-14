-- role
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'firefly') THEN
    CREATE ROLE firefly LOGIN PASSWORD 'IKWeyQYUDMBHO6-BwUKfMbN43--g8EBk';
  END IF;
END$$;

-- database
SELECT format('CREATE DATABASE %I OWNER %I', 'firefly', 'firefly')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'firefly')\gexec
