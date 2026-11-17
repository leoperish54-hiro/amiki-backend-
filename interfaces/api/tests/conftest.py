"""Keep unrelated route tests focused; API auth has dedicated real-dependency tests."""
import pytest

from interfaces.api.security import require_api_auth
from interfaces.api.server.app import app


@pytest.fixture(autouse=True)
def bypass_api_auth_for_route_behavior_tests():
    previous = app.dependency_overrides.get(require_api_auth)
    app.dependency_overrides[require_api_auth] = lambda: None
    yield
    if previous is None:
        app.dependency_overrides.pop(require_api_auth, None)
    else:
        app.dependency_overrides[require_api_auth] = previous
