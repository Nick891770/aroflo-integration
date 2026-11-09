"""Monthly scorecard metrics and client segmentation."""
from data_extractor import DataExtractor, MonthlyMetrics


def test_derived_metrics():
    m = MonthlyMetrics(revenue=10000, materials_cost=3000, labour_cost=4000,
                       completed_jobs=20, primary_client_value=6000, other_client_value=4000)
    m.calculate_derived_metrics()
    assert m.gross_profit_dollars == 7000
    assert m.net_profit_dollars == 3000
    assert m.gross_profit_percent == 70
    assert m.net_profit_percent == 30
    assert m.average_job_value == 500
    assert m.primary_client_percent == 60
    assert m.other_client_percent == 40


def test_zero_revenue_does_not_divide_by_zero():
    m = MonthlyMetrics()
    m.calculate_derived_metrics()
    assert m.gross_profit_percent == 0 and m.average_job_value == 0


def test_primary_client_match_is_case_insensitive():
    ex = DataExtractor(connector=object())
    assert ex._is_primary_client("ACME FACILITIES PTY LTD")
    assert not ex._is_primary_client("Someone Else")
    assert not ex._is_primary_client("")


def test_month_range_handles_leap_year():
    ex = DataExtractor(connector=object())
    start, end = ex._get_month_date_range(2028, 2)
    assert (start.day, end.day) == (1, 29)
