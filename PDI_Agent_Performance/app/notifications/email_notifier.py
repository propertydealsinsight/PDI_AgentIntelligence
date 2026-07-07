"""HTML email notifications for agent performance job runs."""

from __future__ import annotations

import html
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from app.models.report import JobReport

logger = logging.getLogger(__name__)

EMAIL_SAMPLE_ROWS = 20


def _esc(value: Any) -> str:
    if value is None:
        return "—"
    return html.escape(str(value))


def _fmt_num(value: Any) -> str:
    if value is None:
        return "—"
    return _esc(value)


def _status_color(status: str) -> str:
    if status in {"SUCCESS", "DRY RUN", "COMPLETED (ALL SKIPPED)"}:
        return "#198754"
    if status in {"PARTIAL", "INTERRUPTED"}:
        return "#fd7e14"
    return "#dc3545"


def _render_records_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "<p><em>No records.</em></p>"

    head_html = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{cell}</td>" for cell in row)
        body_rows.append(f"<tr>{cells}</tr>")

    return f"""
    <table>
      <thead><tr>{head_html}</tr></thead>
      <tbody>{"".join(body_rows)}</tbody>
    </table>
    """


def _sample_note(total: int, shown: int, label: str) -> str:
    if total <= shown:
        return ""
    return (
        f"<p><em>Showing {shown} of {total:,} {label}. "
        f"See the staging table or log file for the full list.</em></p>"
    )


def build_html_report(report: JobReport) -> str:
    status = report.status_label
    color = _status_color(status)

    skipped_rows = [
        [
            _fmt_num(r.agent_id),
            _esc(r.agent_name),
            _esc(r.agent_address),
            _esc(r.reason),
            _fmt_num(r.no_of_listings),
        ]
        for r in report.skipped_sample[:EMAIL_SAMPLE_ROWS]
    ]

    failure_rows = [
        [
            _fmt_num(r.agent_id),
            _esc(r.agent_name),
            _esc(r.agent_address),
            _esc(r.error),
        ]
        for r in report.failures[:EMAIL_SAMPLE_ROWS]
    ]

    db_sample_rows = [
        [
            _fmt_num(r.get("agent_master_id")),
            _esc(r.get("agent_name")),
            _esc(r.get("agent_address")),
            _fmt_num(r.get("no_of_listings")),
            _fmt_num(r.get("no_of_live_listings")),
            _fmt_num(r.get("no_of_sold_listings")),
            _fmt_num(r.get("no_of_withdrawn_listing")),
            _fmt_num(r.get("avg_difference_in_percentage")),
            _fmt_num(r.get("turnaround_days")),
        ]
        for r in report.staging_db_sample[:EMAIL_SAMPLE_ROWS]
    ]

    db_summary = report.staging_db_summary or {}
    staging_name = report.staging_table or "—"
    swap_message = report.atomic_swap_message or (
        "Atomic swap performed." if report.atomic_swap_performed else "No atomic swap."
    )

    upserted_total = report.upserted_count or db_summary.get("row_count") or 0

    db_verify_block = ""
    if db_summary:
        db_verify_block = f"""
        <h2>Inserted Data Verification (from staging table)</h2>
        <div class="summary-grid">
          <div class="card"><strong>Rows inserted</strong><br>{_fmt_num(db_summary.get("row_count"))}</div>
          <div class="card"><strong>Total listings</strong><br>{_fmt_num(db_summary.get("total_listings"))}</div>
          <div class="card"><strong>Total live</strong><br>{_fmt_num(db_summary.get("total_live"))}</div>
          <div class="card"><strong>Total sold</strong><br>{_fmt_num(db_summary.get("total_sold"))}</div>
          <div class="card"><strong>Total withdrawn</strong><br>{_fmt_num(db_summary.get("total_withdrawn"))}</div>
          <div class="card"><strong>Avg price diff %</strong><br>{_fmt_num(db_summary.get("avg_price_diff"))}</div>
          <div class="card"><strong>Avg turnaround days</strong><br>{_fmt_num(db_summary.get("avg_turnaround"))}</div>
        </div>
        <p>Sample of top agents by listing count (for spot-checking):</p>
        {_sample_note(int(db_summary.get("row_count") or 0), min(len(report.staging_db_sample), EMAIL_SAMPLE_ROWS), "inserted rows")}
        {_render_records_table(
            [
                "Agent ID", "Name", "Address", "Listings", "Live",
                "Sold", "Withdrawn", "Avg Price Diff %", "Turnaround Days",
            ],
            db_sample_rows,
        )}
        """
    elif not report.dry_run:
        db_verify_block = """
        <h2>Inserted Data Verification</h2>
        <p><em>No staging table data available. Check logs for details.</em></p>
        """

    dry_run_block = ""
    if report.dry_run:
        dry_run_block = f"""
        <h2>Dry Run</h2>
        <p>Computed stats for <strong>{report.dry_run_count:,}</strong> agent(s). No data was written.</p>
        <p class="meta">Run without --dry-run to insert into a staging table.</p>
        """

    interrupt_block = ""
    if report.interrupted:
        interrupt_block = f"""
        <div class="interrupted">
          <strong>Process interrupted by user (Ctrl+C).</strong>
          {_esc(report.interrupt_message or "The job was stopped before completion.")}
          Processed {report.processed_count:,} of {report.summary.total:,} agents before shutdown.
          Partial results are below. Atomic table swap was not performed.
        </div>
        """

    processed_card = ""
    if report.interrupted:
        processed_card = (
            f'<div class="card"><strong>Processed before stop</strong><br>'
            f"{report.processed_count:,} / {report.summary.total:,}</div>"
        )

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; color: #212529; line-height: 1.45; }}
    h1, h2 {{ margin-bottom: 0.4em; }}
    .status {{ display: inline-block; padding: 6px 12px; border-radius: 4px; color: #fff;
               background: {color}; font-weight: bold; }}
    .interrupted {{ background: #fff3cd; border-left: 4px solid #fd7e14; padding: 12px 14px;
                    margin: 12px 0 18px; }}
    .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
                     gap: 10px; margin: 16px 0; }}
    .card {{ background: #f8f9fa; border: 1px solid #dee2e6; border-radius: 6px; padding: 10px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 10px 0 20px; font-size: 13px; }}
    th, td {{ border: 1px solid #dee2e6; padding: 6px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #e9ecef; }}
    tr:nth-child(even) {{ background: #f8f9fa; }}
    .errors {{ background: #fff5f5; border-left: 4px solid #dc3545; padding: 10px 12px; }}
    .meta {{ color: #6c757d; font-size: 13px; }}
  </style>
</head>
<body>
  <h1>PDI Agent Performance Job Report</h1>
  <p><span class="status">{_esc(status)}</span></p>

  {interrupt_block}

  <h2>Run Summary</h2>
  <div class="summary-grid">
    <div class="card"><strong>Started</strong><br>{_esc(report.started_at.strftime("%Y-%m-%d %H:%M:%S"))}</div>
    <div class="card"><strong>Finished</strong><br>{_esc(report.finished_at.strftime("%Y-%m-%d %H:%M:%S") if report.finished_at else "—")}</div>
    <div class="card"><strong>Elapsed</strong><br>{report.elapsed_seconds:.2f}s</div>
    {processed_card}
    <div class="card"><strong>Database</strong><br>{_esc(report.database)}</div>
    <div class="card"><strong>Main table</strong><br>{_esc(report.main_table)}</div>
    <div class="card"><strong>Staging table</strong><br>{_esc(staging_name)}</div>
    <div class="card"><strong>Total agents</strong><br>{report.summary.total:,}</div>
    <div class="card"><strong>Upserted</strong><br>{upserted_total:,}</div>
    <div class="card"><strong>Skipped</strong><br>{report.summary.skipped:,}</div>
    <div class="card"><strong>Failed</strong><br>{report.summary.failed:,}</div>
    <div class="card"><strong>Dry run</strong><br>{"Yes" if report.dry_run else "No"}</div>
    <div class="card"><strong>Atomic replace</strong><br>{"Enabled" if report.atomic_replace_enabled else "Disabled"}</div>
  </div>
  <p class="meta">{_esc(swap_message)}</p>
  <p class="meta">Log file: {_esc(report.log_file or "—")}</p>
  <p class="meta">Full per-agent details are in the log file and staging table — this email shows summary totals and small samples only.</p>

  {dry_run_block}
  {db_verify_block}

  <h2>Skipped Agents ({report.summary.skipped:,})</h2>
  {_sample_note(report.summary.skipped, len(skipped_rows), "skipped agents")}
  {_render_records_table(["Agent ID", "Name", "Address", "Reason", "Listings"], skipped_rows) if report.summary.skipped else "<p><em>No agents skipped.</em></p>"}

  <h2>Errors ({report.summary.failed:,})</h2>
  {"<div class='errors'>" + _sample_note(report.summary.failed, len(failure_rows), "errors") + _render_records_table(["Agent ID", "Name", "Address", "Error"], failure_rows) + "</div>"
   if report.summary.failed else "<p><em>No errors.</em></p>"}
</body>
</html>"""


def send_job_report_email(config_module: Any, report: JobReport) -> None:
    if not getattr(config_module, "EnableMailNotification", False):
        logger.info("Email notification disabled in config.")
        return

    recipients = getattr(config_module, "MailTO", [])
    if not recipients:
        logger.warning("EnableMailNotification is True but MailTO is empty.")
        return

    mail_from = config_module.MAIL_FROM
    mail_host = config_module.MAIL_HOST
    mail_port = int(config_module.MAIL_PORT)
    mail_user = config_module.MAIL_USER
    mail_pass = config_module.MAIL_PASS
    mail_ssl = getattr(config_module, "MAIL_SSL", False)
    mail_tls = getattr(config_module, "MAIL_TLS", False)

    html_body = build_html_report(report)
    subject = (
        f"[{report.status_label}] PDI Agent Performance — "
        f"{report.started_at.strftime('%Y-%m-%d %H:%M')} — "
        f"{report.upserted_count:,} upserted"
    )

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = ", ".join(recipients)
    message.attach(MIMEText(html_body, "html", "utf-8"))

    logger.info("Sending job report email to %s", recipients)

    if mail_ssl or mail_port == 465:
        with smtplib.SMTP_SSL(mail_host, mail_port, timeout=30) as server:
            server.login(mail_user, mail_pass)
            server.sendmail(mail_from, recipients, message.as_string())
    else:
        with smtplib.SMTP(mail_host, mail_port, timeout=30) as server:
            if mail_tls:
                server.starttls()
            server.login(mail_user, mail_pass)
            server.sendmail(mail_from, recipients, message.as_string())

    logger.info("Job report email sent successfully.")
