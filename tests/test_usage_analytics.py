from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from metrics import metrics_response, set_usage_analytics
from usage_analytics import (
    FEATURES,
    UsageAnalytics,
    UsageConfig,
    canonical_command,
    inject_usage_tracker,
    render_usage_html,
)


def test_command_aliases_roll_up_to_canonical_commands() -> None:
    assert canonical_command("/ot_map yesterday") == "otm"
    assert canonical_command("/memory_query@my_bot where was I?") == "memq"
    assert canonical_command("not a command") is None
    assert canonical_command("/unknown") is None


def test_usage_summary_includes_used_and_never_used_features(tmp_path: Path) -> None:
    analytics = UsageAnalytics(UsageConfig(True, tmp_path / "usage.sqlite", 365))
    assert analytics.record("telegram.command.otm", "telegram") is True
    assert analytics.record("telegram.command.otm", "telegram") is True
    assert analytics.record("contains spaces", "telegram") is False

    summary = analytics.summary(30)
    by_key = {item["key"]: item for item in summary["features"]}
    assert by_key["telegram.command.otm"]["count"] == 2
    assert by_key["telegram.command.otm"]["status"] == "used"
    assert by_key["telegram.command.oto"]["count"] == 0
    assert by_key["telegram.command.oto"]["status"] == "never"
    assert summary["totals"]["events"] == 2
    assert summary["totals"]["never_used"] == len(FEATURES) - 1


def test_usage_summary_honors_window_and_retention(tmp_path: Path) -> None:
    analytics = UsageAnalytics(UsageConfig(True, tmp_path / "usage.sqlite", 7))
    analytics.record("telegram.command.cmd", "telegram")
    with analytics._connect() as db:
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        db.execute(
            "INSERT INTO usage_events(occurred_at, feature, surface, action) VALUES (?, ?, ?, ?)",
            (old, "telegram.command.cxq", "telegram", "use"),
        )
    analytics.record("telegram.command.cxs", "telegram")
    summary = analytics.summary(30)
    by_key = {item["key"]: item for item in summary["features"]}
    assert by_key["telegram.command.cxq"]["count"] == 0
    lifetime = {item["feature"]: item for item in analytics.prometheus_snapshot()["features"]}
    assert lifetime["telegram.command.cmd"]["count"] == 1


def test_tracker_and_report_do_not_embed_content_values(tmp_path: Path) -> None:
    document = '<html><body><input id="search" value="private place"><button id="toggleEdges">Edges</button></body></html>'
    tracked = inject_usage_tracker(document, "day")
    assert "data-feature-usage" in tracked
    assert "private place" in tracked  # Existing page content is left untouched.
    assert "event.target?.value" not in tracked

    analytics = UsageAnalytics(UsageConfig(True, tmp_path / "usage.sqlite", 365))
    report = render_usage_html(analytics.summary())
    assert "Feature usage" in report
    assert "Removal guidance" in report
    assert "Never used" in report


def test_prometheus_metrics_expose_durable_counts_and_registered_zeroes(tmp_path: Path) -> None:
    analytics = UsageAnalytics(UsageConfig(True, tmp_path / "usage.sqlite", 365))
    analytics.record("telegram.command.otm", "telegram")
    set_usage_analytics(analytics)
    try:
        status, _content_type, body = metrics_response()
    finally:
        set_usage_analytics(None)
    output = body.decode()
    assert status == 200
    assert 'telegram_control_feature_usage_total{category="OwnTracks commands",feature="telegram.command.otm",surface="telegram"} 1.0' in output
    assert 'telegram_control_feature_usage_total{category="OwnTracks commands",feature="telegram.command.oto",surface="telegram"} 0.0' in output
    assert 'telegram_control_feature_last_used_timestamp_seconds{category="OwnTracks commands",feature="telegram.command.otm",surface="telegram"}' in output
