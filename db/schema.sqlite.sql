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

CREATE TABLE IF NOT EXISTS keyword_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    title TEXT NOT NULL,
    company_name TEXT,
    location TEXT,
    remote INTEGER,
    salary TEXT,
    description TEXT,
    job_url TEXT,
    source_url TEXT,
    posted_date TEXT,
    scraped_at TEXT DEFAULT (datetime('now')),
    UNIQUE(job_url)
);

CREATE TABLE IF NOT EXISTS resume_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    candidate_name TEXT,
    base_resume_id TEXT NOT NULL,
    base_resume_name TEXT,
    target_roles TEXT,           -- JSON array
    work_authorization TEXT,
    visa_status TEXT,
    verified_skills TEXT,        -- JSON array
    location_preference TEXT,
    open_to_relocation INTEGER,
    keywords TEXT,               -- JSON array, active (non-dismissed) keywords
    additional_rules TEXT,       -- free-text hard-gate rules
    review_status TEXT,
    generation_status TEXT,
    is_match_ready INTEGER DEFAULT 0,  -- approved + current profile_version per masterprompt rule 4
    is_test_account INTEGER DEFAULT 0, -- masterprompt s.5: excluded from production runs
    synced_at TEXT DEFAULT (datetime('now')),
    location_gate TEXT,          -- app/filters.py GATES key; machine-enforced, not advisory
    years_experience REAL,       -- computed from resume content.experience[]; app/experience.py
    UNIQUE(base_resume_id)
);

CREATE TABLE IF NOT EXISTS resume_job_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_profile_id INTEGER REFERENCES resume_profiles(id) ON DELETE CASCADE,
    keyword_job_id INTEGER REFERENCES keyword_jobs(id) ON DELETE CASCADE,
    score INTEGER,
    band TEXT,                   -- TOP_MATCH | REVIEWABLE_MATCH
    reason TEXT,
    matched_terms TEXT,          -- JSON array
    hard_gate_results TEXT,      -- JSON array
    run_id TEXT,
    matched_at TEXT DEFAULT (datetime('now')),
    UNIQUE(resume_profile_id, keyword_job_id)
);

CREATE TABLE IF NOT EXISTS match_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT,
    profiles_processed INTEGER DEFAULT 0,
    jobs_considered INTEGER DEFAULT 0,
    matches_created INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_resume_matches_profile ON resume_job_matches(resume_profile_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_keyword_jobs_keyword ON keyword_jobs(keyword);
CREATE INDEX IF NOT EXISTS idx_keyword_jobs_posted_date ON keyword_jobs(posted_date);

CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_jobs_posted_date ON jobs(posted_date);
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(scrape_status);
