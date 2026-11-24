"""Narrow protobuf operations; no arbitrary mutation or update-mask input."""


def mutate(
    client, action: str, before: dict, change: dict, validate_only: bool
):
    target = before["target"]
    if action == "campaign_status":
        prefix, method, plural = "Campaign", "mutate_campaigns", "Campaigns"
    elif action == "daily_budget":
        prefix, method, plural = (
            "CampaignBudget",
            "mutate_campaign_budgets",
            "CampaignBudgets",
        )
    else:
        prefix = (
            "AdGroupCriterion" if target["ad_group_id"] else "CampaignCriterion"
        )
        method = (
            "mutate_ad_group_criteria"
            if target["ad_group_id"]
            else "mutate_campaign_criteria"
        )
        plural = (
            "AdGroupCriteria" if target["ad_group_id"] else "CampaignCriteria"
        )
    request = client.get_type(f"Mutate{plural}Request")
    request.customer_id = target["customer_id"]
    request.validate_only = validate_only
    request.partial_failure = False
    items = change["additions"] if action == "add_keywords" else [None]
    for item in items:
        op = client.get_type(f"{prefix}Operation")
        if action == "add_keywords":
            op.create.ad_group = f"customers/{target['customer_id']}/adGroups/{target['ad_group_id']}"
            op.create.negative = False
            op.create.status = change["status"]
            op.create.keyword.text = item["text"]
            op.create.keyword.match_type = item["match_type"]
        elif action == "remove_keyword":
            op.remove = before["keywords"][0]["resource_name"]
        else:
            if action == "daily_budget":
                op.update.resource_name = before["budget"]["resource_name"]
                op.update.amount_micros = change["amount_micros"]
                op.update_mask.paths.append("amount_micros")
            else:
                op.update.resource_name = (
                    target["campaign_resource"]
                    if action == "campaign_status"
                    else before["keywords"][0]["resource_name"]
                )
                op.update.status = change["status"]
                op.update_mask.paths.append("status")
        request.operations.append(op)
    service = client.get_service(f"{prefix}Service")
    return getattr(service, method)(request=request, retry=None, timeout=30)
