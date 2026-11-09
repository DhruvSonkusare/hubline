import pytest

from app.db.engine import normalize_database_url


@pytest.mark.parametrize(
    ("url", "expected_url", "expected_args"),
    [
        (
            "postgresql://u:p@host/db",
            "postgresql+asyncpg://u:p@host/db",
            {},
        ),
        (
            "postgres://u:p@host:5432/db",
            "postgresql+asyncpg://u:p@host:5432/db",
            {},
        ),
        (  # typical Neon URL
            "postgresql://u:p@ep-x.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://u:p@ep-x.ap-southeast-1.aws.neon.tech/neondb",
            {"ssl": "require"},
        ),
        (
            "postgresql+asyncpg://u:p@host/db?sslmode=disable&application_name=hub",
            "postgresql+asyncpg://u:p@host/db?application_name=hub",
            {},
        ),
    ],
)
def test_normalize(url: str, expected_url: str, expected_args: dict[str, str]) -> None:
    assert normalize_database_url(url) == (expected_url, expected_args)


def test_rejects_other_schemes() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        normalize_database_url("mysql://u:p@host/db")
