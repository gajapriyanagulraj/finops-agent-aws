import boto3
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────
REGION             = os.environ.get("AWS_REGION", "us-east-1")
INSTANCE_ID        = os.environ.get("INSTANCE_ID", "")      # optional override
SNS_TOPIC_ARN      = os.environ.get("SNS_TOPIC_ARN", "")    # set after terraform apply
CPU_THRESHOLD      = 5.0
INSTANCE_HOURLY_COST = 0.0104  # t3.micro on-demand, us-east-1


# ─────────────────────────────────────────
# Step 0 — Resolve instance ID
# ─────────────────────────────────────────
def get_instance_id() -> str:
    """Return INSTANCE_ID env var, EC2 metadata, or auto-detect by tag Name=finops-demo."""
    if INSTANCE_ID:
        return INSTANCE_ID

    # Try EC2 instance metadata service (IMDSv2)
    try:
        import urllib.request

        token_req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
            method="PUT",
        )
        with urllib.request.urlopen(token_req, timeout=2) as r:
            token = r.read().decode()

        id_req = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/instance-id",
            headers={"X-aws-ec2-metadata-token": token},
        )
        with urllib.request.urlopen(id_req, timeout=2) as r:
            return r.read().decode()
    except Exception:
        pass

    # Fallback — describe instances by tag
    ec2 = boto3.client("ec2", region_name=REGION)
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": ["finops-demo"]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    reservations = resp.get("Reservations", [])
    if reservations:
        return reservations[0]["Instances"][0]["InstanceId"]

    raise RuntimeError(
        "Cannot find instance. Set INSTANCE_ID env variable or ensure "
        "the 'finops-demo' instance is running."
    )


# ─────────────────────────────────────────
# Step 1 — Fetch CPU from CloudWatch
# ─────────────────────────────────────────
def get_cpu_average(instance_id: str, hours: int = 24) -> float:
    """Return average CPU utilization (%) over the last N hours."""
    cw = boto3.client("cloudwatch", region_name=REGION)
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
        print("[WARNING] No CloudWatch datapoints yet. Instance may be < 1 hour old.")
        return 0.0

    avg = sum(d["Average"] for d in datapoints) / len(datapoints)
    return round(avg, 2)


# ─────────────────────────────────────────
# Step 2 + 3 — Apply rule & estimate savings
# ─────────────────────────────────────────
def analyze(cpu_avg: float) -> dict:
    """Apply FinOps utilization rules and return status + recommendation + savings."""
    if cpu_avg < CPU_THRESHOLD:
        status           = "UNDERUTILIZED"
        recommendation   = "Stop during off-hours (nights + weekends)"
        # ~128 off-hours per month (16 h/day × 5 days + 48 h weekend)
        off_hours        = 128
        savings          = round(INSTANCE_HOURLY_COST * off_hours, 2)

    elif cpu_avg < 20:
        status           = "LOW UTILIZATION"
        recommendation   = "Consider downsizing to t3.nano"
        # t3.nano is ~50% cheaper; 730 h/month
        savings          = round(INSTANCE_HOURLY_COST * 0.5 * 730, 2)

    else:
        status           = "NORMAL"
        recommendation   = "No action needed"
        savings          = 0.0

    return {
        "status":             status,
        "recommendation":     recommendation,
        "estimated_savings":  f"${savings}/month",
    }


# ─────────────────────────────────────────
# Step 4 — Generate report
# ─────────────────────────────────────────
def generate_report(instance_id: str, cpu_avg: float, analysis: dict) -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    return f"""# FinOps Cost Optimization Report

**Generated:** {timestamp}

---

## Instance Details

| Field         | Value          |
|---------------|----------------|
| Instance ID   | `{instance_id}` |
| Instance Name | finops-demo    |
| Instance Type | t3.micro       |
| Region        | {REGION}       |

---

## Utilization Analysis

| Metric            | Value       |
|-------------------|-------------|
| Average CPU (24h) | {cpu_avg}%  |
| Status            | **{analysis['status']}** |

---

## Recommendation

**{analysis['recommendation']}**

---

## Estimated Savings

**{analysis['estimated_savings']}**

---

*FinOps Agent — Automated cost governance report*
"""


def save_report(content: str) -> str:
    reports_dir = Path(__file__).parent.parent / "reports"
    reports_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"report_{timestamp}.md"
    path.write_text(content)

    # Always keep a latest copy for quick access
    (reports_dir / "report_latest.md").write_text(content)

    print(f"[INFO] Report saved → {path}")
    return str(path)


# ─────────────────────────────────────────
# Step 5 — Send SNS email alert
# ─────────────────────────────────────────
def send_sns_alert(instance_id: str, cpu_avg: float, analysis: dict, report: str) -> None:
    if not SNS_TOPIC_ARN:
        print("[WARNING] SNS_TOPIC_ARN not set — skipping email alert.")
        return

    sns = boto3.client("sns", region_name=REGION)

    subject = f"FinOps Alert - {analysis['status']} Resource Detected"
    body = f"""FinOps Agent Alert
==================

Instance:      finops-demo ({instance_id})
Instance Type: t3.micro
Region:        {REGION}

CPU Usage (24h avg): {cpu_avg}%
Status:              {analysis['status']}

Recommendation:
  {analysis['recommendation']}

Potential Savings:
  {analysis['estimated_savings']}


========================================
FULL COST OPTIMIZATION REPORT
========================================

{report}

--
This alert was generated automatically by the FinOps Agent.
"""

    sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=body)
    print(f"[INFO] SNS alert sent with full report → {SNS_TOPIC_ARN}")


# ─────────────────────────────────────────
# Main
# ─────────────────────────────────────────
def main():
    print("=" * 52)
    print("   FinOps Agent — Starting Analysis")
    print("=" * 52)

    instance_id = get_instance_id()
    print(f"[INFO] Target instance : {instance_id}")

    cpu_avg = get_cpu_average(instance_id)
    print(f"[INFO] Average CPU (24h): {cpu_avg}%")

    analysis = analyze(cpu_avg)
    print(f"[INFO] Status          : {analysis['status']}")
    print(f"[INFO] Recommendation  : {analysis['recommendation']}")
    print(f"[INFO] Est. savings    : {analysis['estimated_savings']}")

    report = generate_report(instance_id, cpu_avg, analysis)
    save_report(report)

    if analysis["status"] in ("UNDERUTILIZED", "LOW UTILIZATION"):
        send_sns_alert(instance_id, cpu_avg, analysis, report)

    print("=" * 52)
    print("   FinOps Agent — Complete")
    print("=" * 52)


if __name__ == "__main__":
    main()
