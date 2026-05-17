"""Package layout tests for domain modules."""

from pathlib import Path

import llming_com


def test_domain_modules_are_not_in_package_root():
    root = Path(llming_com.__file__).parent
    forbidden = [
        "mcp_http_server.py",
        "mcp_stdio_server.py",
        "p2p_admission.py",
        "p2p_proxy.py",
        "remote_access.py",
    ]

    assert [name for name in forbidden if (root / name).exists()] == []


def test_domain_subpackages_exist():
    root = Path(llming_com.__file__).parent
    expected = [
        "access/remote.py",
        "mcp/http_server.py",
        "mcp/stdio_server.py",
        "p2p/admission.py",
        "p2p/proxy.py",
    ]

    assert [path for path in expected if not (root / path).is_file()] == []
