"""Tests for subscription entitlement derivation."""

import time

from api import plans


def test_paid_status_with_future_period_end_stays_paid():
    account = {
        "status": "active",
        "plan": "pro",
        "run_credits": 2,
        "current_period_end": int(time.time()) + 3600,
    }
    entitlements = plans.entitlements_for(account)

    assert entitlements["plan"] == "pro"
    assert entitlements["providers"] == "all"
    assert entitlements["monthly_run_cap"] == plans.PLANS["pro"].monthly_run_cap
    assert entitlements["run_credits"] == 2


def test_paid_status_with_past_period_end_downgrades_to_free():
    account = {
        "status": "active",
        "plan": "business",
        "current_period_end": int(time.time()) - 1,
    }
    entitlements = plans.entitlements_for(account)

    assert entitlements["plan"] == "free"
    assert entitlements["providers"] == plans.PLANS["free"].providers
    assert entitlements["monthly_run_cap"] == plans.PLANS["free"].monthly_run_cap


def test_missing_account_defaults_to_free():
    entitlements = plans.entitlements_for(None)

    assert entitlements["plan"] == "free"
    assert entitlements["providers"] == "gmail"
    assert entitlements["monthly_run_cap"] == plans.PLANS["free"].monthly_run_cap


def test_invalid_period_end_is_treated_as_inactive():
    account = {
        "status": "active",
        "plan": "pro",
        "current_period_end": "n/a",
    }
    entitlements = plans.entitlements_for(account)

    assert entitlements["plan"] == "free"
