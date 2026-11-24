"""Live state needed to constrain approved Google Ads management changes."""

import re
from decimal import Decimal

from fastmcp.exceptions import ToolError

from ads_mcp.tools import negatives as n


def amount_micros(amount: str) -> int:
    """Accept exact decimal currency units, never binary floats or exponents."""
    if not isinstance(amount, str) or not re.fullmatch(
        r"[0-9]{1,13}(?:\.[0-9]{1,6})?", amount
    ):
        raise ToolError(
            "amount must be a decimal string with at most six decimal places."
        )
    micros = int(Decimal(amount) * 1000000)
    if not 0 < micros <= 9223372036854775807:
        raise ToolError(
            "The daily amount must be positive and fit Google's int64 micros field."
        )
    return micros


def selection(customer_id, campaign_id, ad_group_id=None) -> dict:
    return {
        "customer_id": n._id(customer_id, "customer_id", True),
        "campaign_id": n._id(campaign_id, "campaign_id"),
        "ad_group_id": (
            n._id(ad_group_id, "ad_group_id")
            if ad_group_id is not None
            else None
        ),
    }


def target(client, selected: dict, manager: str) -> dict:
    """Reauthorize a live MCC client; resolve names and exact parent resources."""
    customer, campaign, group = (
        selected[k] for k in ("customer_id", "campaign_id", "ad_group_id")
    )
    hierarchy = n._rows(
        client,
        manager,
        f"SELECT customer_client.id, customer_client.level, customer_client.manager, customer_client.status FROM customer_client WHERE customer_client.id = {customer}",
    )
    if not any(
        str(r.customer_client.id) == customer
        and r.customer_client.level > 0
        and not r.customer_client.manager
        and r.customer_client.status.name == "ENABLED"
        for r in hierarchy
    ):
        raise ToolError("Select an active client under the connected manager.")
    accounts = n._rows(
        client,
        customer,
        "SELECT customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone, customer.manager, customer.status FROM customer LIMIT 1",
    )
    campaigns = n._rows(
        client,
        customer,
        f"SELECT campaign.id, campaign.resource_name, campaign.name, campaign.status, campaign.advertising_channel_type, campaign.campaign_budget FROM campaign WHERE campaign.id = {campaign}",
    )
    if len(accounts) != 1 or len(campaigns) != 1:
        raise ToolError("The account or campaign was not found.")
    account, camp = accounts[0].customer, campaigns[0].campaign
    if (
        str(account.id) != customer
        or account.manager
        or account.status.name != "ENABLED"
        or str(camp.id) != campaign
        or camp.status.name not in {"ENABLED", "PAUSED"}
    ):
        raise ToolError(
            "The account or campaign is not an eligible active target."
        )
    expected = f"customers/{customer}/campaigns/{campaign}"
    if camp.resource_name != expected:
        raise ToolError(
            "The campaign resource does not match the selected account."
        )
    result = selected | {
        "account_name": account.descriptive_name,
        "currency_code": account.currency_code,
        "time_zone": account.time_zone,
        "campaign_name": camp.name,
        "campaign_resource": camp.resource_name,
        "campaign_status": camp.status.name,
        "campaign_type": camp.advertising_channel_type.name,
        "budget_resource": camp.campaign_budget,
    }
    if group:
        groups = n._rows(
            client,
            customer,
            f"SELECT ad_group.id, ad_group.name, ad_group.status, campaign.id FROM ad_group WHERE ad_group.id = {group}",
        )
        if (
            len(groups) != 1
            or str(groups[0].ad_group.id) != group
            or str(groups[0].campaign.id) != campaign
            or groups[0].ad_group.status.name not in {"ENABLED", "PAUSED"}
        ):
            raise ToolError(
                "The ad group is not active in the selected campaign."
            )
        result.update(
            ad_group_name=groups[0].ad_group.name,
            ad_group_status=groups[0].ad_group.status.name,
        )
    return result


def budget(client, resolved: dict) -> dict:
    resource = resolved["budget_resource"]
    customer = resolved["customer_id"]
    if not re.fullmatch(
        rf"customers/{customer}/campaignBudgets/[1-9][0-9]*", resource
    ):
        raise ToolError("The campaign has no supported existing budget.")
    rows = n._rows(
        client,
        customer,
        f"SELECT campaign_budget.id, campaign_budget.resource_name, campaign_budget.name, campaign_budget.amount_micros, campaign_budget.total_amount_micros, campaign_budget.period, campaign_budget.status, campaign_budget.explicitly_shared FROM campaign_budget WHERE campaign_budget.resource_name = '{resource}'",
    )
    if (
        len(rows) != 1
        or rows[0].campaign_budget.resource_name != resource
        or rows[0].campaign_budget.status.name != "ENABLED"
    ):
        raise ToolError("The existing campaign budget could not be resolved.")
    b = rows[0].campaign_budget
    members = n._rows(
        client,
        customer,
        f"SELECT campaign.id, campaign.name, campaign.status FROM campaign WHERE campaign.campaign_budget = '{resource}' AND campaign.status != REMOVED",
    )
    affected = sorted(
        [
            {
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "status": r.campaign.status.name,
            }
            for r in members
        ],
        key=lambda r: r["campaign_id"],
    )
    if resolved["campaign_id"] not in {m["campaign_id"] for m in affected}:
        raise ToolError(
            "The selected campaign's budget membership changed. Preview again."
        )
    return {
        "resource_name": resource,
        "name": b.name,
        "period": b.period.name,
        "amount_micros": b.amount_micros,
        "amount": format(Decimal(b.amount_micros) / 1000000, "f"),
        "total_amount_micros": b.total_amount_micros,
        "explicitly_shared": b.explicitly_shared,
        "currency_code": resolved["currency_code"],
        "affected_campaigns": affected,
    }


def keywords(client, resolved: dict) -> list[dict]:
    group = resolved["ad_group_id"]
    level = "ad_group" if group else "campaign"
    owner = group or resolved["campaign_id"]
    cr = f"{level}_criterion"
    rows = n._rows(
        client,
        resolved["customer_id"],
        f"SELECT {cr}.resource_name, {cr}.criterion_id, {cr}.type, {cr}.negative, {cr}.status, {cr}.keyword.text, {cr}.keyword.match_type FROM {cr} WHERE {level}.id = {owner} AND {cr}.status != REMOVED",
    )
    result = []
    path = "adGroupCriteria" if group else "campaignCriteria"
    for row in rows:
        item = getattr(row, cr)
        if item.type_.name != "KEYWORD" or item.status.name == "REMOVED":
            continue
        if (
            item.resource_name
            != f"customers/{resolved['customer_id']}/{path}/{owner}~{item.criterion_id}"
        ):
            raise ToolError(
                "A keyword resource did not match its selected parent."
            )
        result.append(
            {
                "criterion_id": str(item.criterion_id),
                "resource_name": item.resource_name,
                "text": item.keyword.text,
                "match_type": item.keyword.match_type.name,
                "negative": item.negative,
                "status": item.status.name,
            }
        )
    return sorted(result, key=lambda r: r["criterion_id"])


def snapshot(
    client, action: str, selected: dict, change: dict, manager: str
) -> dict:
    resolved = target(client, selected, manager)
    state = {"target": resolved}
    if action in {"campaign_status", "daily_budget"}:
        state["budget"] = budget(client, resolved)
    else:
        supported = (
            {"SEARCH"}
            if selected["ad_group_id"]
            else {"SEARCH", "SHOPPING", "PERFORMANCE_MAX"}
        )
        if resolved["campaign_type"] not in supported:
            raise ToolError(
                "Keyword management supports Search ad groups and direct Search, Shopping or Performance Max campaign negatives."
            )
        if not selected["ad_group_id"] and not change.get("negative", False):
            raise ToolError("Positive keywords require a Search ad group.")
        found = keywords(client, resolved)
        if action == "add_keywords":
            wanted = {n._keyword_key(k) for k in change["keywords"]}
            state["keywords"] = [
                k
                for k in found
                if not k["negative"] and n._keyword_key(k) in wanted
            ]
        else:
            state["keywords"] = [
                k for k in found if k["criterion_id"] == change["criterion_id"]
            ]
    return state


def satisfied(before: dict, observed: dict, action: str, change: dict) -> bool:
    """Compare an entire expected snapshot, allowing only the approved differences."""
    import copy

    expected = copy.deepcopy(before)
    if action == "campaign_status":
        expected["target"]["campaign_status"] = change["status"]
        for member in expected["budget"]["affected_campaigns"]:
            if member["campaign_id"] == expected["target"]["campaign_id"]:
                member["status"] = change["status"]
    elif action == "daily_budget":
        expected["budget"]["amount_micros"] = change["amount_micros"]
        expected["budget"]["amount"] = format(
            Decimal(change["amount_micros"]) / 1000000, "f"
        )
    elif action == "keyword_status":
        expected["keywords"][0]["status"] = change["status"]
    elif action == "remove_keyword":
        expected["keywords"] = []
    elif action == "add_keywords":
        existing = {n._keyword_key(k): k for k in observed["keywords"]}
        if any(
            n._keyword_key(k) not in existing
            or existing[n._keyword_key(k)]["status"] != change["status"]
            for k in change["additions"]
        ):
            return False
        old = {k["resource_name"]: k for k in before["keywords"]}
        if any(k not in observed["keywords"] for k in old.values()):
            return False
        expected["keywords"] = observed["keywords"]
    return expected == observed
