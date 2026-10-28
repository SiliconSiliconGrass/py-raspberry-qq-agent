"""Preview/apply the QQ attribute and a dedicated read-only Bot service account.

uv run --env-file ../guild-go/.env --env-file ../keycloak.env python scripts/configure_keycloak.py
Append --apply to write. No user bindings or existing clients are modified.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--client-id", default="qq-bot")
    parser.add_argument("--output", type=Path, default=Path("data/keycloak-bot.env"))
    args = parser.parse_args()
    issuer = os.environ["OIDC_ISSUER"].rstrip("/")
    base, realm = issuer.split("/realms/", 1)
    with httpx.Client(timeout=15) as http:
        response = http.post(
            base + "/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": os.environ["user"],
                "password": os.environ["password"],
            },
        )
        response.raise_for_status()
        http.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        admin = base + "/admin/realms/" + realm

        def get(path: str):
            response = http.get(admin + path)
            response.raise_for_status()
            return response.json()

        profile = get("/users/profile")
        attribute = {
            "name": "qq_number",
            "displayName": "QQ",
            "multivalued": False,
            "permissions": {"view": ["admin"], "edit": ["admin"]},
            "validations": {"pattern": {"pattern": "[1-9][0-9]{4,19}"}},
        }
        existing = next(
            (a for a in profile["attributes"] if a["name"] == "qq_number"), None
        )
        if existing and existing != attribute:
            raise SystemExit(
                "qq_number already exists with different settings; inspect it before applying"
            )
        clients = get("/clients")
        if any(c["clientId"] == args.client_id for c in clients):
            raise SystemExit(
                "Bot client already exists; inspect it rather than overwriting its settings"
            )
        management = next(
            (c for c in clients if c["clientId"] == "realm-management"), None
        )
        if management is None:
            management = next(
                (c for c in clients if c["clientId"] == realm + "-realm"), None
            )
        if management is None:
            raise SystemExit("Cannot find the realm user-management client")
        role = get("/clients/" + management["id"] + "/roles/view-users")
        client = {
            "clientId": args.client_id,
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "fullScopeAllowed": False,
        }
        print(
            json.dumps(
                {
                    "realm": realm,
                    "attribute": attribute,
                    "client": client,
                    "service_account_role": management["clientId"] + ":view-users",
                    "credentials_file": str(args.output),
                    "apply": args.apply,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not args.apply:
            return
        if args.output.exists():
            raise SystemExit(
                "Credential output already exists; refusing to overwrite it"
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Reserve a private credential file before making server changes.
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            if not existing:
                profile["attributes"].append(attribute)
                response = http.put(admin + "/users/profile", json=profile)
                response.raise_for_status()
            response = http.post(admin + "/clients", json=client)
            response.raise_for_status()
            client_id = get("/clients?clientId=" + args.client_id)[0]["id"]
            account = get("/clients/" + client_id + "/service-account-user")
            for path in [
                "/clients/" + client_id + "/scope-mappings/clients/" + management["id"],
                "/users/"
                + account["id"]
                + "/role-mappings/clients/"
                + management["id"],
            ]:
                response = http.post(admin + path, json=[role])
                response.raise_for_status()
            secret = get("/clients/" + client_id + "/client-secret")["value"]
            # Secrets are written locally, never printed.
            output.write("BOT_KEYCLOAK_CLIENT_ID=" + args.client_id + "\n")
            output.write("BOT_KEYCLOAK_CLIENT_SECRET=" + secret + "\n")
        args.output.chmod(0o600)
        print(
            "Applied. Credentials saved with mode 0600; no user QQ bindings were changed."
        )


if __name__ == "__main__":
    main()
