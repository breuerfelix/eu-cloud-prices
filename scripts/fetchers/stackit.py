"""STACKIT — public PIM catalog (no authentication)."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from . import common

BASE_URL = "https://pim.api.stackit.cloud/v3alpha1/skus"
PAGE_LIMIT = 100
DEFAULT_REGION = "eu01"
LOCATION = "eu-de"

COMPUTE_PRODUCTS = frozenset({
    "Server",
    "GPU Server",
    "Confidential Server",
})


def _parse_category(title: str, flavor: str) -> str:
    """Map STACKIT SKU title / flavor prefix to valid schema category enum."""
    t = title.lower()
    if t.startswith("tiny server") or flavor.startswith("t"):
        return "burstable"
    if t.startswith("compute optimized") or flavor.startswith(("c", "s")):
        return "compute"
    if t.startswith("memory optimized") or flavor.startswith(("b", "m")):
        return "memory"
    if t.startswith("gpu server") or flavor.startswith("n"):
        return "gpu"
    return "general"


def _parse_hardware(hardware: str, flavor: str) -> str:
    """Derive hardware identifier matching historical conventions (vendor-genX)."""
    m = re.match(r"^[a-zA-Z]+([0-9]+)([a-zA-Z]*)", flavor)
    gen = f"-gen{m.group(1)}" if m else ""
    hw = (hardware or "").lower()
    if "intel" in hw:
        return f"intel{gen}"
    if "amd" in hw:
        return f"amd{gen}"
    if "arm" in hw:
        return f"arm{gen}"
    if "gpu" in hw:
        # GPU flavors usually based on AMD or Intel host nodes
        return f"amd{gen}" if (m and m.group(2) == "a") else f"intel{gen}"
    return f"{hw}{gen}" if hw else "unknown"


def _parse_gpu_count(flavor: str) -> int | None:
    """Extract GPU count from flavor suffix (e.g. .g1 -> 1, .g2 -> 2)."""
    m = re.search(r"\.g([0-9]+)$", flavor)
    return int(m.group(1)) if m else None


def parse_skus(skus: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float | None]:
    """Parse raw PIM SKU items into schema-conforming instances and K8s control plane cost."""
    instances = []
    seen_flavors: set[str] = set()
    k8s_cost: float | None = None

    for item in skus:
        if item.get("deprecated"):
            continue

        prod_name = item.get("product", {}).get("name")

        # Extract K8s Control Plane monthly cost
        if prod_name == "Kubernetes Engine":
            price_monthly = item.get("price", {}).get("monthly", {}).get("value")
            if price_monthly is not None:
                k8s_cost = round(float(price_monthly), 2)
            continue

        if prod_name not in COMPUTE_PRODUCTS:
            continue

        # Exclude internal / unlisted price list items unless already established
        if not item.get("priceListVisibility", False):
            continue

        attrs = item.get("productSpecificAttributes", {})
        flavor = attrs.get("flavor")
        if not flavor:
            continue

        is_metro = attrs.get("metro", False)
        # Filter metro variants to prevent duplicates, unless flavor is only available in metro
        if is_metro and flavor != "n1.56d.g4":
            continue

        if flavor in seen_flavors:
            continue
        seen_flavors.add(flavor)

        price = item.get("price", {})
        hourly = price.get("list", {}).get("value")
        monthly = price.get("monthly", {}).get("value")
        if hourly is None or monthly is None:
            continue

        raw_hw = attrs.get("hardware", "")
        architecture = "arm64" if raw_hw == "ARM" else "x86"
        overprov = bool(attrs.get("cpuOverprovisioning", False))

        instance: dict[str, Any] = {
            "id": flavor,
            "name": flavor.upper(),
            "vcpu": attrs.get("vCPU", 0),
            "ram_gb": attrs.get("ram", 0),
            "disk_gb": 0,
            "disk_type": "network",
            "price_hourly": round(float(hourly), 8),
            "price_monthly": round(float(monthly), 2),
            "currency": price.get("list", {}).get("currencyCode", "EUR"),
            "architecture": architecture,
            "location": LOCATION,
            "category": _parse_category(item.get("title", ""), flavor),
            "dedicated": not overprov,
            "overprovisioned": overprov,
            "hardware": _parse_hardware(raw_hw, flavor),
        }

        gpu_count = _parse_gpu_count(flavor)
        if gpu_count:
            instance["gpu_count"] = gpu_count

        instances.append(instance)

    instances.sort(key=lambda x: x["id"])
    return instances, k8s_cost


def fetch(ctx: common.Context, region: str = DEFAULT_REGION) -> dict[str, Any]:
    """Fetch all pages from STACKIT PIM v3alpha1 endpoint and return price file payload."""
    all_skus: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        url = f"{BASE_URL}?limit={PAGE_LIMIT}&region={region}"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor)}"

        data = ctx.http_get_json(url)
        items = data.get("data", [])
        all_skus.extend(items)

        meta = data.get("meta", {})
        if not meta.get("hasNextPage") or not meta.get("nextCursor"):
            break
        cursor = meta["nextCursor"]

    instances, k8s_cost = parse_skus(all_skus)
    if not instances:
        raise common.FetchError(f"no STACKIT instance SKUs found for region {region}")

    payload: dict[str, Any] = {
        "provider": "stackit",
        "fetched_at": common.now_iso(),
        "source": BASE_URL,
        "source_url": BASE_URL,
        "manual": False,
        "instances": instances,
    }
    if k8s_cost is not None:
        payload["control_plane_cost"] = k8s_cost

    return payload
