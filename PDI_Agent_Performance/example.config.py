# PDI database
HOST = "HOST"
USER = "USER"
PASSWORD = "PASSWORD"
DATABASE = "PDI_PortalsData"
PORT = 3306
POOL_SIZE = 5

# Target table for reads/schema copy. Writes go to a timestamped staging table.
MAIN_PERFORMANCE_TABLE = "pdi_agent_performance"

# When True, atomically swap staging table into main on a successful full run:
#   pdi_agent_performance      -> pdi_agent_performance_bkp
#   pdi_agent_performance_<ts> -> pdi_agent_performance
ATOMIC_REPLACE_MAIN_TABLE = False


# Email notification sent after every run when enabled
EnableMailNotification = False
MailTO = [
    "your@email.com",
]
MAIL_FROM = "notify@example.com"
MAIL_HOST = "smtp.example.com"
MAIL_PORT = 465
MAIL_USER = "notify@example.com"
MAIL_PASS = "your-smtp-password"
MAIL_SSL = True   # Use SMTP_SSL (recommended for port 465)
MAIL_TLS = False  # Use STARTTLS (recommended for port 587)