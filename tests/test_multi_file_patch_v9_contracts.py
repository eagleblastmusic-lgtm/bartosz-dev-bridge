from __future__ import annotations

from bdb_bridge.migrations import MIGRATIONS
from bdb_bridge.multi_file_patch_migration import MIGRATION_V9, MIGRATION_V9_STATEMENTS


V9_CHECKSUM = "ff7019381e0c16588fc4871d0041bd44d08a74ee2dfe3f1387274f8715be3af3"


def test_v9_registry_name_statements_and_literal_checksum() -> None:
    assert tuple(migration.version for migration in MIGRATIONS) == tuple(range(1, 10))
    assert MIGRATIONS[8] is MIGRATION_V9
    assert MIGRATION_V9.name == "journal_v9_multi_file_patch_recovery"
    assert MIGRATION_V9.statements == MIGRATION_V9_STATEMENTS
    assert MIGRATION_V9.checksum() == V9_CHECKSUM


def test_v1_through_v8_checksums_remain_frozen() -> None:
    assert tuple(migration.checksum() for migration in MIGRATIONS[:8]) == (
        "1d293179f582464fa10eecd37fb381c0a5913d85ed629c9ec244c8bfdb2fe31a",
        "80178c2da604e77b9f568467ffa54865dbad3867193dc9f489e002cb5c3dbc33",
        "4dffb2c3e5807cba98d8f5323554e625e4acc58559cc807e2728eab7f07bb9db",
        "b19f7ef96b5c9e25ad9cad9c6d2160a667c5c1b5db68d1d0e7accb2f1f2ba3c9",
        "9bfc62c82e71ebbf968f6a171eb0b320a4d2510dec158db13a8d940afd315670",
        "eaac8a58c752800581d5f02504d7d5b509985fbb2638cb6924f5673828689839",
        "639b9d4eaa0e142fc958c9fa0a1a03a2421802a75ba963b84c3b835d28e30cf8",
        "cbc8c9c6b5907c1f4d82cc9f95b095d8cceff4ef4aaca454f883cd3bb2ad55b6",
    )
