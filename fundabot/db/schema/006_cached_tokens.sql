ALTER TABLE model_calls ADD COLUMN cached_input_tokens int NOT NULL DEFAULT 0;
ALTER TABLE model_calls ADD COLUMN cache_write_tokens int NOT NULL DEFAULT 0;
