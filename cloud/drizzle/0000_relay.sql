CREATE TABLE relay_jobs (
  id TEXT PRIMARY KEY NOT NULL,
  owner TEXT NOT NULL,
  status TEXT NOT NULL,
  payload TEXT NOT NULL,
  response TEXT,
  claim TEXT,
  lease_until INTEGER,
  created INTEGER NOT NULL,
  expires INTEGER NOT NULL
);
--> statement-breakpoint
CREATE INDEX relay_jobs_queue ON relay_jobs(status,created);
--> statement-breakpoint
CREATE TABLE relay_health (id TEXT PRIMARY KEY NOT NULL,last_seen INTEGER NOT NULL);
