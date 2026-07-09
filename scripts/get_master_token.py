"""One-time setup: exchange a Google App Password for a gkeepapi master token.

Run this once with `uv run python scripts/get_master_token.py`. It prints the
master token and device ID to set as GOOGLE_KEEP_MASTER_TOKEN and
GOOGLE_KEEP_DEVICE_ID in the MCP server's environment. The app password is
used only for this exchange and is not stored anywhere.
"""

import getpass
import secrets

import gkeepapi


def main() -> None:
    email = input("Google account email: ").strip()
    app_password = getpass.getpass("App password (input hidden): ").replace(" ", "").strip()
    device_id = secrets.token_hex(8)

    keep = gkeepapi.Keep()
    keep.login(email, app_password, sync=False, device_id=device_id)
    master_token = keep.getMasterToken()

    print("\nSuccess. Set these in the MCP server's environment:\n")
    print(f"GOOGLE_KEEP_EMAIL={email}")
    print(f"GOOGLE_KEEP_MASTER_TOKEN={master_token}")
    print(f"GOOGLE_KEEP_DEVICE_ID={device_id}")
    print(
        "\nThe device ID is pinned so future logins look like the same "
        "device — keep it paired with this master token."
    )


if __name__ == "__main__":
    main()
