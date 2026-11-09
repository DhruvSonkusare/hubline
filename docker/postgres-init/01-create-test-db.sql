-- Runs once when the local Postgres volume is first created.
CREATE DATABASE hubline_test OWNER hubline;
CREATE DATABASE hubline_e2e OWNER hubline;  -- Playwright (frontend/tests/e2e), reset each run
