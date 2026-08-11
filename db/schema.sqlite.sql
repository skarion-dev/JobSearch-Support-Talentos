CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT,
    name TEXT NOT NULL,
    website TEXT,
    careers_url TEXT,
    industry TEXT,
    location TEXT,
    last_scraped_at TEXT,
    scrape_status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    location TEXT,
    remote INTEGER,
    salary TEXT,
    description TEXT,
    job_url TEXT,
    posted_date TEXT,
    scraped_at TEXT DEFAULT (datetime('now')),
    UNIQUE(company_id, job_url)
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    status TEXT,
    jobs_found INTEGER DEFAULT 0,
    error TEXT,
    started_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS scrape_methods (
    company_id INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    method_type TEXT NOT NULL,       -- 'greenhouse' | 'lever' | 'workday' | 'css' | 'ai_only'
    method_config TEXT,               -- JSON: board token / css selectors / etc
    figured_out_at TEXT DEFAULT (datetime('now')),
    last_success_at TEXT,
    consecutive_failures INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT,
    companies_attempted INTEGER DEFAULT 0,
    companies_deterministic INTEGER DEFAULT 0,
    companies_ai INTEGER DEFAULT 0,
    companies_failed INTEGER DEFAULT 0,
    jobs_found INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_jobs_posted_date ON jobs(posted_date);
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(scrape_status);
