"""azd lifecycle hooks. Invoked from azure.yaml; not meant to be run by hand.

preprovision
    * checks the required azd environment values are set
    * regenerates infra/generated/dab-config.{poc,secure}.json from dab/dab-config.json
    * PoC: if ALLOWED_IP_RANGES is unset, pins it to your current public IP

postprovision
    * grants DAB's managed identity a role on the Fabric workspace
    * prints the endpoint and how to test it

postdown
    * deletes the secure profile's Entra app registration, which is not in the resource group
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
FABRIC_API = "https://api.fabric.microsoft.com/v1"
REQUIRED = ("FABRIC_SQL_SERVER", "FABRIC_SQL_DATABASE", "FABRIC_WORKSPACE_ID")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def azd_env_set(name: str, value: str) -> None:
    subprocess.run([shutil.which("azd") or "azd", "env", "set", name, value], check=True)


def preprovision() -> None:
    missing = [n for n in REQUIRED if not env(n)]
    if missing:
        sys.exit(
            "Missing azd environment values: " + ", ".join(missing) + "\n"
            + "\n".join(f"  azd env set {n} <value>" for n in missing)
        )

    profile = env("DEPLOYMENT_PROFILE", "poc")
    if profile not in ("poc", "secure"):
        sys.exit(f"DEPLOYMENT_PROFILE must be 'poc' or 'secure', got '{profile}'")

    subprocess.run([sys.executable, os.path.join(HERE, "build_profiles.py")], check=True)

    if profile == "poc":
        ranges = env("ALLOWED_IP_RANGES")
        if not ranges:
            with urllib.request.urlopen("https://api.ipify.org", timeout=10) as r:
                ip = r.read().decode().strip()
            ranges = f"{ip}/32"
            azd_env_set("ALLOWED_IP_RANGES", ranges)
            print(f"ALLOWED_IP_RANGES not set; allowing only your current IP {ranges}")
            print("  If Azure traffic leaves through a proxy or SSE client (e.g. Global Secure Access),")
            print("  the endpoint sees a different, often rotating, IP and will return 403.")
        if any(r.strip() in ("0.0.0.0/0", "*") for r in ranges.split(",")):
            print("WARNING: ALLOWED_IP_RANGES opens the anonymous PoC endpoint to the whole internet.")
    print(f"profile: {profile}")


def grant_fabric_access() -> None:
    from azure.identity import DefaultAzureCredential

    workspace = env("FABRIC_WORKSPACE_ID")
    principal = env("DAB_IDENTITY_PRINCIPAL_ID")
    role = env("FABRIC_WORKSPACE_ROLE", "Viewer")
    token = DefaultAzureCredential().get_token("https://api.fabric.microsoft.com/.default").token
    r = httpx.post(
        f"{FABRIC_API}/workspaces/{workspace}/roleAssignments",
        headers={"Authorization": f"Bearer {token}"},
        json={"principal": {"id": principal, "type": "ServicePrincipal"}, "role": role},
        timeout=30,
    )
    if r.status_code in (200, 201):
        print(f"granted {env('DAB_IDENTITY_NAME')} the {role} role on workspace {workspace}")
    elif r.status_code == 409:
        print(f"{env('DAB_IDENTITY_NAME')} already has a role on workspace {workspace}")
    else:
        sys.exit(
            f"Fabric role assignment failed: HTTP {r.status_code} {r.text[:400]}\n"
            "You need Admin or Member on the workspace. Grant it by hand in Fabric:\n"
            f"  Workspace -> Manage access -> add '{env('DAB_IDENTITY_NAME')}' as {role}"
        )


def postprovision() -> None:
    grant_fabric_access()
    endpoint = env("MCP_ENDPOINT")
    rg = env("AZURE_RESOURCE_GROUP")
    print(f"\nMCP endpoint: {endpoint}")
    print("DAB restarts until it can reach Fabric; allow a minute or two after the grant.")
    if env("DEPLOYMENT_PROFILE", "poc") == "secure":
        job = env("TEST_JOB_NAME")
        print("The endpoint is only reachable inside the VNet. Run the in-VNet test:")
        print(f"  az containerapp job start -g {rg} -n {job}")
        print(f"  az containerapp job logs show -g {rg} -n {job} --container test --follow")
    else:
        print("Test it:")
        print(f"  python scripts/test_endpoint.py --url {endpoint}")


def postdown() -> None:
    import json

    app_id = env("ENTRA_APP_ID")
    if not app_id:
        return
    # azd's own credential, so this works without a separate `az login`.
    out = subprocess.run(
        [shutil.which("azd") or "azd", "auth", "token", "--scope", "https://graph.microsoft.com/.default", "--output", "json"],
        capture_output=True, text=True, check=True,
    ).stdout
    token = json.loads(out)["token"]
    r = httpx.delete(
        f"https://graph.microsoft.com/v1.0/applications(appId='{app_id}')",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if r.status_code in (204, 404):
        print(f"deleted Entra app registration {app_id}")
        azd_env_set("ENTRA_APP_ID", "")
    else:
        sys.exit(
            f"could not delete Entra app registration {app_id}: HTTP {r.status_code} {r.text[:300]}\n"
            f"  delete it by hand: Entra admin center > App registrations > {app_id}"
        )


if __name__ == "__main__":
    {"preprovision": preprovision, "postprovision": postprovision, "postdown": postdown}[sys.argv[1]]()
