import boto3
import json
import os
import time
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────────
# Configuration (from Lambda env variables)
# ─────────────────────────────────────────
REGION           = os.environ.get("AWS_REGION_NAME", "us-east-1")
SNS_TOPIC_ARN    = os.environ.get("SNS_TOPIC_ARN", "")
S3_BUCKET        = os.environ.get("S3_BUCKET", "")
CPU_THRESHOLD    = 5.0
INSTANCE_HOURLY_COST = 0.0104  # t3.micro on-demand, us-east-1


# ─────────────────────────────────────────
# Step 0 — Resolve instance ID by tag
# ─────────────────────────────────────────
def get_instance_id() -> str:
    ec2 = boto3.client("ec2", region_name=REGION)
    resp = ec2.describe_instances(
        Filters=[{"Name": "tag:Name", "Values": ["finops-demo"]}]
    )
    reservations = resp.get("Reservations", [])
    if not reservations:
        raise RuntimeError("Instance 'finops-demo' not found.")
    instance = reservations[0]["Instances"][0]
    return instance["InstanceId"]


# ─────────────────────────────────────────
# EC2 Lifecycle — Start / Stop
# ─────────────────────────────────────────
def get_instance_state(instance_id: str) -> str:
    ec2 = boto3.client("ec2", region_name=REGION)
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    return resp["Reservations"][0]["Instances"][0]["State"]["Name"]


def ensure_instance_running(instance_id: str) -> bool:
    """Start EC2 if stopped. Returns True if it was started by us."""
    ec2   = boto3.client("ec2", region_name=REGION)
    state = get_instance_state(instance_id)
    print(f"[INFO] Instance state: {state}")

    if state == "running":
        return False  # already running, we didn't start it

    if state == "stopped":
        print(f"[INFO] Starting instance {instance_id} ...")
        ec2.start_instances(InstanceIds=[instance_id])

        # Wait up to 90s for running state
        for _ in range(18):
            time.sleep(5)
            state = get_instance_state(instance_id)
            print(f"[INFO] Waiting for running... current: {state}")
            if state == "running":
                print("[INFO] Instance is running.")
                return True

        raise RuntimeError("Instance did not reach running state in time.")

    raise RuntimeError(f"Instance is in unexpected state: {state}")


def stop_instance(instance_id: str) -> None:
    ec2 = boto3.client("ec2", region_name=REGION)
    print(f"[INFO] Stopping instance {instance_id} ...")
    ec2.stop_instances(InstanceIds=[instance_id])
    print("[INFO] Stop command sent.")


# ─────────────────────────────────────────
# Step 1 — Fetch CPU from CloudWatch
# ─────────────────────────────────────────
def get_cpu_average(instance_id: str, hours: int = 24) -> float:
    cw         = boto3.client("cloudwatch", region_name=REGION)
    end_time   = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=hours)

    response = cw.get_metric_statistics(
        Namespace  = "AWS/EC2",
        MetricName = "CPUUtilization",
        Dimensions = [{"Name": "InstanceId", "Value": instance_id}],
        StartTime  = start_time,
        EndTime    = end_time,
        Period     = 3600,
        Statistics = ["Average"],
    )

    datapoints = response.get("Datapoints", [])
    if not datapoints:
        print("[WARNING] No CloudWatch datapoints found.")
        return 0.0

    avg = sum(d["Average"] for d in datapoints) / len(datapoints)
    return round(avg, 2)


# ─────────────────────────────────────────
# Step 2 + 3 — Apply rule & estimate savings
# ─────────────────────────────────────────
def analyze(cpu_avg: float) -> dict:
    if cpu_avg < CPU_THRESHOLD:
        status         = "UNDERUTILIZED"
        recommendation = "Stop during off-hours (nights + weekends)"
        savings        = round(INSTANCE_HOURLY_COST * 128, 2)
    elif cpu_avg < 20:
        status         = "LOW UTILIZATION"
        recommendation = "Consider downsizing to t3.nano"
        savings        = round(INSTANCE_HOURLY_COST * 0.5 * 730, 2)
    else:
        status         = "NORMAL"
        recommendation = "No action needed"
        savings        = 0.0

    return {
        "status":            status,
        "recommendation":    recommendation,
        "estimated_savings": f"${savings}/month",
    }


# ─────────────────────────────────────────
# Step 4 — Generate report
# ─────────────────────────────────────────
def generate_report(instance_id: str, cpu_avg: float, analysis: dict) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""# FinOps Cost Optimization Report

**Generated:** {timestamp}

---

## Instance Details

| Field         | Value           |
|---------------|-----------------|
| Instance ID   | `{instance_id}` |
| Instance Name | finops-demo     |
| Instance Type | t3.micro        |
| Region        | {REGION}        |

---

## Utilization Analysis

| Metric            | Value      |
|-------------------|------------|
| Average CPU (24h) | {cpu_avg}% |
| Status            | **{analysis['status']}** |

---

## Recommendation

**{analysis['recommendation']}**

---

## Estimated Savings

**{analysis['estimated_savings']}**

---

*FinOps Agent — Automated cost governance report (Lambda)*
"""


# ─────────────────────────────────────────
# Step 5a — Save report to S3
# ─────────────────────────────────────────
def save_report_to_s3(report: str) -> tuple:
    """Returns (s3_uri, presigned_url)"""
    if not S3_BUCKET:
        print("[WARNING] S3_BUCKET not set — skipping S3 upload.")
        return "", ""

    s3        = boto3.client("s3", region_name=REGION)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    key       = f"reports/report_{timestamp}.md"

    s3.put_object(
        Bucket      = S3_BUCKET,
        Key         = key,
        Body        = report.encode("utf-8"),
        ContentType = "text/markdown",
    )

    # Also overwrite latest
    s3.put_object(
        Bucket      = S3_BUCKET,
        Key         = "reports/report_latest.md",
        Body        = report.encode("utf-8"),
        ContentType = "text/markdown",
    )

    s3_uri = f"s3://{S3_BUCKET}/{key}"

    # Generate a pre-signed URL valid for 7 days
    presigned_url = s3.generate_presigned_url(
        "get_object",
        Params     = {"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn  = 604800,  # 7 days in seconds
    )

    print(f"[INFO] Report saved to S3 → {s3_uri}")
    return s3_uri, presigned_url


# ─────────────────────────────────────────
# Step 5b — Send HTML email via SES
# ─────────────────────────────────────────
def build_html_email(instance_id: str, cpu_avg: float, analysis: dict, s3_url: str, presigned_url: str, timestamp: str) -> str:
    status         = analysis["status"]
    savings        = analysis["estimated_savings"]
    recommendation = analysis["recommendation"]

    status_color = "#c0392b" if status == "UNDERUTILIZED" else "#e67e22" if status == "LOW UTILIZATION" else "#27ae60"
    badge_color  = "#fde8e8" if status != "NORMAL" else "#e8fdf0"
    badge_border = "#e74c3c" if status != "NORMAL" else "#27ae60"

    report_block = ""
    if presigned_url:
        report_block = f"""
      <tr>
        <td style="padding:20px 24px 0;">
          <p style="margin:0 0 10px;font-size:11px;font-weight:600;letter-spacing:1px;color:#888;text-transform:uppercase;">Full Report</p>
          <a href="{presigned_url}" style="display:inline-block;background:#1a1f2e;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:6px;font-size:13px;font-weight:600;">
            View Full Report
          </a>
          <p style="margin:8px 0 0;font-size:11px;color:#aaa;">Link valid for 7 days</p>
        </td>
      </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>FinOps Alert</title></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;padding:32px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.10);">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#1a1f2e 0%,#2d3450 100%);padding:28px 24px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td>
                  <span style="font-size:24px;">⚠️</span>
                  <span style="font-size:22px;font-weight:700;color:#ffffff;vertical-align:middle;margin-left:10px;">FinOps Agent Alert</span>
                  <p style="margin:6px 0 0;font-size:13px;color:#8892b0;">Automated cost governance &middot; EventBridge + Lambda &middot; {REGION}</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Status Badge -->
        <tr>
          <td style="padding:20px 24px 0;">
            <div style="display:inline-block;background:{badge_color};border:1.5px solid {badge_border};border-radius:20px;padding:6px 16px;">
              <span style="font-size:12px;font-weight:700;color:{status_color};letter-spacing:0.5px;">● {status} INSTANCE DETECTED</span>
            </div>
          </td>
        </tr>

        <!-- Instance Details -->
        <tr>
          <td style="padding:20px 24px 0;">
            <p style="margin:0 0 10px;font-size:11px;font-weight:600;letter-spacing:1px;color:#888;text-transform:uppercase;">Instance Details</p>
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
              <tr style="border-bottom:1px solid #f0f0f0;">
                <td style="padding:10px 0;font-size:13px;color:#888;width:40%;">Instance ID</td>
                <td style="padding:10px 0;font-size:13px;color:#333;font-family:monospace;">{instance_id}</td>
              </tr>
              <tr style="border-bottom:1px solid #f0f0f0;">
                <td style="padding:10px 0;font-size:13px;color:#888;">Instance Name</td>
                <td style="padding:10px 0;font-size:13px;color:#333;font-family:monospace;">finops-demo</td>
              </tr>
              <tr style="border-bottom:1px solid #f0f0f0;">
                <td style="padding:10px 0;font-size:13px;color:#888;">Instance Type</td>
                <td style="padding:10px 0;font-size:13px;color:#333;font-family:monospace;">t3.micro</td>
              </tr>
              <tr style="border-bottom:1px solid #f0f0f0;">
                <td style="padding:10px 0;font-size:13px;color:#888;">Region</td>
                <td style="padding:10px 0;font-size:13px;color:#333;font-family:monospace;">{REGION}</td>
              </tr>
              <tr>
                <td style="padding:10px 0;font-size:13px;color:#888;">Report Generated</td>
                <td style="padding:10px 0;font-size:13px;color:#333;">{timestamp}</td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Utilization Cards -->
        <tr>
          <td style="padding:20px 24px 0;">
            <p style="margin:0 0 12px;font-size:11px;font-weight:600;letter-spacing:1px;color:#888;text-transform:uppercase;">Utilization Analysis (24H)</p>
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td width="33%" style="padding-right:8px;">
                  <div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;text-align:center;">
                    <p style="margin:0;font-size:28px;font-weight:700;color:{status_color};">{cpu_avg}%</p>
                    <p style="margin:6px 0 0;font-size:11px;color:#888;letter-spacing:0.5px;text-transform:uppercase;">Avg CPU</p>
                  </div>
                </td>
                <td width="33%" style="padding:0 4px;">
                  <div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;text-align:center;">
                    <p style="margin:0;font-size:14px;font-weight:700;color:{status_color};">{status}</p>
                    <p style="margin:6px 0 0;font-size:11px;color:#888;letter-spacing:0.5px;text-transform:uppercase;">Status</p>
                  </div>
                </td>
                <td width="33%" style="padding-left:8px;">
                  <div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;text-align:center;">
                    <p style="margin:0;font-size:28px;font-weight:700;color:#27ae60;">{savings}</p>
                    <p style="margin:6px 0 0;font-size:11px;color:#888;letter-spacing:0.5px;text-transform:uppercase;">Savings / Mo</p>
                  </div>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- Recommendation -->
        <tr>
          <td style="padding:20px 24px 0;">
            <p style="margin:0 0 10px;font-size:11px;font-weight:600;letter-spacing:1px;color:#888;text-transform:uppercase;">Recommendation</p>
            <div style="background:#fffbeb;border-left:4px solid #f59e0b;border-radius:6px;padding:14px 16px;">
              <p style="margin:0 0 4px;font-size:14px;font-weight:600;color:#333;">{recommendation}</p>
              <p style="margin:0;font-size:13px;color:#666;">Consider using AWS Instance Scheduler or an EventBridge cron rule to automate start/stop and capture this saving automatically.</p>
            </div>
          </td>
        </tr>

        <!-- Report Button -->
        {report_block}

        <!-- Footer -->
        <tr>
          <td style="padding:24px;margin-top:8px;">
            <hr style="border:none;border-top:1px solid #f0f0f0;margin:0 0 16px;">
            <p style="margin:0;font-size:12px;color:#aaa;text-align:center;">
              This alert was generated automatically by the FinOps Agent (EventBridge + Lambda).<br>
              To stop receiving notifications, <a href="#" style="color:#667eea;">click here to unsubscribe</a>.
              Please do not reply to this email.
            </p>
            <p style="margin:12px 0 0;font-size:11px;color:#ccc;text-align:center;">
              FinOps Agent &middot; AWS SES &middot; {timestamp}
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def send_html_email(instance_id: str, cpu_avg: float, analysis: dict, s3_url: str, presigned_url: str) -> None:
    alert_email = os.environ.get("ALERT_EMAIL", "")
    if not alert_email:
        print("[WARNING] ALERT_EMAIL not set — skipping HTML email.")
        return

    ses       = boto3.client("ses", region_name=REGION)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject   = f"FinOps Alert - {analysis['status']} Resource Detected"
    html_body = build_html_email(instance_id, cpu_avg, analysis, s3_url, presigned_url, timestamp)

    # Plain text fallback
    plain_body = (
        f"FinOps Alert\n\nInstance: finops-demo ({instance_id})\n"
        f"CPU (24h avg): {cpu_avg}%\nStatus: {analysis['status']}\n"
        f"Recommendation: {analysis['recommendation']}\n"
        f"Estimated savings: {analysis['estimated_savings']}\n"
        f"Report: {s3_url}\n\n"
        "Generated by FinOps Agent (EventBridge + Lambda)."
    )

    ses.send_email(
        Source      = f"FinOps Agent <{alert_email}>",
        Destination = {"ToAddresses": [alert_email]},
        Message     = {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": plain_body,  "Charset": "UTF-8"},
                "Html": {"Data": html_body,   "Charset": "UTF-8"},
            },
        },
        ReplyToAddresses = [alert_email],
    )
    print(f"[INFO] HTML email sent via SES → {alert_email}")


# ─────────────────────────────────────────
# Lambda Handler
# ─────────────────────────────────────────
def handler(event, context):
    print("=" * 52)
    print("   FinOps Agent Lambda — Starting Analysis")
    print("=" * 52)

    instance_id  = get_instance_id()
    print(f"[INFO] Target instance : {instance_id}")

    # Auto-start EC2 if stopped, track if we started it
    we_started_it = ensure_instance_running(instance_id)

    cpu_avg = get_cpu_average(instance_id)
    print(f"[INFO] Average CPU (24h): {cpu_avg}%")

    analysis = analyze(cpu_avg)
    print(f"[INFO] Status          : {analysis['status']}")
    print(f"[INFO] Recommendation  : {analysis['recommendation']}")
    print(f"[INFO] Est. savings    : {analysis['estimated_savings']}")

    report = generate_report(instance_id, cpu_avg, analysis)
    s3_url, presigned_url = save_report_to_s3(report)
    if analysis["status"] in ("UNDERUTILIZED", "LOW UTILIZATION"):
        send_html_email(instance_id, cpu_avg, analysis, s3_url, presigned_url)

    # Auto-stop EC2 if we were the ones who started it
    if we_started_it:
        stop_instance(instance_id)

    print("=" * 52)
    print("   FinOps Agent Lambda — Complete")
    print("=" * 52)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "instance_id":        instance_id,
            "cpu_avg":            cpu_avg,
            "status":             analysis["status"],
            "estimated_savings":  analysis["estimated_savings"],
            "s3_report":          s3_url,
            "ec2_auto_stopped":   we_started_it,
        }),
    }
