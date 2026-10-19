-- PROPOSED, NOT RUN. For Nitin to review (PROP-1622).
-- Tables for when the client-base engine moves into the app: the agent report's "what happened since" panel,
-- the On-Market Activity rebuild, and the watch list. Columns mirror CONTRACT.md (schema 1.0).
-- Schema name is a placeholder: EnterpriseDemo is where the agent report keeps its job tables today.

CREATE TABLE EnterpriseDemo.client_base_run (
    run_id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    org               VARCHAR(100)  NOT NULL,
    group_name        VARCHAR(150)  NOT NULL,
    schema_version    VARCHAR(10)   NOT NULL,
    run_date          DATE          NOT NULL,
    params_json       JSON          NOT NULL,
    summary_json      JSON          NULL,          -- the whole summary.json, for the head-office view
    seconds           DECIMAL(8,1)  NULL,
    queries           INT           NULL,
    status            VARCHAR(20)   NOT NULL DEFAULT 'completed',
    created_at        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id),
    KEY idx_cbr_org_date (org, run_date)
);

CREATE TABLE EnterpriseDemo.client_base_branch (
    run_id            BIGINT UNSIGNED NOT NULL,
    brand             VARCHAR(150)  NOT NULL,
    branch            VARCHAR(150)  NOT NULL,
    agent_master_ids  JSON          NOT NULL,      -- pdi_agent_master ids resolved for this branch
    rightmove_pairs   JSON          NOT NULL,
    zoopla_pairs      JSON          NOT NULL,
    book_properties   INT           NOT NULL,
    totals_json       JSON          NOT NULL,      -- summary.json brands[].branches[].totals
    PRIMARY KEY (run_id, brand, branch)
);

-- The book = the watch list: one row per (branch, property), with its current state event.
CREATE TABLE EnterpriseDemo.client_base_book (
    run_id            BIGINT UNSIGNED NOT NULL,
    brand             VARCHAR(150)  NOT NULL,
    branch            VARCHAR(150)  NOT NULL,
    property_key      VARCHAR(120)  NOT NULL,
    uprn              VARCHAR(20)   NULL,
    address           VARCHAR(255)  NOT NULL,
    postcode          VARCHAR(10)   NOT NULL,
    outcode           VARCHAR(4)    NOT NULL,
    match_basis       VARCHAR(30)   NOT NULL,
    branch_listings   INT           NOT NULL,
    branch_last_type  VARCHAR(4)    NOT NULL,
    branch_last_date  DATE          NULL,
    branch_last_price INT           NULL,
    branch_last_status VARCHAR(60)  NULL,
    branch_last_portal VARCHAR(10)  NOT NULL,
    state_event       VARCHAR(40)   NULL,
    PRIMARY KEY (run_id, brand, branch, property_key),
    KEY idx_cbb_uprn (uprn),
    KEY idx_cbb_postcode (postcode)
);

CREATE TABLE EnterpriseDemo.client_base_event (
    run_id            BIGINT UNSIGNED NOT NULL,
    event_id          INT UNSIGNED  NOT NULL,      -- row number within the run
    brand             VARCHAR(150)  NOT NULL,
    branch            VARCHAR(150)  NOT NULL,
    property_key      VARCHAR(120)  NOT NULL,
    uprn              VARCHAR(20)   NULL,
    address           VARCHAR(255)  NOT NULL,
    postcode          VARCHAR(10)   NOT NULL,
    outcode           VARCHAR(4)    NOT NULL,
    match_basis       VARCHAR(30)   NOT NULL,
    branch_last_type  VARCHAR(4)    NOT NULL,
    branch_last_date  DATE          NULL,
    branch_last_price INT           NULL,
    branch_last_status VARCHAR(60)  NULL,
    branch_last_portal VARCHAR(10)  NOT NULL,
    event             ENUM('with_you_now','relisted_with_competitor_sale','relisted_with_competitor_let',
                           'withdrawn','price_cut','sold','relet_by_anyone','renewal_due') NOT NULL,
    event_date        DATE          NULL,
    detail            VARCHAR(400)  NULL,
    current_agent     VARCHAR(255)  NULL,
    current_agent_relation ENUM('branch','group','sister_brand','competitor','unknown') NOT NULL,
    current_price     INT           NULL,
    price_type        VARCHAR(12)   NOT NULL,
    portal            VARCHAR(10)   NULL,
    url               VARCHAR(500)  NULL,
    event_address     VARCHAR(255)  NULL,
    event_uprn        VARCHAR(20)   NULL,
    gap_months        DECIMAL(6,1)  NULL,
    est_fee           DECIMAL(12,2) NOT NULL DEFAULT 0,
    fee_basis         VARCHAR(4)    NOT NULL,
    value_used        DECIMAL(12,2) NULL,
    value_basis       VARCHAR(30)   NOT NULL,
    confidence        ENUM('own_listing','uprn','address') NOT NULL,
    PRIMARY KEY (run_id, event_id),
    KEY idx_cbe_branch_event (run_id, brand, branch, event),
    KEY idx_cbe_property (property_key),
    KEY idx_cbe_uprn (uprn)
);
