"""
Scenario registry.

To add a new scenario:
  1. Create app/scenarios/my_new_scenario.py
  2. Implement:  async def run(page, config, user_idx) -> dict
  3. Register it in REGISTRY below with a unique key.

The run() function must return a dict:
  {
    "status": "PASS" | "WARN" | "FAIL",
    "steps":  [ { "name": str, "status": str, "notes": [str], "duration_ms": int } ],
    "errors": [ str ]
  }
"""

from typing import Callable, Dict

# ── import all scenario modules ──────────────────────────────────────
from . import buoy_champ_onboarding

REGISTRY: Dict[str, Callable] = {
    "buoy_champ_onboarding": buoy_champ_onboarding.run,
    # add more scenarios here:
    # "employee_enrollment": employee_enrollment.run,
}

REGISTRY_META: Dict[str, dict] = {
    "buoy_champ_onboarding": {
        "label": "Business Owner Onboarding",
        "description": "Full UAT flow: Onboarding → LOI → PandaDoc signature (Business Owner path)",
    },
    # "employee_enrollment": {
    #     "label": "Employee Enrollment",
    #     "description": "Employee fills in census data and enrolls in benefits",
    # },
}


def get_scenario_func(key: str) -> Callable:
    if key not in REGISTRY:
        raise KeyError(f"Unknown scenario key: {key!r}. Available: {list(REGISTRY)}")
    return REGISTRY[key]


def list_available() -> list:
    return [
        {"key": k, **REGISTRY_META.get(k, {"label": k, "description": ""})}
        for k in REGISTRY
    ]
