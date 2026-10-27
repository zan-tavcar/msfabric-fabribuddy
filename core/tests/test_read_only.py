"""FabriBuddy never writes to the tenant it scans: the client refuses before any request is sent."""

import pytest

from fabribuddy_core.fabric.client import FabricClient, FabricError
from fabribuddy_core.fabric.credentials import Credentials


class _NoNetwork:
    """A session that fails the test if a request ever reaches it."""

    def request(self, *args, **kwargs):
        raise AssertionError("a refused request reached the network")


def _client():
    return FabricClient(Credentials("tenant", "client", "secret"), session=_NoNetwork())


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_write_verb_is_refused(method):
    with pytest.raises(FabricError, match="read-only"):
        _client().request(method, "https://api.fabric.microsoft.com/v1/workspaces", "scope")


@pytest.mark.parametrize(
    "url",
    [
        "https://api.fabric.microsoft.com/v1/workspaces",
        "https://api.fabric.microsoft.com/v1/workspaces/w/items",
        "https://api.fabric.microsoft.com/v1/workspaces/w/items/i/updateDefinition",
    ],
)
def test_only_read_shaped_posts_get_through_even_when_allowed(url):
    with pytest.raises(FabricError, match="not a known read-only operation"):
        _client().request("POST", url, "scope", allow_write=True)
