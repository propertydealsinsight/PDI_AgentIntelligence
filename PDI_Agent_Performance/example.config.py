# Copy this file to config.py and fill in real values. config.py is gitignored.

# PDI database
HOST = "your-mysql-host"
USER = "your-user"
PASSWORD = "your-password"
DATABASE = "PDI_PortalsData"
PORT = 3306
POOL_SIZE = 5

# Target table for reads/schema copy. Writes go to a timestamped staging table.
# NOTE: staging is CREATE TABLE ... LIKE this table, so it must carry BOTH unique
# keys (agent_master_id AND agent_name+agent_address) — the name/addr one is what
# resolves master-row collisions under the COALESCE(zl, rm) naming. In production
# this must be "pdi_agent_performance". See docs/agent_performance_design_and_decisions.md.
MAIN_PERFORMANCE_TABLE = "pdi_agent_performance"

# When True, atomically swap staging table into main on a successful full run:
#   pdi_agent_performance      -> pdi_agent_performance_bkp   (previous bkp is DROPPED)
#   pdi_agent_performance_<ts> -> pdi_agent_performance
ATOMIC_REPLACE_MAIN_TABLE = True

# Validation harness overrides — LEAVE EMPTY for normal operation.
# Defaults are automatic: target = live pdi_agent_performance,
# baseline = pdi_agent_performance_bkp (maintained by the atomic swap).
VALIDATION_TARGET_TABLE = ""
VALIDATION_BASELINE_TABLE = ""

# Pre-swap validation (runs INSIDE the job, against the staging table, before
# the atomic swap; the swap is blocked if RED agents exceed the threshold).
# PRESWAP_VALIDATION_SAMPLE = 0 disables it (structural gate still runs).
PRESWAP_VALIDATION_SAMPLE = 40
PRESWAP_VALIDATION_MAX_RED = 4

# Email notification sent after every run when enabled
EnableMailNotification = False

# Mail config
MailTO = ["you@example.com"]
MAIL_FROM = "batchjobs@example.com"
MAIL_HOST = "smtp.example.com"
MAIL_PORT = 465
MAIL_USER = "batchjobs@example.com"
MAIL_PASS = "your-smtp-password"
MAIL_SSL = True
MAIL_TLS = True
