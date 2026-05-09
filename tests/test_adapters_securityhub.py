"""Tests for SecurityHubFindingsAdapter.

All tests use stub boto3 clients — no real AWS credentials required.
Coverage targets:
- to_ticket(): all severity labels, all workflow statuses, tags
  (region/product/company), AccountEntity, timestamps, description
  extraction, category from Types[], external_url from ProductArn
- from_ticket(): minimal dict with FindingId
- fetch_new(): pagination, since filter (UpdatedAt ASFF date filter)
- write(): raises NotImplementedError
- Edge cases: missing Severity block, missing Workflow, empty Types,
  Description as dict vs string, empty title fallback
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from ticketsync.adapters.securityhub import SecurityHubFindingsAdapter
from ticketsync.models import Ticket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_finding(
    finding_id: str = "arn:aws:securityhub:us-east-1:123:product/aws/guardduty/f1",
    title: str = "Suspicious activity detected",
    description: str = "An unusual API call was made.",
    severity_label: str = "HIGH",
    workflow_status: str = "NEW",
    account_id: str = "123456789012",
    region: str = "us-east-1",
    product_name: str = "GuardDuty",
    company_name: str = "Amazon",
    types: list[str] | None = None,
    created_at: str = "2026-05-01T10:00:00Z",
    updated_at: str = "2026-05-01T11:00:00Z",
    product_arn: str = "arn:aws:securityhub:us-east-1::product/aws/guardduty",
) -> dict[str, Any]:
    return {
        "Id": finding_id,
        "Title": title,
        "Description": description,
        "Severity": {"Label": severity_label, "Normalized": 70},
        "Workflow": {"Status": workflow_status},
        "AwsAccountId": account_id,
        "Region": region,
        "ProductName": product_name,
        "CompanyName": company_name,
        "Types": types if types is not None else ["Software and Configuration Checks/AWS Security Best Practices"],
        "CreatedAt": created_at,
        "UpdatedAt": updated_at,
        "ProductArn": product_arn,
    }


def make_stub_client(
    findings: list[dict[str, Any]] | None = None,
    next_token: str = "",
) -> MagicMock:
    client = MagicMock()
    client.get_findings.return_value = {
        "Findings": findings or [],
        "NextToken": next_token,
    }
    return client


def make_adapter(
    client: MagicMock | None = None,
    filters: dict[str, Any] | None = None,
) -> SecurityHubFindingsAdapter:
    return SecurityHubFindingsAdapter(
        client=client or make_stub_client(),
        filters=filters,
    )


# ---------------------------------------------------------------------------
# to_ticket — severity mapping
# ---------------------------------------------------------------------------


class TestSeverityMapping:
    @pytest.mark.parametrize(
        "label,expected",
        [
            ("CRITICAL", "critical"),
            ("HIGH", "high"),
            ("MEDIUM", "medium"),
            ("LOW", "low"),
            ("INFORMATIONAL", "informational"),
        ],
    )
    def test_all_severity_labels(self, label: str, expected: str) -> None:
        adapter = make_adapter()
        raw = make_finding(severity_label=label)
        ticket = adapter.to_ticket(raw)
        assert ticket.severity == expected

    def test_unknown_severity_label_maps_informational(self) -> None:
        adapter = make_adapter()
        raw = make_finding(severity_label="UNKNOWN")
        ticket = adapter.to_ticket(raw)
        assert ticket.severity == "informational"

    def test_missing_severity_block_maps_informational(self) -> None:
        adapter = make_adapter()
        raw = make_finding()
        del raw["Severity"]
        ticket = adapter.to_ticket(raw)
        assert ticket.severity == "informational"


# ---------------------------------------------------------------------------
# to_ticket — status mapping
# ---------------------------------------------------------------------------


class TestStatusMapping:
    @pytest.mark.parametrize(
        "ws,expected",
        [
            ("NEW", "open"),
            ("NOTIFIED", "open"),
            ("SUPPRESSED", "closed"),
            ("RESOLVED", "resolved"),
        ],
    )
    def test_all_workflow_statuses(self, ws: str, expected: str) -> None:
        adapter = make_adapter()
        raw = make_finding(workflow_status=ws)
        ticket = adapter.to_ticket(raw)
        assert ticket.status == expected

    def test_missing_workflow_block_is_open(self) -> None:
        adapter = make_adapter()
        raw = make_finding()
        del raw["Workflow"]
        ticket = adapter.to_ticket(raw)
        assert ticket.status == "open"

    def test_unknown_workflow_status_is_open(self) -> None:
        adapter = make_adapter()
        raw = make_finding(workflow_status="PENDING")
        ticket = adapter.to_ticket(raw)
        assert ticket.status == "open"


# ---------------------------------------------------------------------------
# to_ticket — field mapping
# ---------------------------------------------------------------------------


class TestFieldMapping:
    def test_source_id_is_finding_id(self) -> None:
        adapter = make_adapter()
        finding_id = "arn:aws:securityhub:us-east-1:123:product/aws/gd/finding-1"
        ticket = adapter.to_ticket(make_finding(finding_id=finding_id))
        assert ticket.source_id == finding_id

    def test_source_system(self) -> None:
        adapter = make_adapter()
        ticket = adapter.to_ticket(make_finding())
        assert ticket.source_system == "securityhub"

    def test_custom_system_name(self) -> None:
        adapter = SecurityHubFindingsAdapter(
            client=make_stub_client(),
            system_name="sh-prod",
        )
        ticket = adapter.to_ticket(make_finding())
        assert ticket.source_system == "sh-prod"

    def test_title(self) -> None:
        adapter = make_adapter()
        ticket = adapter.to_ticket(make_finding(title="Brute force attack"))
        assert ticket.title == "Brute force attack"

    def test_empty_title_gets_default(self) -> None:
        adapter = make_adapter()
        raw = make_finding(title="")
        ticket = adapter.to_ticket(raw)
        assert ticket.title == "Security Hub Finding"

    def test_description_as_string(self) -> None:
        adapter = make_adapter()
        raw = make_finding(description="Plain string description")
        ticket = adapter.to_ticket(raw)
        assert ticket.description == "Plain string description"

    def test_description_as_dict_with_text(self) -> None:
        adapter = make_adapter()
        raw = make_finding()
        raw["Description"] = {"Text": "Dict description text", "Other": "ignored"}
        ticket = adapter.to_ticket(raw)
        assert ticket.description == "Dict description text"

    def test_missing_description(self) -> None:
        adapter = make_adapter()
        raw = make_finding()
        del raw["Description"]
        ticket = adapter.to_ticket(raw)
        assert ticket.description == ""

    def test_category_from_types(self) -> None:
        adapter = make_adapter()
        raw = make_finding(types=["TTPs/Initial Access/Exploit Public-Facing Application"])
        ticket = adapter.to_ticket(raw)
        assert ticket.category == "TTPs/Initial Access/Exploit Public-Facing Application"

    def test_empty_types_empty_category(self) -> None:
        adapter = make_adapter()
        raw = make_finding(types=[])
        ticket = adapter.to_ticket(raw)
        assert ticket.category == ""

    def test_external_url_from_product_arn(self) -> None:
        adapter = make_adapter()
        arn = "arn:aws:securityhub:us-east-1::product/aws/guardduty"
        ticket = adapter.to_ticket(make_finding(product_arn=arn))
        assert ticket.external_url == arn

    def test_timestamps_parsed(self) -> None:
        adapter = make_adapter()
        raw = make_finding(
            created_at="2026-03-10T06:00:00Z",
            updated_at="2026-03-11T18:30:00Z",
        )
        ticket = adapter.to_ticket(raw)
        assert ticket.created_at == datetime(2026, 3, 10, 6, 0, 0, tzinfo=timezone.utc)
        assert ticket.updated_at == datetime(2026, 3, 11, 18, 30, 0, tzinfo=timezone.utc)

    def test_raw_preserved(self) -> None:
        adapter = make_adapter()
        raw = make_finding(finding_id="raw-preserve")
        ticket = adapter.to_ticket(raw)
        assert ticket.raw["Id"] == "raw-preserve"


# ---------------------------------------------------------------------------
# to_ticket — tags
# ---------------------------------------------------------------------------


class TestTags:
    def test_region_tag(self) -> None:
        adapter = make_adapter()
        ticket = adapter.to_ticket(make_finding(region="ap-southeast-1"))
        assert "region:ap-southeast-1" in ticket.tags

    def test_product_tag(self) -> None:
        adapter = make_adapter()
        ticket = adapter.to_ticket(make_finding(product_name="Inspector"))
        assert "product:Inspector" in ticket.tags

    def test_company_tag(self) -> None:
        adapter = make_adapter()
        ticket = adapter.to_ticket(make_finding(company_name="Amazon"))
        assert "company:Amazon" in ticket.tags

    def test_missing_fields_no_tags(self) -> None:
        adapter = make_adapter()
        raw = make_finding(region="", product_name="", company_name="")
        ticket = adapter.to_ticket(raw)
        assert ticket.tags == []


# ---------------------------------------------------------------------------
# to_ticket — entities
# ---------------------------------------------------------------------------


class TestEntities:
    def test_account_entity_created(self) -> None:
        adapter = make_adapter()
        ticket = adapter.to_ticket(make_finding(account_id="555444333222"))
        assert len(ticket.entities) == 1
        entity = ticket.entities[0]
        assert entity.kind == "account"  # type: ignore[union-attr]
        assert entity.uid == "555444333222"  # type: ignore[union-attr]

    def test_missing_account_no_entities(self) -> None:
        adapter = make_adapter()
        raw = make_finding(account_id="")
        ticket = adapter.to_ticket(raw)
        assert ticket.entities == []


# ---------------------------------------------------------------------------
# from_ticket
# ---------------------------------------------------------------------------


class TestFromTicket:
    def test_returns_finding_id(self) -> None:
        adapter = make_adapter()
        ticket = Ticket(
            source_system="securityhub",
            source_id="arn:aws:securityhub:us-east-1:123:finding/x",
            title="Some finding",
            severity="medium",
        )
        result = adapter.from_ticket(ticket)
        assert result["FindingId"] == "arn:aws:securityhub:us-east-1:123:finding/x"
        assert result["Title"] == "Some finding"


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_raises(self) -> None:
        adapter = make_adapter()
        ticket = Ticket(
            source_system="securityhub",
            source_id="x",
            title="T",
            severity="low",
        )
        with pytest.raises(NotImplementedError):
            adapter.write(ticket)


# ---------------------------------------------------------------------------
# fetch_new
# ---------------------------------------------------------------------------


class TestFetchNew:
    def test_empty_response(self) -> None:
        client = make_stub_client(findings=[])
        adapter = make_adapter(client=client)
        assert adapter.fetch_new() == []

    def test_single_finding(self) -> None:
        finding = make_finding(finding_id="arn:aws:x")
        client = make_stub_client(findings=[finding])
        adapter = make_adapter(client=client)
        result = adapter.fetch_new()
        assert len(result) == 1
        assert result[0]["Id"] == "arn:aws:x"

    def test_pagination_followed(self) -> None:
        """get_findings NextToken is followed until exhausted."""
        client = MagicMock()
        client.get_findings.side_effect = [
            {"Findings": [make_finding(finding_id="a")], "NextToken": "tok2"},
            {"Findings": [make_finding(finding_id="b")], "NextToken": ""},
        ]
        adapter = make_adapter(client=client)
        result = adapter.fetch_new()
        assert client.get_findings.call_count == 2
        assert len(result) == 2

    def test_max_results_set(self) -> None:
        client = make_stub_client(findings=[])
        adapter = make_adapter(client=client)
        adapter.fetch_new()
        kwargs = client.get_findings.call_args.kwargs
        assert kwargs.get("MaxResults") == 100

    def test_since_adds_updated_at_filter(self) -> None:
        client = make_stub_client(findings=[])
        adapter = make_adapter(client=client)
        since = datetime(2026, 4, 1, tzinfo=timezone.utc)
        adapter.fetch_new(since=since)
        kwargs = client.get_findings.call_args.kwargs
        filters = kwargs.get("Filters", {})
        assert "UpdatedAt" in filters
        updated_at_filter = filters["UpdatedAt"]
        assert len(updated_at_filter) == 1
        assert "Start" in updated_at_filter[0]
        assert "2026-04-01" in updated_at_filter[0]["Start"]

    def test_since_none_no_updated_at_filter(self) -> None:
        client = make_stub_client(findings=[])
        adapter = make_adapter(client=client)
        adapter.fetch_new()
        kwargs = client.get_findings.call_args.kwargs
        filters = kwargs.get("Filters", {})
        assert "UpdatedAt" not in filters

    def test_base_filters_merged_with_since_filter(self) -> None:
        """Constructor filters are included alongside since-derived filters."""
        client = make_stub_client(findings=[])
        base_filters: dict[str, Any] = {
            "ProductName": [{"Value": "GuardDuty", "Comparison": "EQUALS"}]
        }
        adapter = SecurityHubFindingsAdapter(
            client=client,
            filters=base_filters,
        )
        since = datetime(2026, 4, 1, tzinfo=timezone.utc)
        adapter.fetch_new(since=since)
        kwargs = client.get_findings.call_args.kwargs
        filters = kwargs.get("Filters", {})
        assert "ProductName" in filters
        assert "UpdatedAt" in filters

    def test_no_findings_since_filter(self) -> None:
        """fetch_new() without since does not add UpdatedAt to filters."""
        client = make_stub_client(findings=[])
        adapter = make_adapter(client=client)
        adapter.fetch_new(since=None)
        kwargs = client.get_findings.call_args.kwargs
        filters = kwargs.get("Filters", {})
        assert "UpdatedAt" not in filters
