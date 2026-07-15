from src.clamp import clamp_percent


def test_normal_value() -> None:
    assert clamp_percent(50) == 50


def test_upper_bound() -> None:
    assert clamp_percent(120) == 100


def test_lower_bound() -> None:
    assert clamp_percent(-10) == 0
