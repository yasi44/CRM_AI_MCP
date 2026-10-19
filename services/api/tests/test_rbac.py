import pytest

from app.security import has_perm


@pytest.mark.parametrize("role,perm,want", [
    ("viewer", "accounts:read", True),
    ("viewer", "deal_value:read", False),      # privacy: viewers never see money
    ("viewer", "opportunities:write", False),
    ("rep", "opportunities:write", True),
    ("rep", "audit:read", False),
    ("manager", "audit:read", True),
    ("admin", "users:manage", True),
    ("ghost", "accounts:read", False),         # unknown role => deny by default
])
def test_role_permissions(role, perm, want):
    assert has_perm(role, perm) is want
