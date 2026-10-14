-- Pricing database for the GFE responder.
--
-- The shape intentionally mirrors what a provider billing system or a price-transparency
-- data company already holds:
--   * negotiated_rates   ~ payer Transparency-in-Coverage (TiC) in-network files, normalized
--   * standard_charges   ~ hospital price transparency (45 CFR 180) standard charges files
--   * service_components ~ the lines billed for an orderable service, by role (facility, radiologist...)
--   * role_taxonomies    ~ which provider taxonomies bill a role, so each provider prices only its own lines

CREATE TABLE IF NOT EXISTS payers (
    payer_id      TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    etin          TEXT,              -- Electronic Transmitter ID (what the PCT IG uses on payer Organizations)
    naic          TEXT,
    phone         TEXT,
    address_line  TEXT,
    city          TEXT,
    state         TEXT,
    postal_code   TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id               TEXT PRIMARY KEY,
    payer_id              TEXT NOT NULL REFERENCES payers(payer_id),
    plan_name             TEXT NOT NULL,
    coverage_class_value  TEXT,      -- matched against Coverage.class[type=plan].value
    plan_type             TEXT,      -- PPO / HMO / EPO ...
    network_name          TEXT,
    price_network_id      TEXT       -- the price source's id for this network (e.g. Serif Neuron network_template_id)
);

CREATE TABLE IF NOT EXISTS providers (
    npi               TEXT PRIMARY KEY,
    entity_type       TEXT NOT NULL CHECK (entity_type IN ('organization', 'practitioner')),
    name              TEXT NOT NULL,
    given_name        TEXT,
    family_name       TEXT,
    tin               TEXT,
    taxonomy_code     TEXT NOT NULL,
    taxonomy_display  TEXT,
    billing_class     TEXT NOT NULL CHECK (billing_class IN ('institutional', 'professional')),
    place_of_service  TEXT,          -- CMS POS code used on professional lines
    phone             TEXT,
    address_line      TEXT,
    city              TEXT,
    state             TEXT,
    postal_code       TEXT
);

-- Which taxonomies can fill a care-team role named in service_components.role
CREATE TABLE IF NOT EXISTS role_taxonomies (
    role           TEXT NOT NULL,
    taxonomy_code  TEXT NOT NULL,
    PRIMARY KEY (role, taxonomy_code)
);

CREATE TABLE IF NOT EXISTS code_descriptions (
    code_type  TEXT NOT NULL,        -- CPT | HCPCS | RC | ICD10CM | POS | MS-DRG
    code       TEXT NOT NULL,
    display    TEXT NOT NULL,
    PRIMARY KEY (code_type, code)
);

CREATE TABLE IF NOT EXISTS service_components (
    primary_code_type  TEXT NOT NULL,
    primary_code       TEXT NOT NULL,
    seq                INTEGER NOT NULL,
    role               TEXT NOT NULL,   -- facility | radiologist | surgeon | anesthesiologist | ...
    code_type          TEXT NOT NULL,
    code               TEXT NOT NULL,
    modifier           TEXT NOT NULL DEFAULT '',
    revenue_code       TEXT,
    quantity           REAL NOT NULL DEFAULT 1,
    service_category   TEXT NOT NULL,   -- becomes Claim.item serviceDescription
    PRIMARY KEY (primary_code_type, primary_code, seq)
);

CREATE TABLE IF NOT EXISTS negotiated_rates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    payer_id         TEXT NOT NULL REFERENCES payers(payer_id),
    plan_id          TEXT REFERENCES plans(plan_id),   -- NULL = applies to every plan of the payer
    npi              TEXT NOT NULL REFERENCES providers(npi),
    tin              TEXT,
    billing_class    TEXT NOT NULL CHECK (billing_class IN ('institutional', 'professional')),
    code_type        TEXT NOT NULL,
    code             TEXT NOT NULL,
    modifier         TEXT NOT NULL DEFAULT '',
    negotiated_type  TEXT NOT NULL CHECK (negotiated_type IN ('negotiated', 'fee schedule', 'percentage', 'per diem')),
    negotiated_rate  REAL NOT NULL,                    -- dollars, or percent of gross charge for 'percentage'
    effective_date   TEXT NOT NULL,
    expiration_date  TEXT NOT NULL,
    source           TEXT NOT NULL                     -- provenance (file / feed name)
);

CREATE INDEX IF NOT EXISTS ix_rates_lookup
    ON negotiated_rates (npi, payer_id, code_type, code, modifier, billing_class);

CREATE TABLE IF NOT EXISTS standard_charges (
    npi              TEXT NOT NULL REFERENCES providers(npi),
    code_type        TEXT NOT NULL,
    code             TEXT NOT NULL,
    modifier         TEXT NOT NULL DEFAULT '',
    gross_charge     REAL,
    discounted_cash  REAL,
    source           TEXT NOT NULL,
    PRIMARY KEY (npi, code_type, code, modifier)
);
