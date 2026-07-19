"""Push secrets from the local .env to Fly.io without values touching shell history.

Run: python -m scripts.push_fly_secrets [--only KEY1,KEY2] [--dry-run] [--flyctl PATH]

Reads .env in the repo root, filters to known secret keys (never pushes local-only
settings), and calls `flyctl secrets import` once with all pairs on STDIN — a single
app restart. Values go through stdin (never argv, so they don't appear in the local
process list), never through a shell, and only key NAMES are printed.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Keys that belong on Fly. Local-only keys (e.g. DATABASE_URL_DIRECT for alembic on
# the workstation) are excluded by default — add via --only if ever needed remotely.
FLY_KEYS = [
    "APP_BASE_URL",
    "CLINIC_TZ",
    "CLINIKO_API_KEY",
    "CLINIKO_VENDOR_EMAIL",
    "CLINIKO_VENDOR_NAME",
    "DATABASE_URL",
    "OPENAI_API_KEY",
    "RETELL_AGENT_ID",
    "RETELL_API_KEY",
    "RETELL_PHONE_NUMBER",
    "RETELL_VOICE_ID",
    "TOOL_SHARED_SECRET",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_VERIFY_SERVICE_SID",
    "REQUIRE_VERIFICATION",
    "TURNSTILE_SITE_KEY",
    "TURNSTILE_SECRET_KEY",
    "MAX_WEB_CALLS_PER_DAY",
    "STAFF_TRANSFER_TARGET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "SLACK_WEBHOOK_URL",
    "SENTRY_DSN",
    "HEALTHCHECKS_RECONCILE_URL",
]


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="comma-separated key names (subset of .env)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--flyctl", default=None, help="path to flyctl if not on PATH")
    args = parser.parse_args()

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        sys.exit(f"ERROR: {env_path} not found")
    env = parse_env(env_path)

    wanted = [k.strip() for k in args.only.split(",")] if args.only else FLY_KEYS
    missing = [k for k in wanted if not env.get(k)]
    pairs = {k: env[k] for k in wanted if env.get(k)}
    if missing:
        print(f"skipping (empty or absent in .env): {', '.join(missing)}")
    if not pairs:
        sys.exit("ERROR: nothing to push")

    print(f"pushing {len(pairs)} secrets: {', '.join(pairs)}")
    if args.dry_run:
        print("dry run — not calling flyctl")
        return

    flyctl = args.flyctl or shutil.which("flyctl")
    if not flyctl:
        sys.exit("ERROR: flyctl not on PATH — pass --flyctl <full path to flyctl.exe>")
    # `flyctl secrets import` reads KEY=VALUE lines from stdin, so secret VALUES
    # never enter the process argument list (visible to other local processes).
    payload = "".join(f"{key}={value}\n" for key, value in pairs.items())
    result = subprocess.run([flyctl, "secrets", "import"], input=payload, text=True, cwd=env_path.parent)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
